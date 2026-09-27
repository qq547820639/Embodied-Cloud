"""「不存在」这个结论只能由引擎给出，不能由一列空值给出（N-70）。

改前 DockerProvider 有两处伪造缺席：

1. `reconcile` 开头 `if not workspace.container_name: return MISSING` —— 名字是 provision
   先 `docker run --name ec-…` 建容器、之后才持久化到那一列的，中间崩了就留下"容器活着而列为空"；
   同一时间 `destroy`/`pull_artifact` 却按 `ec-{id[:12]}` 推导去找它 ⇒ 一个容器同时是活的和没的。
2. `inspect` 把 **任何** rc!=0 都读成 `{"state": "absent"}` —— 连"守护进程连不上"也算。
   本机 docker CLI 实测两种形状同为 rc=1，只差 stderr：
     不存在        → `error: no such object: <name>`
     连不上守护进程 → `Cannot connect to the Docker daemon at tcp://127.0.0.1:1. Is the docker daemon running?`
   于是 daemon 抖动那段时间里每个 workspace 都"看起来没了"，而释放准入
   `_release_admitted(command_succeeded=False)` 恰恰只认 MISSING ⇒ 卡被放回池子。

改法：名字推导收成一处 `DockerProvider._name`；inspect 失败按 stderr 分 absent／unknown；
reconcile 把 unknown 读成 UNKNOWN。语义借 docker-py 的类型分层（`docker/errors.py:93` 把 404
单独收成 `NotFound(APIError)`，而连接失败不是 APIError）—— 本机取回该文件 5379 字节读过，
但不引依赖：provider 的 provision 侧故意把 argv 交给守护进程自己验收（G0.19 钉着），
换 SDK 会拆掉那批判据，且 edge_agent 是 stdlib-only 包。
"""

import ast
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import Workspace, WorkspaceStatus
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers import docker as docker_module
from app.services.providers.base import RuntimeState
from app.services.providers.docker import DockerProvider

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_DIR = REPO_ROOT / "app" / "services" / "providers"

WORKSPACE_ID = "11111111-2222-3333-4444-555555555555"
DERIVED = f"ec-{WORKSPACE_ID[:12]}"  # 与 provider 的约定同一个串：列没落上时该找这个名字

RUNNING_STATE = '{"Status":"running","Running":true,"ExitCode":0}'
ABSENT_STDERR = f"error: no such object: {DERIVED}\n"
UNREACHABLE_STDERR = (
    "Cannot connect to the Docker daemon at tcp://127.0.0.1:1. Is the docker daemon running?\n"
)


class ScriptedDocker(DockerProvider):
    """`_run` 的返回形状由构造参数决定，argv 全部记录下来。

    夹具用引擎真实吐过的两种 stderr（本机 `docker inspect` 实测读数），不猜文案。
    """

    def __init__(self, settings, *, rc: int = 0, stdout: str = "", stderr: str = ""):
        super().__init__(settings)
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.commands: list[list[str]] = []

    def _run(self, args, *, check=True):
        self.commands.append(list(args))
        return CompletedProcess(args=args, returncode=self.rc, stdout=self.stdout, stderr=self.stderr)


def _workspace(*, container_name: str | None = None) -> Workspace:
    return Workspace(
        id=WORKSPACE_ID,
        name="n",
        template_id="t",
        provider="docker",
        status=WorkspaceStatus.RUNNING.value,
        container_name=container_name,
    )


@pytest.fixture
def with_docker(monkeypatch):
    """让 `shutil.which("docker")` 为真：本轮判的是"问到了引擎之后怎么判"，不是有没有二进制。"""
    monkeypatch.setattr(docker_module.shutil, "which", lambda _name: "/usr/local/bin/docker")
    return Settings(eula_accepted=True)


def _provider(settings, **shape) -> ScriptedDocker:
    return ScriptedDocker(settings, **shape)


# ---------------------------------------------------------------------------
# A 空列不得伪造缺席
# ---------------------------------------------------------------------------


def test_empty_name_column_still_asks_the_engine_and_reports_alive(with_docker):
    """列是空的而引擎说在跑 → ALIVE（改前读 MISSING），且问的就是约定名。"""
    provider = _provider(with_docker, stdout=RUNNING_STATE)
    ws = _workspace(container_name=None)

    assert provider.reconcile(ws) is RuntimeState.ALIVE
    inspect_calls = [c for c in provider.commands if "inspect" in c]
    assert inspect_calls, f"根本没问引擎：{provider.commands}"
    assert inspect_calls[0][-1] == DERIVED, f"没按命名约定推导：{inspect_calls[0]}"


