"""G1–G4 物理验收脚本的退出码与缺件行为，从今天起有常驻读者（N-62）。

为什么需要：`docs/ACCEPTANCE_GATES.md` 的 G1.1/G1.2/G2.1/G3.1/G4.1 全靠 `scripts/` 下这五支
bash 脚本执行，而本机没有 NVIDIA 设备 ⇒ 它们从来不进任何一轮 `make validate`。
改它们的人此前拿不到任何反馈：`make lint` 只做 `bash -n`（语法），全仓也没有一支常驻用例
exec 过它们（只有文档与消费方名单提到名字）。物理门禁那一侧一旦"退 0 但什么都没验"，
发现的人正拿着 GPU 硬件等结果。

先记下本轮实测出来的两件事，因为它们推翻了我动手前的判断：
- 这五支脚本本身写得比预期稳。把 `rc=${PIPESTATUS[0]}` 改成 `rc=$?` **不会**造成假绿
  （顶部 `set -euo pipefail` 还在，`$?` 已经是管道里那个非零）；单独把 `pipefail` 摘掉也
  不会（那一行还在读 `PIPESTATUS[0]`）。要两处一起坏，失败才会被说成 PASS。
  ⇒ 判据因此钉的是"这个组合一旦成立必须立刻红"，不是"某一行必须存在"。
- 缺件档的形状本轮也量出来过：三支 smoke 脚本 `rc=2` 且首行
  `BLOCKED_EXTERNAL_DEPENDENCY: 缺少宿主机前置依赖: …`；`preflight_gpu_host.sh` 与
  `gpu_acceptance.sh` `rc=1` 并逐样点名 `MISSING`。这几行现在是被判据守着的，不再是巧合。

替身一律用**净 PATH**（`stub 目录:/usr/bin:/bin`）：直接沿用宿主 PATH 会得到一种假缺件档——
本机装着真 docker，"不给它建替身"根本不构成"这台宿主没有 docker"。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
GATE_SCRIPTS = (
    "preflight_gpu_host.sh",
    "gpu_acceptance.sh",
    "isaac_sim_smoke.sh",
    "isaac_lab_cartpole_smoke.sh",
    "franka_smoke.sh",
)
SMOKES = ("isaac_sim_smoke.sh", "isaac_lab_cartpole_smoke.sh", "franka_smoke.sh")
# 读容器退出码的那几支（preflight 不跑容器，只看命令在不在）
RC_READING = ("gpu_acceptance.sh", *SMOKES)
BASE_PATH = "/usr/bin:/bin"

_STUB_DOCKER = """#!/bin/sh
# 只覆盖这五支脚本真正用到的子命令；行为由 env 决定。
case "$1" in
  info) exit "${STUB_DOCKER_INFO_RC:-0}" ;;
  run)  printf '%s\\n' "stub docker run"
        echo "stub: simulated GPU render" >&2
        exit "${STUB_DOCKER_RUN_RC:-0}" ;;
  image)
    if [ "${STUB_WORKSPACE_IMAGE:-1}" = "1" ]; then
      printf 'image=sha256:aaaa created=1970 size=1\\n'; exit 0
    fi
    echo "Error: No such image: embodiedcloud/isaaclab-workspace:0.1.0" >&2
    exit 1 ;;
  *) exit 0 ;;
