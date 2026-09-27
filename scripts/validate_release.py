#!/usr/bin/env python3
"""Release validation：自动运行全部软件 gate 并产出两份报告。

- `docs/VALIDATION.{json,md}`（**提交物**）：只含可复现门禁 —— 内容是仓库代码的函数，
  换机器重跑必须逐字节相同，CI 的 `git diff --exit-code` 才有意义。
- `dist/VALIDATION_RUN.{json,md}`（**本次跑读数**，gitignored）：各集成档状态、
  跳过哪几支等随环境（daemon、外网通道、已装二进制）而变的事实。

分开存放的理由（N-31 实测）：同一棵树跑两次，一次 `skipped=1`、一次 `skipped=2`
（registry 通道抖动让 docker 档一条自判 PENDING），旧版把两类读数混在一份提交物里，
于是"外网今天通不通"会改动版本库，并把 CI 的新鲜度门禁变成随机红。

Gate：test collected/failed、lint、type、migration、build、skip 闭集、
GPU/K8s/Streaming/Robot 物理状态。CURRENT_STATE 引用本文件输出。

用法：python scripts/validate_release.py
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable  # 当前解释器（venv）

# 本文件既可 `make validate` 跑，也可被常驻判据按路径 importlib 加载（那时 scripts/ 不在
# sys.path 上）——构建时间口径必须还是同一份实现，所以这里显式把 scripts/ 挂进来。
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from build_env import epoch_env  # noqa: E402

# 环境读数的落点。必须在 gitignored 的 dist/ 下（常驻判据 tests/test_validation_matrix.py
# 钉住这一点），否则它又变回提交物，本轮的拆分就白做。
RUN_REPORT_JSON = ROOT / "dist" / "VALIDATION_RUN.json"
RUN_REPORT_MD = ROOT / "dist" / "VALIDATION_RUN.md"

# 哪些 check 键属于环境读数（不进提交面）。`integration_` 前缀覆盖全部六档；
# `unexpected_skips` 的判决取自本次跑的 skip 集合，同样属环境。
ENV_CHECK_KEYS = ("unexpected_skips",)
ENV_CHECK_PREFIXES = ("integration_",)
# 哪些字段在"可复现的 check"里其实属环境读数（整条 check 留下、剥掉这几个字段）。
ENV_FIELDS: dict[str, tuple[str, ...]] = {
    "test_run": ("passed", "skipped", "skipped_names"),
}


def run(cmd: list[str], timeout: int = 900, env: dict[str, str] | None = None) -> tuple[int, str]:
    # S603: cmd 由本脚本受控常量构造（[sys.executable, "-m", pytest/ruff/mypy/...]），无用户输入
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout, env=env)  # noqa: S603
    return result.returncode, (result.stdout + result.stderr).strip()


def build_env() -> dict[str, str]:
    """构建用的环境：`SOURCE_DATE_EPOCH` 钉到 HEAD 提交时间。

    口径不在本文件里（见 scripts/build_env.py 的模块注释）：Makefile 与 validate
    两处都得走同一个 `epoch_env()`，`make validate` 会 export 一份、直接跑本脚本时
    由它补上同一条兜底 —— 两条路径的产物必须是同一秒。
    """
    return epoch_env(dict(os.environ))


def count_tests_junit(report_path: Path) -> dict:
    """§16：用 JUnit XML 的 testsuite 属性稳定计数（不解析 pytest 文本输出）。

    同时把**失败用例名**捞出来：只报 "failed: 1" 的报告等于没有报告——本轮就出现过
    一次"全量跑挂一条、日志里连用例名都没有"（validate 丢弃了 pytest 输出），
    排查只能靠重跑撞运气。名字进 VALIDATION.json 之后，一次偶发失败也是可归因的。
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(report_path)  # noqa: S314 pytest 生成的本地 JUnit（受控输出，非外部输入）
    root = tree.getroot()
    # pytest 产出 <testsuites><testsuite .../></testsuites>
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return {"collected": 0, "passed": 0, "skipped": 0, "failed": 0,
                "failed_names": [], "skipped_names": [], "skips": [], "test_names": []}
    failed_names = [
        f"{tc.get('classname') or ''}::{tc.get('name') or ''}"
        for tc in root.iter("testcase")
        if tc.find("failure") is not None or tc.find("error") is not None
    ]
    # skip 也按用例名留痕（总数不够）：总数只能告诉我"抖了几支"，名字才能告诉我
    # "抖的是不是那支外网通道的判据"，也才能判"有没有第三支偷偷开始跳"。
    skips = [
        {
            "id": f"{tc.get('classname') or ''}::{tc.get('name') or ''}",
            "message": (tc.find("skipped").get("message") or ""),
            "type": (tc.find("skipped").get("type") or ""),
        }
        for tc in root.iter("testcase")
        if tc.find("skipped") is not None
    ]
    return {
        "collected": int(suite.get("tests", 0)),
        "passed": int(suite.get("tests", 0))
        - int(suite.get("failures", 0))
        - int(suite.get("errors", 0))
        - int(suite.get("skipped", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "failed": int(suite.get("failures", 0)) + int(suite.get("errors", 0)),
        "failed_names": failed_names,
        "skipped_names": [s["id"] for s in skips],
        "skips": skips,
        "test_names": [
            f"{tc.get('classname') or ''}::{tc.get('name') or ''}" for tc in root.iter("testcase")
        ],
    }


def print_failure_scene(report_path: Path, max_cases: int = 6, tail_lines: int = 14) -> None:
    """把红掉那几条的 traceback 尾部打到 stdout（不进报告，报告要确定性）。

    有了名字仍然不够：本轮 4 条红里两条是"文档占位符没填"（一眼可知），
    两条是状态相关的（要看现场才知道是配额/池子还是断言）。
    """
    import xml.etree.ElementTree as ET

    print(f"\n[validate] ===== 失败现场（完整报告：{os.path.relpath(report_path, ROOT)}）=====")
    root = ET.parse(report_path).getroot()  # noqa: S314 pytest 生成的本地 JUnit
    shown = 0
    for tc in root.iter("testcase"):
        node = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
        if node is None:
            continue
        shown += 1
        if shown > max_cases:
            print("[validate] …其余失败见 dist/validate-junit.xml")
            break
        print(f"\n--- {tc.get('classname')}::{tc.get('name')}  [{node.get('message', '')[:120]}]")
        body = (node.text or "").strip().splitlines()
        print("\n".join(body[-tail_lines:]))


def integration_gate_statuses(junit_path: Path, gates: dict[str, dict]) -> dict[str, dict]:
    """按 JUnit 的 `testcase@file` + `<skipped message>` 现算集成档状态。

    三态分开：PASS（真后端上跑过）/ PENDING(原因)（整档因缺件跳过，原因来自 skip
    文案本身）/ NOT_RUN（该档一条都没被收集到——例如文件被删或 selection 打错）。
    不从"退出码"倒推原因，也不把普通 skip 折算成通过。
    """
    import xml.etree.ElementTree as ET

    root = ET.parse(junit_path).getroot()  # noqa: S314 本地 pytest 产物
    buckets: dict[str, dict[str, object]] = {}
    for tc in root.iter("testcase"):
        # pytest 的 JUnit 用 classname="tests.test_x"（无 file 属性）标模块归属；
        # 参数化/类内用例会带 "::"，归属取第一段
        module = (tc.get("classname") or "").split("::")[0]
        if not module:
            continue
        b = buckets.setdefault(module, {"total": 0, "passed": 0, "bad": 0, "skipped": []})
        b["total"] += 1
        skipped = tc.find("skipped")
        if skipped is not None:
            b["skipped"].append(skipped.get("message") or "")
        elif tc.find("failure") is not None or tc.find("error") is not None:
            b["bad"] += 1
        else:
            b["passed"] += 1

    out: dict[str, dict] = {}
    for name, spec in gates.items():
        b = buckets.get(spec["module"])
        if b is None or b["total"] == 0:
            out[name] = {"status": "NOT_RUN", "note": f"未收集到 {spec['module']} 的任何用例"}
            continue
        total, passed, bad = int(b["total"]), int(b["passed"]), int(b["bad"])
        skipped_msgs = [str(m) for m in b["skipped"]]
        sentinel = spec["sentinel"]
        pending = [m for m in skipped_msgs if sentinel in m]
        if bad:
            out[name] = {"status": "FAIL", "note": f"{bad}/{total} 用例在真实后端上失败"}
        elif passed == 0 and len(pending) == total:
            reason = pending[0].split(":", 1)[-1].strip() if ":" in pending[0] else pending[0]
            out[name] = {
                "status": "PENDING",
                "note": f"整档 {total} 用例未执行，原因：{reason}（哨兵 {sentinel}）",
            }
        elif skipped_msgs:
            out[name] = {
                "status": "PARTIAL",
                "note": f"执行 {passed}/{total}，跳过 {len(skipped_msgs)}（哨兵 {sentinel} 命中 {len(pending)}）",
            }
        else:
            out[name] = {"status": "PASS", "note": f"{passed}/{total} 用例在真实后端上执行"}
    return out


def _const_str(rel_path: str, name: str) -> str:
    """从用例源码的 AST 里现取模块级字符串常量（哨兵不许手抄）。"""
    import ast

    tree = ast.parse((ROOT / rel_path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name and isinstance(node.value, ast.Constant):
                    return str(node.value.value)
    raise LookupError(f"{rel_path} 里没有模块级字符串常量 {name}")


def _integration_gates() -> dict[str, dict]:
    return {
        "k8s": {
            "module": "tests.test_k8s_integration",
            "sentinel": _const_str("tests/test_k8s_integration.py", "GATE_SENTINEL"),
        },
        "postgres": {
            "module": "tests.test_postgres_concurrency",
            "sentinel": _const_str("tests/pg_server.py", "GATE_SENTINEL"),
        },
        "docker": {
            "module": "tests.test_docker_provider_integration",
            "sentinel": _const_str("tests/test_docker_provider_integration.py", "GATE_SENTINEL"),
        },
        "browser": {
            "module": "tests.test_browser_console",
            "sentinel": _const_str("tests/test_browser_console.py", "GATE_SENTINEL"),
        },
        "object_store": {
            "module": "tests.test_s3_artifact_store",
            "sentinel": _const_str("tests/s3_server.py", "GATE_SENTINEL"),
        },
        "k8s_control_plane": {
            "module": "tests.test_k8s_control_plane",
            "sentinel": _const_str("tests/k8s_server.py", "GATE_SENTINEL"),
        },
    }


# 不是"集成档"、但确实按条件跳过的模块：闭集的第二个来源。
# 这里是空的——原先唯一的住户 `test_gpu_pool_guard` 在 N-33 收了口：夹具显式回收并**断言**
# 余量，不再有条件跳过分支。机制留着（将来若再有合法的条件跳过，必须同时在这里登记 +
# 用例里带哨兵常量，由 _const_str 走 AST 现取，不手抄），否则 unexpected_skips 会把它报成越界。
EXTRA_SKIP_UNIVERSE: tuple[tuple[str, str], ...] = ()


def _dotted(rel_path: str) -> str:
    """tests/test_x.py → tests.test_x（JUnit 的 classname 用点号形式）。"""
    return rel_path.removesuffix(".py").replace("/", ".")


def require_name(spec: str) -> str:
    """PEP 508 条目取包名：'setuptools>=75' → 'setuptools'（比较符/extras/环境标记一律截断）。"""
    return re.split(r"[<>=!~;\[ ]", spec, maxsplit=1)[0].strip().lower().replace("_", "-")


def optional_extra(pyproject_text: str, name: str) -> list[str]:
    import tomllib

    return tomllib.loads(pyproject_text)["project"]["optional-dependencies"].get(name, [])


def locked_version(lock_text: str, name: str) -> str | None:
    """从 uv.lock 取某个包被锁住的版本（不能按行找：每个包都有 version 行）。"""
    for block in lock_text.split("[[package]]"):
        if re.search(rf'^name = "{re.escape(name)}"$', block, re.MULTILINE):
            found = re.search(r'^version = "([^"]+)"', block, re.MULTILINE)
            return found.group(1) if found else None
    return None


def spec_satisfied(spec: str, version: str) -> bool:
    """锁住的版本是否满足那条 requires 区间（判据交给 packaging，不自研比较器）。"""
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    return Version(version) in SpecifierSet(re.sub(r"^[A-Za-z0-9._-]+", "", spec))


def conditional_skip_universe() -> dict[str, str]:
    """允许跳过的闭集：{模块点号名: 该模块 skip 文案必须含的哨兵}。

    哨兵一律由 AST 现取源码常量（_const_str），不许手抄 —— 手抄那份会在哨兵改名时
    继续"看着对"，而闭集当场失效。
    """
    universe = {spec["module"]: spec["sentinel"] for spec in _integration_gates().values()}
    for rel_path, const in EXTRA_SKIP_UNIVERSE:
        universe[_dotted(rel_path)] = _const_str(rel_path, const)
    return universe


def skip_admission_offenders(universe: dict[str, str], skips: list[dict]) -> list[str]:
    """本次跑的 skip 是否全在闭集内。纯函数，可喂夹具。"""
    out: list[str] = []
    for skip in skips:
        case_id = str(skip.get("id", ""))
        module = case_id.split("::")[0]
        message = str(skip.get("message", ""))
        sentinel = universe.get(module)
        if sentinel is None:
            out.append(f"{case_id}: 该模块不在条件跳过闭集内（skip 文案 {message[:60]!r}）")
        elif sentinel not in message:
            out.append(f"{case_id}: 文案未含本模块哨兵 {sentinel}，是普通 skip 不是缺件")
    return out


def _is_env_check(key: str) -> bool:
    return key in ENV_CHECK_KEYS or key.startswith(ENV_CHECK_PREFIXES)


# PENDING 的说明必须落到"缺什么"上：跳过本身不是终点，读的人要能照着补环境。
# 原因文本都是代码里的 skip 文案（常量），所以这条判据的结论不随环境变：
# 全档跑通时没有 PENDING = 无可核 = PASS；跳了档就必须可行动。
ACTIONABLE_MARKERS = (
    "缺", "未", "不可", "无法", "需要", "超时", "不在场", "没装", "装不上", "不匹配", "拒绝",
)


# "可行动"有两种成立方式：说出缺什么（关键词表），或直接给一条能跑的动作。
# 只给动作这一支是新加的：docker 档的原因原本描述了现状（"没有本地缓存…试过：…"），
# 既没有关键词也没有动作，被 N-49 的半挂剧本抓出来后补上了 `docker pull` / 变量名两条指引。
ACTION_RE = re.compile(r"docker pull|pip install|export [A-Z_][A-Z0-9_]*|make [a-z][a-z0-9-]*")


def pending_reason_offenders(statuses: dict[str, object]) -> list[str]:
    out: list[str] = []
    for key, value in sorted(statuses.items()):
        if not isinstance(value, dict) or value.get("status") != "PENDING":
            continue
        note = str(value.get("note") or "").strip()
        _before, sep, tail = note.partition("原因：")
        if not sep:
            out.append(f"{key}: PENDING 却没留下『原因：…』段：{note[:70] or '(空)'}")
            continue
        reason, _, sent = tail.partition("（哨兵")
        reason = reason.strip()
        sentinel = sent.rstrip("）").strip()
        if not reason:
            out.append(f"{key}: PENDING 的原因段是空的（哨兵 {sentinel or '?'}）")
        elif reason == sentinel:
            out.append(f"{key}: 原因只是把哨兵又抄了一遍（{reason}）——补环境的人看不出缺什么")
        elif not any(marker in reason for marker in ACTIONABLE_MARKERS) and not re.search(ACTION_RE, reason):
            out.append(f"{key}: 原因没指向可行动缺项：{reason[:70]}")
    return out


# 操作指针核到"引用的东西真的存在"：档位原因与文档里的变量名、`make` 目标、`.[extra]`
# 是人照着补环境的入口，而人会记错字母（这条判据上线后抓到的第一个真错就是我自己写的
# 一个复数形式的开关名——仓里真正被读的是单数）。目录来自仓内事实，不依赖环境。
POINTER_DOCS = ("docs",)
POINTER_EXTRA_DOCS = ("DELIVERY.md", "README.md", "CHANGELOG.md")
POINTER_MODULES = (
    "tests/pg_server.py", "tests/s3_server.py", "tests/k8s_server.py",
    "tests/test_docker_provider_integration.py", "tests/test_browser_console.py",
    "tests/test_k8s_integration.py",
)
# 生成物：它们是这条判据的输出，不能反过来当输入
GENERATED_DOCS = frozenset({"docs/VALIDATION.md"})
ENV_RE = re.compile(r"EMBODIEDCLOUD_[A-Z0-9_]+")
MAKE_RE = re.compile(r"\bmake ([a-z][a-z0-9-]*)")
EXTRA_RE = re.compile(r"\.\[([a-z0-9]+)")


def env_names_read_by(sources: dict[str, str]) -> set[str]:
    """AST 找**真的被读**的环境变量名：`os.environ["X"]` / `.get("X")` / `getenv("X")`，
    含 `CONST = "X"` 之后 `os.environ[CONST]` 这种间接写法。

    注释与文档字符串里的名字不算出处——判据要能抓的正是"写着像有、其实没人读"。
    """
    from ast import Assign, Attribute, Call, Constant, Name, Subscript, parse, walk

    def is_env_root(node: object) -> bool:
        # os.environ / environ / os.getenv 的宿主
        parts: list[str] = []
        while isinstance(node, Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, Name):
            parts.append(node.id)
        return "environ" in parts or "os" in parts

    def resolve(name: str, str_values: dict[str, str], ref_values: dict[str, str]) -> str | None:
        """跟着 `A = B = … = "字面量"` 走到底；有环就停在 None，不递归炸栈。"""
        seen: set[str] = set()
        current: str | None = name
        while current is not None and current not in seen:
            seen.add(current)
            if current in str_values:
                return str_values[current]
            current = ref_values.get(current)
        return None

    found: set[str] = set()
    for text in sources.values():
        tree = parse(text)
        str_values: dict[str, str] = {}
        ref_values: dict[str, str] = {}
        for node in tree.body:
            if not (isinstance(node, Assign) and len(node.targets) == 1 and isinstance(node.targets[0], Name)):
                continue
            target = node.targets[0].id
            if isinstance(node.value, Constant) and isinstance(node.value.value, str):
                str_values[target] = node.value.value
            elif isinstance(node.value, Name):
                ref_values[target] = node.value.id

        for node in walk(tree):
            first: object | None = None
            if isinstance(node, Subscript) and is_env_root(node.value):
                first = node.slice
            elif isinstance(node, Call) and isinstance(node.func, (Attribute, Name)):
                fname = node.func.attr if isinstance(node.func, Attribute) else node.func.id
                if fname in {"get", "getenv"} and (fname == "getenv" or is_env_root(node.func.value)):
                    first = node.args[0] if node.args else None
            if isinstance(first, Constant) and isinstance(first.value, str):
                found.add(first.value)
            elif isinstance(first, Name):
                resolved = resolve(first.id, str_values, ref_values)
                if resolved:
                    found.add(resolved)
    return found


def reference_catalog() -> dict[str, set[str]]:
    """{env, make, extra} 三个目录，全部从仓内来源现取。"""
    everywhere = {
        str(f): f.read_text(encoding="utf-8", errors="replace")
        for d in ("app", "edge_agent", "tests", "scripts")
        for f in sorted((ROOT / d).rglob("*.py"))
    }
    config = (ROOT / "app/config.py").read_text(encoding="utf-8")
    prefix_match = re.search(r'env_prefix="([^"]+)"', config)
    prefix = prefix_match.group(1) if prefix_match else "EMBODIEDCLOUD_"
    fields = re.findall(r"^    ([a-z0-9_]+)\s*[:=]", config, re.M)
    env = env_names_read_by(everywhere) | {prefix + f.upper() for f in fields}
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", makefile, re.M))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = pyproject.split("[project.optional-dependencies]")[-1]
    extras = set(re.findall(r"^([a-z0-9]+)\s*=\s*\[", section, re.M))
    return {"env": env, "make": targets, "extra": extras}


def pointer_bearing_texts() -> dict[str, str]:
    """被扫的文本：面向人的文档 + 档位前置模块（原因文案就在那儿）。

    跳过生成物：`docs/VALIDATION.md` 是这份报告自己的输出，它会把上一轮的 FAIL 原因
    （里面就有被点名的指针）原样抄回来 —— 扫它就等于让报告自己喂自己红一轮。
    """
    out: dict[str, str] = {}
    for doc in sorted((ROOT / "docs").glob("*.md")):
        rel = doc.relative_to(ROOT).as_posix()
        if rel in GENERATED_DOCS:
            continue
        out[rel] = doc.read_text(encoding="utf-8", errors="replace")
    for rel in POINTER_EXTRA_DOCS:
        path = ROOT / rel
        if path.exists():
            out[rel] = path.read_text(encoding="utf-8", errors="replace")
    for rel in POINTER_MODULES:
        path = ROOT / rel
        if path.exists():
            out[rel] = path.read_text(encoding="utf-8", errors="replace")
    return out


def dangling_reference_offenders(texts: dict[str, str], catalog: dict[str, set[str]]) -> list[str]:
    """三种失效指针各自点名；目录空或一处引用都没扫到 ⇒ 判据恒真，也算偏离。"""
    out: list[str] = []
    for kind in ("env", "make", "extra"):
        if not catalog.get(kind):
            out.append(f"目录的 {kind} 一侧为空：这条判据无法判任何东西（恒真）")
    if not texts:
        out.append("没有一份文本被扫：这条判据无事可做")
        return out
    found = 0
    for name, text in sorted(texts.items()):
        for env in sorted(set(ENV_RE.findall(text))):
            found += 1
            if env not in catalog["env"]:
                out.append(f"{name}: 引用了没人读的变量 {env}")
        for target in sorted(set(MAKE_RE.findall(text))):
            found += 1
            if target not in catalog["make"]:
                out.append(f"{name}: 引用了不存在的目标 make {target}")
        for extra in sorted(set(EXTRA_RE.findall(text))):
            found += 1
            if extra not in catalog["extra"]:
                out.append(f"{name}: 引用了不存在的 extra .[{extra}]")
    if not found:
        out.append("一处操作指针都没扫到：覆盖面可疑，不按\'通过\'处理")
    return out


def reproducible_checks(checks: dict) -> dict:
    """提交面：剥掉环境读数后的报告。换机器重跑必须逐字节相同。"""
    view: dict[str, dict] = {}
    for key, value in checks.items():
        if _is_env_check(key):
            continue
        drop = ENV_FIELDS.get(key, ())
        view[key] = {k: v for k, v in value.items() if k not in drop} if drop else value
    return view


def mask_offenders(checks: dict) -> list[str]:
    """遮罩自证：声明的环境字段必须真在报告里，且遮完还得剩得下可复现门禁。

    没有这一条，"把某个字段划进环境读数"和"把报告删了"在效果上无法区分 ——
    投影判据（两档对照）也就成了摆设。
    """
    out: list[str] = []
    for key, fields in ENV_FIELDS.items():
        entry = checks.get(key)
        if entry is None:
            out.append(f"遮罩声明的 check {key} 不在报告里（清单已过期）")
            continue
        missing = [f for f in fields if f not in entry]
        if missing:
            out.append(f"{key} 里没有环境字段 {missing}（遮罩指向不存在的字段）")
    known = [k for k in checks if _is_env_check(k)]
    if len(reproducible_checks(checks)) < 4:
        out.append(f"可复现面只剩 {len(reproducible_checks(checks))} 项门禁，遮罩把报告遮没了")
    if not known and any(k for k in checks if k == "test_run"):
        out.append("报告里一个环境读数都没有：闭集/环境分档判据无事可做")
    return out


def main() -> int:
    checks: dict[str, dict] = {}
    gates = _integration_gates()

    # 1) 全量 test run（JUnit 报告 → 稳定计数）
    # JUnit 落在 dist/（gitignored）而不是临时目录：临时目录随进程退出，
    # 于是"红过哪几条"除了名字之外什么都留不下来，排查只能重跑撞运气。
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    junit = dist / "validate-junit.xml"
    code, _ = run([PYTHON, "-m", "pytest", "--junitxml", str(junit)])
    universe = conditional_skip_universe()
    if junit.exists():
        counts = count_tests_junit(junit)
        checks["test_collected"] = {"status": "PASS", "count": counts["collected"]}
        checks["test_run"] = {
            "status": "PASS" if code == 0 else "FAIL",
            "passed": counts["passed"],
            "skipped": counts["skipped"],
            "failed": counts["failed"],
            # 报告里只放名字、不放 message：报告必须确定性（CI freshness 门禁比较
            # git diff），而失败消息里带时间/端口就每次不同。现场另打 stdout。
            "failed_names": counts["failed_names"],
            "skipped_names": counts["skipped_names"],
        }
        checks.update({f"integration_{k}": v for k, v in integration_gate_statuses(junit, gates).items()})
        offenders = skip_admission_offenders(universe, counts["skips"])
        checks["unexpected_skips"] = {
            "status": "FAIL" if offenders else "PASS",
            "note": (
                "; ".join(offenders)
                if offenders
                else f"本次跳过 {counts['skipped']} 支，全部落在闭集内（闭集 {len(universe)} 个模块）"
            ),
        }
        if counts["failed"]:
            print_failure_scene(junit)
    else:
        checks["test_collected"] = {"status": "FAIL", "count": 0}
        checks["test_run"] = {
            "status": "FAIL", "passed": 0, "skipped": 0, "failed": 0, "failed_names": [],
            "skipped_names": [],
        }
        checks.update({
            f"integration_{k}": {"status": "NOT_RUN", "note": "pytest 未产出 JUnit 报告"}
            for k in gates
        })
        # 没有 JUnit 就读不到 skip 名单；此时闭集判据无从判，如实记 NOT_RUN 而不是 PASS。
        checks["unexpected_skips"] = {"status": "NOT_RUN", "note": "pytest 未产出 JUnit 报告"}

    # 3) lint / type / migration / build
    code, _ = run([PYTHON, "-m", "ruff", "check", "app", "tests", "edge_agent", "scripts"])
    checks["lint"] = {"status": "PASS" if code == 0 else "FAIL"}

    code, mypy_out = run([PYTHON, "-m", "mypy", "app", "edge_agent"])
    checks["typecheck"] = {"status": "PASS" if code == 0 else "FAIL"}
    files = mypy_source_files(mypy_out)
    if files is not None:
        checks["typecheck"]["files"] = files

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{tmp}/validate.db"
        env = dict(os.environ, EMBODIEDCLOUD_DATABASE_URL=db_url)
        result = subprocess.run(  # noqa: S603 受控常量参数（alembic CLI），无用户输入
            [PYTHON, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT, text=True, capture_output=True, env=env, timeout=300,
        )
        checks["migration"] = {"status": "PASS" if result.returncode == 0 else "FAIL"}
        checks["migration"]["chain"] = alembic_chain_length()

    # 构建后端来自锁（--no-isolation），产物时间来自 HEAD 提交（SOURCE_DATE_EPOCH），
    # 两条判据见 tests/test_validation_matrix.py
    code, _ = run([PYTHON, "-m", "build", "--no-isolation"], env=build_env())
    checks["build"] = {"status": "PASS" if code == 0 else "FAIL"}

    # 4) 物理 gate：仍缺硬件的档保持 NOT_RUN（不假装 PASS，也不倒推原因）。
    #    K8s/Postgres 不再写死在这里 —— 它们由上面的 integration_* 从 JUnit 现算。
    for name in ("gpu", "streaming", "robot"):
        checks[f"physical_{name}"] = {
            "status": "NOT_RUN",
            "note": "需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行",
        }

    # 5) 文档 ↔ 实测计数对账（必须在这里做：pytest 阶段看不到这份新报告）
    version = _read_version()
    docs_offenders = docs_counts_discrepancies(version, checks)
    checks["docs_test_counts"] = {
        "status": "FAIL" if docs_offenders else "PASS",
        "note": "; ".join(docs_offenders) if docs_offenders else "CHANGELOG/CURRENT_STATE 计数面与本报告一致",
    }
    row_offenders = doc_row_order_discrepancies()
    checks["docs_row_order"] = {
        "status": "FAIL" if row_offenders else "PASS",
        "note": "; ".join(row_offenders) if row_offenders else "带编号的登记表行均按号递增且无重号",
    }
    # 遮罩自证：环境读数清单过期（指向不存在的字段）或把可复现面遮没，都当场判红。
    split_offenders = mask_offenders(checks)
    checks["report_split"] = {
        "status": "FAIL" if split_offenders else "PASS",
        "note": "; ".join(split_offenders) if split_offenders else "环境读数清单与报告字段对得上",
    }
    # 操作指针（变量名 / make 目标 / extra）必须能在仓内找到出处：文档与档位原因一起扫。
    ref_offenders = dangling_reference_offenders(pointer_bearing_texts(), reference_catalog())
    ref_note = "文档与档位原因里的 env / make 目标 / extra 引用都有出处"
    checks["reason_references"] = {
        "status": "FAIL" if ref_offenders else "PASS",
        "note": "; ".join(ref_offenders) if ref_offenders else ref_note,
    }
    # PENDING 的说明要能指着补：健康时没有 PENDING 档 = 无可核 = PASS。
    tier_statuses = {k: v for k, v in checks.items() if k.startswith("integration_")}
    pending_offenders = pending_reason_offenders(tier_statuses)
    pending_note = "PENDING 档的原因都指向可行动缺项（无 PENDING 档时视为通过）"
    checks["pending_reasons"] = {
        "status": "FAIL" if pending_offenders else "PASS",
        "note": "; ".join(pending_offenders) if pending_offenders else pending_note,
    }
    # 状态页 §1：可复现的数对上、随环境抖的数不许手抄。事实源缺位时不猜 0，直接判红。
    missing_facts = [
        f"{key} 没给出 {field}：状态页对账失去事实源"
        for key, field in (("typecheck", "files"), ("migration", "chain"))
        if field not in checks[key]
    ]
    state_offenders = missing_facts or state_row_offenders(
        (ROOT / "docs" / "CURRENT_STATE.md").read_text(encoding="utf-8"),
        typecheck_files=int(checks["typecheck"].get("files", 0)),
        migration_chain=int(checks["migration"].get("chain", 0)),
    )
    checks["docs_state_rows"] = {
        "status": "FAIL" if state_offenders else "PASS",
        "note": "; ".join(state_offenders) if state_offenders else "状态页的可复现数与本报告一致，且未手抄环境读数",
    }

    # 6) 汇总：退出码看全量（环境读数红也一样挡发布），提交面的 overall 只看可复现项。
    run_failed = any(c["status"] == "FAIL" for c in checks.values())
    reproducible = reproducible_checks(checks)
    overall = "FAIL" if any(c["status"] == "FAIL" for c in reproducible.values()) else "PASS_WITH_PHYSICAL_PENDING"
    # 注意：提交面内容必须**确定性**（不含时间戳、不含环境读数）——CI freshness 门禁是
    # `make validate && git diff --exit-code docs/VALIDATION.*`，任何每次运行
    # 都变化的内容（生成时间、本次跳了几支）都会让门禁随机红。生成时间不入报告。
    # "checks" 必须字面经过 reproducible_checks( —— 常驻判据按这条字面接线开火。
    report = {
        "version": version,
        "overall": overall,
        "checks": reproducible_checks(checks),
        "environment_readings": "dist/VALIDATION_RUN.md（本次跑读数，不进版本库）",
    }
    run_report = {
        "version": version,
        "overall": "FAIL" if run_failed else "PASS_WITH_PHYSICAL_PENDING",
        "checks": checks,
        "conditional_skip_universe": universe,
    }

    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "VALIDATION.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (docs / "VALIDATION.md").write_text(_render_md(report))
    RUN_REPORT_JSON.write_text(json.dumps(run_report, indent=2, ensure_ascii=False))
    RUN_REPORT_MD.write_text(_render_md(run_report, title_suffix="（本次跑读数，不进版本库）"))
    print(
        f"[validate] overall={report['overall']} run_overall={run_report['overall']} "
        f"tests={checks['test_run']} skips={checks['unexpected_skips']['status']}"
    )
    return 1 if run_failed else 0


def _read_version() -> str:
    init = ROOT / "app" / "__init__.py"
    for line in init.read_text().splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    return "unknown"


# 文档引用实测计数时的唯一合法写法：**只写可复现的那两个数**（collected / failed），
# 顺序固定、空白放开（markdown 正文换行是排版需要）。解析只有这一份实现：pytest 判据
# 与下面的值对账共用，避免"两处各抄一份正则、其中一份悄悄过期"。
#
# passed/skipped 被逐出计数面是故意的（N-31）：它们 = collected − failed − 本次跳过数，
# 而"本次跳过几支"随外网通道/守护进程状态变。实测同一棵树两次跑出 557/2 与 558/1，
# 于是提交物跟着抖。它们属环境读数，落 dist/VALIDATION_RUN.md。
DOC_COUNT_RE = re.compile(r"collected\s+(\d+)\s*/\s*failed\s+(\d+)")
# 计数面上不许再出现的抖动数（只查承载计数面的那一行，别处的历史记述不动）。
UNSTABLE_ON_FACE_RE = re.compile(r"(?:passed|skipped)\s+\d")


def doc_count_tokens(text: str) -> list[tuple[int, int]]:
    return [tuple(int(x) for x in match) for match in DOC_COUNT_RE.findall(text)]


def face_offenders(text: str) -> list[str]:
    """承载计数面的那一行上的越界写法。纯函数，可喂夹具。"""
    out: list[str] = []
    for line in text.splitlines():
        if not DOC_COUNT_RE.search(line):
            continue
        unstable = UNSTABLE_ON_FACE_RE.findall(line)
        if unstable:
            out.append(
                f"计数面那行写了随环境抖的数（{sorted(set(unstable))}）——"
                "passed/skipped 属本次跑读数，请改到 dist/VALIDATION_RUN.md 一侧"
            )
    return out


# 带编号的登记表行：编号必须**按出现顺序单调递增且唯一**。
# 插入新行时若锚在"上一轮那一行"或表中间，新行会落进倒序位置，而每行内容本身
# 完全正确——只有顺序看得见。本轮真实踩到两次（一条判据行插进了 G0.30 之前、
# 一条交付行插进了 N-12 之前）。
ORDERED_ROWS: tuple[tuple[str, str], ...] = (
    ("docs/CURRENT_STATE.md", r"^\| N-(\d+) \|"),
    ("docs/ACCEPTANCE_GATES.md", r"^\| G0\.(\d+) "),
)


def row_order_offenders(named_texts: list[tuple[str, str, str]]) -> list[str]:
    """(标签, 正文, 行首正则) → 倒序/重号/解析不到行的读数。纯函数，可喂夹具。"""
    out: list[str] = []
    for label, text, pattern in named_texts:
        nums = [int(m.group(1)) for m in re.finditer(pattern, text, re.MULTILINE)]
        if len(nums) < 2:
            out.append(f"{label}: 只解析到 {len(nums)} 行编号（判据会恒真）")
            continue
        bad = [(nums[i - 1], nums[i]) for i in range(1, len(nums)) if nums[i] <= nums[i - 1]]
        if bad:
            out.append(f"{label}: 编号非单调递增，相邻逆序对 {bad}")
        dup = sorted({n for n in nums if nums.count(n) > 1})
        if dup:
            out.append(f"{label}: 编号重复 {dup}")
    return out


def mypy_source_files(summary: str) -> int | None:
    """从 mypy 的收口行取"检查了多少个源文件"。两种形状：成功行与错误行。

    读不到返回 None（调用方按"事实源缺位"处理，而不是悄悄当 0 —— 0 会让
    "文档写 45 / 实测 0"这种比较退化成永远对不上或永远对上）。
    """
    found = re.search(r"no issues found in (\d+) source files", summary)
    if not found:
        found = re.search(r"checked (\d+) source files", summary)
    return int(found.group(1)) if found else None


def alembic_chain_length() -> int:
    """迁移链的文件数（状态页 `**N 文件链**` 那一行的事实源）。"""
    versions = ROOT / "alembic" / "versions"
    return len([p for p in versions.glob("*.py") if p.name not in {"__init__.py", "README"}])


STATE_GATED_ROWS = {
    "Lint / Type": ("mypy", r"mypy\s+(\d+)\s+files", "typecheck_files", "mypy 源文件数"),
    "Migration": ("文件链", r"\*\*(\d+)\s*文件链\*\*", "migration_chain", "迁移链长"),
}
INTEGRATION_ROW_RE = re.compile(r"^\|\s*(Integration[^|]*)\|")
ENV_FRACTION_RE = re.compile(r"\d+\s*/\s*\d+")


def state_row_offenders(text: str, *, typecheck_files: int, migration_chain: int) -> list[str]:
    """状态页 §1：代码决定的数要对上，随环境抖的数不许手抄。纯函数，可喂夹具。

    两类数走两条路，是因为 N-31 已经证明"把环境读数钉进文档"会随机红：
    `Integration Docker 执行了几支` 由守护进程/外网通道决定，而 mypy 检查了几个文件、
    迁移链有几段只由仓库内容决定 —— 前者逐出，后者纳入对账。
    """
    out: list[str] = []
    lines = text.splitlines()
    facts = {"typecheck_files": typecheck_files, "migration_chain": migration_chain}
    seen = 0
    for label, (_needle, pattern, fact, what) in STATE_GATED_ROWS.items():
        rows = [ln for ln in lines if ln.startswith(f"| {label} |")]
        if not rows:
            out.append(f"{label} 行不在状态页里（这条对账失去了覆盖面）")
            continue
        seen += 1
        match = re.search(pattern, rows[0])
        if not match:
            out.append(f"{label} 行读不到 {what}（应当写成 {pattern.replace(chr(92), '')} 的形状）")
        elif int(match.group(1)) != facts[fact]:
            out.append(f"{label}: 状态页写 {what} {match.group(1)}，实测 {facts[fact]}")
    for line in lines:
        row = INTEGRATION_ROW_RE.match(line)
        if row and ENV_FRACTION_RE.search(line):
            out.append(
                f"Integration {row.group(1).strip()} 行手抄了随环境抖的环境读数"
                f"（{ENV_FRACTION_RE.search(line).group(0)}）——各档执行了几支属本次跑读数，"
                "请看 dist/VALIDATION_RUN.md"
            )
    if not seen and not any(INTEGRATION_ROW_RE.match(ln) for ln in lines):
        out.append("状态页里一行被盯的表行都没有：本判据与恒真同形")
    return out


def doc_row_order_discrepancies() -> list[str]:
    named = [
        (rel, (ROOT / rel).read_text(encoding="utf-8"), pat) for rel, pat in ORDERED_ROWS
    ]
    return row_order_offenders(named)


def changelog_section(text: str, version: str) -> str:
    """CHANGELOG 里属于某个版本的那一节（到下一个 `## ` 为止）。"""
    head = f"## {version}"
    start = text.find(head)
    if start < 0:
        return ""
    rest = text[start + len(head) :]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def docs_counts_discrepancies(version: str, checks: dict) -> list[str]:
    """CHANGELOG 当前版本节 / CURRENT_STATE 里的计数面必须等于本次实测的可复现数。

    值对账只能发生在这里（报告刚算出来），不能放进 pytest 用例：pytest 看到的是
    上一次运行的报告，新加一条用例就会造出不收敛的自引用（本轮真实踩过）。
    """
    run = checks["test_run"]
    expected = (int(checks["test_collected"]["count"]), int(run["failed"]))
    sources = {
        "CHANGELOG 当前版本节": changelog_section(
            (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version
        ),
        "docs/CURRENT_STATE.md": (ROOT / "docs" / "CURRENT_STATE.md").read_text(encoding="utf-8"),
    }
    out: list[str] = []
    for name, text in sources.items():
        out.extend(f"{name}: {o}" for o in face_offenders(text))
        found = doc_count_tokens(text)
        if len(found) != 1:
            out.append(f"{name}: 计数面 {len(found)} 处（要求恰好 1 处；多处必有一处会过期）")
        elif found[0] != expected:
            out.append(f"{name}: 文档写 {found[0]}，实测 {expected}")
    return out


def _render_md(report: dict, title_suffix: str = "") -> str:
    """渲染一份报告。同一把尺子渲两份：提交面（只有可复现项）与本次跑读数（全量）。

    按 checks 里**实际存在的键**派生行，不写死清单 —— 写死会让"新增一档"和
    "某档被剥出提交面"两种改动在 .md 上看不见。
    """
    lines = [
        f"# VALIDATION — EmbodiedCloud{title_suffix}",
        "",
        f"> 版本：{report['version']}（自动生成，勿手改）",
        "",
        f"## 总览：**{report['overall']}**",
        "",
        "| Gate | 状态 | 明细 |",
        "|---|---|---|",
    ]
    c = report["checks"]
    lines.append(f"| Test collected | {c['test_collected']['status']} | {c['test_collected'].get('count', '')} |")
    tr = c["test_run"]
    detail = f"failed={tr['failed']}"
    for field in ("passed", "skipped"):
        if field in tr:
            detail += f" {field}={tr[field]}"
    lines.append(f"| Test run | {tr['status']} | {detail} |")
    if names := tr.get("failed_names", []):
        # 失败必须可归因：一份只写 "failed: 1" 的报告等于没有报告。
        lines[-1] += f" {' · '.join(f'`{n}`' for n in names)} |"
    elif tr["failed"]:
        lines[-1] += " （JUnit 未给出用例名） |"
    if "skipped_names" in tr:
        # skip 按用例名留痕：总数只能说明"抖了几支"，名字才能说明"抖的是哪一支"。
        lines.append(f"| Test skipped | — | {' · '.join(f'`{n}`' for n in tr['skipped_names']) or '（无）'} |")
    for key in ("lint", "typecheck", "migration", "build"):
        if key in c:
            lines.append(f"| {key} | {c[key]['status']} | {c[key].get('note', '')} |")
    for key in sorted(k[len("integration_"):] for k in c if k.startswith("integration_")):
        item = c[f"integration_{key}"]
        lines.append(f"| Integration {key} | {item['status']} | {item.get('note', '')} |")
    for key in (
        "unexpected_skips", "docs_test_counts", "docs_row_order",
        "report_split", "docs_state_rows", "pending_reasons", "reason_references",
    ):
        if key in c:
            lines.append(f"| {key} | {c[key]['status']} | {c[key].get('note', '')} |")
    for key in ("gpu", "streaming", "robot"):
        item = c[f"physical_{key}"]
        lines.append(f"| Physical {key} | {item['status']} | {item.get('note', '')} |")
    lines.append("")
    lines.append(
        "> 由 `python scripts/validate_release.py` 生成。提交面只放**换机器重跑逐字节相同**的"
        "门禁；各集成档状态与本次跳过哪几支属环境读数，见 `dist/VALIDATION_RUN.md`（gitignored）。"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