def test_engine_saying_no_such_object_is_missing(with_docker):
    """合规侧（不开火的对照）：引擎亲口说不存在才许判 MISSING。

    这一支改前也绿（当时是被空列判掉的），所以它是"改后别把真缺席读成别的"的护栏，
    不是改前的反证 —— 反证是上一支和下面那支。
    """
    provider = _provider(with_docker, rc=1, stderr=ABSENT_STDERR)
    assert provider.reconcile(_workspace(container_name=None)) is RuntimeState.MISSING


# ---------------------------------------------------------------------------
# B 「问不到」不是「不在了」
# ---------------------------------------------------------------------------


def test_daemon_unreachable_is_unknown_not_absent(with_docker):
    """两种 rc=1 的形状必须分档：连不上守护进程 → inspect state=unknown。

    前提用实测形状钉住：两条 stderr 的 rc 相同（都是 1），只有文案可分 ⇒ 任何按 rc
    判决的实现都必然把两者混为一谈。
    """
    down = _provider(with_docker, rc=1, stderr=UNREACHABLE_STDERR)
    gone = _provider(with_docker, rc=1, stderr=ABSENT_STDERR)

    assert down.inspect(_workspace())["state"] == "unknown"
    assert gone.inspect(_workspace())["state"] == "absent"
    assert down.reconcile(_workspace()) is RuntimeState.UNKNOWN