esac
"""
_STUB_NVIDIA_SMI = """#!/bin/sh
printf '0, FAKE GPU, 24000 MiB, 999.99\\n'
exit 0
"""
_STUB_SS = """#!/bin/sh
printf 'LISTEN 0 128 0.0.0.0:18000 0.0.0.0:*\\n'
exit 0
"""
_STUB_LDCONFIG = """#!/bin/sh
printf '\\tlibnvidia-encode.so.1 (libc6,x86-64) => /usr/lib/libnvidia-encode.so.1\\n'
exit 0
"""


def _bin(tag: str, tmp_path: Path, **tools: bool) -> Path:
    bindir = tmp_path / f"bin-{tag}"
    bindir.mkdir(parents=True, exist_ok=True)
    for name, text in (
        ("docker", _STUB_DOCKER), ("nvidia-smi", _STUB_NVIDIA_SMI),
        ("ss", _STUB_SS), ("ldconfig", _STUB_LDCONFIG),
    ):
        key = {"docker": "docker", "nvidia-smi": "nvidia", "ss": "ss", "ldconfig": "ldconfig"}[name]
        if tools.get(key):
            (bindir / name).write_text(text, encoding="utf-8")
            (bindir / name).chmod(0o755)
    return bindir


def _run(
    script: Path, bindir: Path, tmp_path: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full = {
        "PATH": f"{bindir}{os.pathsep}{BASE_PATH}",
        "HOME": str(tmp_path),
        "LOG_DIR": str(tmp_path / "logs"),
        "SMOKE_WORKDIR": str(tmp_path / "work"),
    }
    full.update(env or {})
    return subprocess.run(  # noqa: S603 受控常量：本机 bash + 仓内脚本或 tmp 副本
        ["/bin/bash", str(script)], cwd=ROOT, env=full, text=True, capture_output=True, timeout=300,
    )


def _gate(name: str) -> Path:
    return SCRIPTS / name


# 每支脚本"通过"长什么样：缺席断言必须按它自己的判决形状写。
# 反面教材就是本轮自己踩的：缺件档里"不得在本机冒充 PASS"这句话本身含 PASS，
# 按"文本里不许出现 PASS"判就把合规的脚本判成红。
SUCCESS_MARKER = {
    "preflight_gpu_host.sh": "Preflight passed.",
    "gpu_acceptance.sh": "preflight passed",
    "isaac_sim_smoke.sh": "G2 PASS",
    "isaac_lab_cartpole_smoke.sh": "G3 PASS",
    "franka_smoke.sh": "G4 PASS",
}


def test_every_gate_script_announces_a_missing_host_and_never_passes(tmp_path: Path) -> None:
    """净 PATH 下没有 docker／nvidia-smi ⇒ 五支脚本都必须非零、点名缺的是哪个工具、不得给出自己的"通过"判决。"""
    bare = _bin("none", tmp_path)
    for name, marker in SUCCESS_MARKER.items():
        proc = _run(_gate(name), bare, tmp_path)
        text = proc.stdout + proc.stderr
        assert proc.returncode in (1, 2), (name, proc.returncode, text[-400:])
        assert "docker" in text, (name, text[-400:])
        assert marker not in text, (name, marker, text[-400:])


def test_preflight_announces_each_missing_tool_and_passes_when_all_are_present(tmp_path: Path) -> None:
    """预检两档：全装 ⇒ rc 0 + "Preflight passed."；缺 nvidia-smi ⇒ rc 1 + 点名它 + "Preflight failed."

    聚合式预检最容易烂成"缺一样也照样说 passed"，所以正反两档都要有。
    """
    good = _bin("all", tmp_path, docker=True, nvidia=True, ss=True, ldconfig=True)
    ok = _run(_gate("preflight_gpu_host.sh"), good, tmp_path)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    for token in ("docker", "nvidia-smi", "docker daemon", "NVENC library", "TCP 18000", "Preflight passed."):
        assert token in ok.stdout, (token, ok.stdout)

    bad = _run(_gate("preflight_gpu_host.sh"), _bin("nognu", tmp_path, docker=True, ss=True, ldconfig=True), tmp_path)
    # 失败总结走 stderr（脚本第 33 行 `>&2`），逐工具的 MISSING 走 stdout：两路都要看
    assert bad.returncode == 1, (bad.returncode, bad.stdout + bad.stderr)
    both = bad.stdout + bad.stderr
    assert "nvidia-smi" in both and "MISSING" in both and "Preflight failed." in both, both


def test_a_failing_container_run_cannot_be_reported_as_passing(tmp_path: Path) -> None:
    """G2/G3/G4：容器退出码必须原样成为脚本退出码，失败时不许出现 PASS，成功时必须有 PASS。"""
    bindir = _bin("ok", tmp_path, docker=True, nvidia=True, ss=True, ldconfig=True)
    for name in SMOKES:
        bad = _run(_gate(name), bindir, tmp_path, {"STUB_DOCKER_RUN_RC": "125"})
        text = bad.stdout + bad.stderr
        assert bad.returncode == 125, (name, bad.returncode, text[-400:])
        assert "FAIL" in text and "PASS" not in text, (name, text[-400:])
        good = _run(_gate(name), bindir, tmp_path, {"STUB_DOCKER_RUN_RC": "0"})
        assert good.returncode == 0, (name, good.stdout + good.stderr)
        assert "PASS" in good.stdout, (name, good.stdout[-400:])


def test_gpu_acceptance_inherits_preflight_the_check_and_the_image_absence(tmp_path: Path) -> None:
    """G1.2 三段串起来：预检、容器内 compatibility_check、工作区镜像在不在，各自都要有正确退码。

    "镜像没构建"必须是 2 加一句能照着做的提示：那是 GPU 主机上的第一次状态，
    报成 1 会让人去查显卡而不是去查构建。
    """
    bindir = _bin("acc", tmp_path, docker=True, nvidia=True, ss=True, ldconfig=True)
    ok = _run(_gate("gpu_acceptance.sh"), bindir, tmp_path)
    assert ok.returncode == 0, (ok.stdout[-600:], ok.stderr[-300:])
    assert "compatibility checker" in ok.stdout and "image=" in ok.stdout, ok.stdout[-600:]

    failing = _run(_gate("gpu_acceptance.sh"), bindir, tmp_path, {"STUB_DOCKER_RUN_RC": "7"})
    assert failing.returncode == 7, (failing.returncode, failing.stdout[-300:], failing.stderr[-300:])

    no_image = _run(_gate("gpu_acceptance.sh"), bindir, tmp_path, {"STUB_WORKSPACE_IMAGE": "0"})
    assert no_image.returncode == 2, (no_image.returncode, no_image.stdout[-400:])
    assert "build_workspace_image.sh" in no_image.stdout + no_image.stderr, no_image.stdout[-400:]

    preflight_broken = _run(
        _gate("gpu_acceptance.sh"), _bin("nopreflight", tmp_path, docker=True, ss=True, ldconfig=True), tmp_path
    )
    assert preflight_broken.returncode == 1, (preflight_broken.returncode, preflight_broken.stdout[-400:])


def _mutant(tmp_path: Path, name: str, edits: tuple[tuple[str, str], ...]) -> Path:
    """在副本上做注入：先证改动真的落地（空改写等于没做对照）。"""
    src = (SCRIPTS / name).read_text(encoding="utf-8")
    out = src
    for old, new in edits:
        assert out.count(old) >= 1, (name, old)
        out = out.replace(old, new)
    assert out != src, f"{name}: 空改写"
    d = tmp_path / "mutants" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(out, encoding="utf-8")
    return d / name


def test_the_one_combination_that_would_hide_a_failure_is_detected(tmp_path: Path) -> None:
    """把"两处一起坏"的假绿造出来，并证明这条判据抓得住它。

    单改一处都不致命（本轮实测），所以判据不能钉单行存在，得钉这个组合的后果：
    管道退出码取成 `tee` 的 0 ⇒ 脚本会打印 "G2 PASS" 并退 0。
    """
    bindir = _bin("mut", tmp_path, docker=True, nvidia=True, ss=True, ldconfig=True)
    fooled = _mutant(
        tmp_path, "isaac_sim_smoke.sh",
        (("set -euo pipefail", "set -eu"), ("rc=${PIPESTATUS[0]}", "rc=$?")),
    )
    got = _run(fooled, bindir, tmp_path, {"STUB_DOCKER_RUN_RC": "125"})
    assert got.returncode == 0 and "PASS" in got.stdout, (
        "注入没有造出假绿 ⇒ 这条判据在守一个不存在的洞，改回去重新找：" + got.stdout[-300:])

    # 只坏一处时脚本仍然正确 ⇒ 判据不能钉"某一行必须存在"，得钉这个组合的后果。
    # 于是形状守卫这样写：读管道退出码的那几个脚本，pipefail 与 PIPESTATUS 至少要有一个在场
    # （两个都在当然更好；两个都没有就意味着 rc 取的是管道的最后一个命令）。
    def keeps_rc_source_consistent(text: str) -> bool:
        return ("pipefail" in text) or ("PIPESTATUS" in text)

    for name in RC_READING:
        assert keeps_rc_source_consistent((SCRIPTS / name).read_text(encoding="utf-8")), name
    assert not keeps_rc_source_consistent(fooled.read_text(encoding="utf-8")), "守卫没抓到注入的那一档"


def test_softening_a_missing_dependency_exit_is_detected(tmp_path: Path) -> None:
    """注入：把缺件档的 `exit 2` 改成 `exit 0` ⇒ 上面那支缺件判据的断言当场不成立。

    这一支存在的意义是防"缺件档其实是假档"：净 PATH 若还留着真 docker，
    原始脚本就不会走 exit 2，下面这条 `== 2` 会先响。
    """
    bare = _bin("bare", tmp_path)
    original = _run(_gate("isaac_sim_smoke.sh"), bare, tmp_path)
    assert original.returncode == 2, (original.returncode, original.stdout[-300:])
    softened = _mutant(tmp_path, "isaac_sim_smoke.sh", (("  exit 2\nfi", "  exit 0\nfi"),))
    got = _run(softened, bare, tmp_path)
    assert got.returncode == 0, (got.returncode, got.stdout[-300:])
    assert got.returncode not in (1, 2), "缺件判据的档位不区分 0 与 2：它恒真"