def test_unknown_does_not_admit_the_gpu_release(with_docker):
    """跨文件后果：daemon 连不上时，释放准入（命令失败那一档）必须拒绝放卡。

    这是本轮真正在防的事——`_release_admitted(command_succeeded=False)` 只认 MISSING，
    而改前 inspect 把连接失败读成 absent ⇒ reconcile 给 MISSING ⇒ 准入放行 ⇒ 容器还在吃
    `--gpus device=N`，卡已回池。
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    provider = _provider(with_docker, rc=1, stderr=UNREACHABLE_STDERR)
    orchestrator = WorkspaceOrchestrator(factory, provider, Path("/tmp/test-absence-evidence"))  # noqa: S108

    ws = _workspace()
    assert orchestrator._release_admitted(ws, command_succeeded=False) is False

    # 另一极如实写在这里，不是本轮改的：`command_succeeded=True` 时 UNKNOWN 是放行的
    # （mock/演示没有可观测 runtime），该限定登记于 N-63 的处置行①（"停止命令成功之后
    # docker 二进制才消失"那一档残留风险）。断出来是为了让这个口子是**说出来的**，
    # 而不是靠读者去猜实现。
    assert orchestrator._release_admitted(ws, command_succeeded=True) is True


# ---------------------------------------------------------------------------
# C 生命周期命令不许对空列静默空转
# ---------------------------------------------------------------------------


def test_stop_start_address_the_derived_container(with_docker):
    """stop/start 真得发命令：改前 `if workspace.container_name:` 让空列变成"成功但没做事"，
    上层拿到"命令没报错"就按成功准入。"""
    for verb in ("stop", "start"):
        provider = _provider(with_docker, stdout=RUNNING_STATE)
        getattr(provider, verb)(_workspace(container_name=None))
        calls = [c for c in provider.commands if f"docker {verb}" in " ".join(c)]
        assert calls, f"{verb} 在列空时整条跳过（没发出任何命令）：{provider.commands}"
        assert calls[0][-1] == DERIVED


# ---------------------------------------------------------------------------
# D 结构判据：一份推导、零处"由列断定缺席"
# ---------------------------------------------------------------------------


def column_decided_absence_offenders(source: str) -> list[int]:
    """列出「以 `not ….container_name` 为条件、在分支里断定 runtime 不在／直接空转」的行号。

    按 AST 判，三种形状都算：
      - `return RuntimeState.MISSING`（伪造缺席，最危险）
      - `return {"state": "absent", ...}`（inspect 的同类写法）
      - 裸 `return`（命令不发就当成功 —— k8s 的 start/stop 改前就是这个形状）
    读数侧（`name = workspace.container_name or …`）不是分支条件，不算。
    """
    offenders: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        guarded = (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Attribute)
            and test.operand.attr == "container_name"
        )
        if not guarded:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Return):
                continue
            value = inner.value
            if value is None:
                offenders.append(inner.lineno)  # 静默空转
                break
            rendered = ast.dump(value)
            if "MISSING" in rendered or "'absent'" in rendered:
                offenders.append(inner.lineno)
                break
    return offenders


def name_derivation_sites(source: str) -> list[int]:
    """列出「容器／Deployment 名的推导式」`f"ec-{…id[:12]}"` 的行号。

    只认首个字面量恰为 `ec-` 的 f-string：`_pvc_name` 的 `f"ec-pvc-{…}"` 是另一种资源的名字，
    不算第二份容器名推导（第一版判据没分这一层，把 PVC 那份数进来读成"两份推导"）。
    文档字符串里的复述是 Constant，不是 JoinedStr，天然不算。
    """
    sites = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.JoinedStr) and node.values:
            first = node.values[0]
            if (
                isinstance(first, ast.Constant)
                and first.value == "ec-"
                and "[:12]" in ast.unparse(node)
            ):
                sites.append(node.lineno)
    return sites


def _provider_sources() -> list[Path]:
    return sorted(p for p in PROVIDERS_DIR.glob("*.py") if p.name != "__init__.py")


def test_no_provider_concludes_absence_from_the_name_column():
    """providers/ 里不许有"列是空的 ⇒ runtime 不在／命令不发"；名字推导每模块恰好一份。"""
    absence: list[str] = []
    derivations: dict[str, int] = {}
    for path in _provider_sources():
        src = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(REPO_ROOT))
        absence.extend(f"{rel}:{line}" for line in column_decided_absence_offenders(src))
        count = len(name_derivation_sites(src))
        if count:
            derivations[rel] = count
    assert absence == [], f"又回到由列断定缺席：{absence}"
    # docker 与 k8s 各有一份推导（f"ec-{workspace.id[:12]}"），且只有那一份
    assert derivations, "一把都数不到 ⇒ 尺子或约定名变了，别把看不见读成合规"
    assert all(v == 1 for v in derivations.values()), derivations


def test_the_structure_ruler_can_see_the_old_shapes():
    """反向对照：改前那三种形状喂同一把尺子都要点名，合规写法不许开火。"""
    assert column_decided_absence_offenders(
        "def reconcile(self, w):\n    if not w.container_name:\n        return RuntimeState.MISSING\n"
    ) == [3]
    assert column_decided_absence_offenders(
        "def inspect(self, w):\n    if not w.container_name:\n        return {'state': 'absent', 'n': None}\n"
    ) == [3]
    assert column_decided_absence_offenders(
        "def stop(self, w):\n    if not w.container_name:\n        return\n"
    ) == [3]
    # 合规：读列做推导、或判空之后去问引擎
    assert column_decided_absence_offenders(
        "def stop(self, w):\n    name = w.container_name or f'ec-{w.id[:12]}'\n    self._run_checked(name)\n"
    ) == []
    assert name_derivation_sites(
        "def _name(self, w):\n    return w.container_name or f\"ec-{w.id[:12]}\"\n"
    ) == [2]
    assert name_derivation_sites('"""按 ec-{workspace.id[:12]} 推导。"""\n') == []
    # 另一种资源的名字不算第二份容器名推导（第一版把 PVC 那份数进来，读成"两份推导"）
    assert name_derivation_sites("def _pvc_name(w):\n    return f\"ec-pvc-{w.id[:12]}\"\n") == []


def test_the_ruler_is_not_blind_on_the_real_providers():
    """正向对照（非零）：把改前那行放回真源码上，尺子必须点名 —— 否则上一条只是自测。"""
    src = (PROVIDERS_DIR / "docker.py").read_text(encoding="utf-8")
    mutant = src.replace(
        "        if inspect.get(\"state\") == \"absent\":",
        "        if not workspace.container_name:\n            return RuntimeState.MISSING\n"
        "        if inspect.get(\"state\") == \"absent\":",
        1,
    )
    assert mutant != src, "锚点没落地（命中 0 会被读成尺子失灵）"
    assert column_decided_absence_offenders(mutant), "变异放回了假缺席而尺子沉默"
