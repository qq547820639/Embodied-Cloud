"""镜像构建配方里的"每个外部下载都要校验"（SUPPLY_CHAIN §2/§3/§7）。

判据来自文件本身，不来自手抄清单：把 `runtime/Dockerfile*` 的续行折成 RUN 块，
逐个块检查——

- 含下载（`curl -o` / `wget`）的块必须同块内出现 `sha256sum -c`；
- 含 `git clone --branch <ref>` 的块必须把解析到的 HEAD 与钉住的 commit 比一次。

为什么比 commit 而不是只信 tag：tag 可移动（annotated/lightweight 都能被 force
push），`--branch` 不保证两次构建拿到同一份代码。

先写判据再看结果：改钉之前这两条都应当**开火**（实测读数记在
docs/CURRENT_STATE.md 的 supply-chain 行）。
"""

import re
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"

DOWNLOAD_RE = re.compile(r"\b(?:curl\b[^|;&]*?-o\b|wget\b)")
SHA_CHECK_RE = re.compile(r"sha256sum\s+-c")
CLONE_RE = re.compile(r"git\s+clone\b[^\n]*--branch")
HEAD_PIN_RE = re.compile(r"rev-parse\s+HEAD.*=|\$\(\s*git\s+rev-parse\s+HEAD\s*\)\s*=?\"?")


def _run_blocks(text: str) -> list[list[str]]:
    r"""把 `\` 续行折叠成逻辑块，返回每个 `RUN` 的物理行列表。"""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if current is None:
            if stripped.startswith("RUN "):
                current = [line]
        else:
            current.append(line)
        if current is not None and not stripped.endswith("\\"):
            blocks.append(current)
            current = None
    if current is not None:
        blocks.append(current)
    return blocks


def _dockerfiles() -> list[Path]:
    return sorted(RUNTIME.glob("Dockerfile*"))


def test_dockerfiles_are_scanned() -> None:
    """判据作用域非空：一个都没扫到就等于恒真。"""
    files = _dockerfiles()
    assert files, f"{RUNTIME} 下没有 Dockerfile"
    blocks = [b for path in files for b in _run_blocks(path.read_text(encoding="utf-8"))]
    download_blocks = [b for b in blocks if DOWNLOAD_RE.search("\n".join(b))]
    clone_blocks = [b for b in blocks if CLONE_RE.search("\n".join(b))]
    assert download_blocks, "没解析到任何下载步骤——判据会恒真"
    assert clone_blocks, "没解析到任何 git clone --branch——判据会恒真"


def test_every_download_step_verifies_its_checksum() -> None:
    offenders: list[str] = []
    for path in _dockerfiles():
        for block in _run_blocks(path.read_text(encoding="utf-8")):
            body = "\n".join(block)
            if DOWNLOAD_RE.search(body) and not SHA_CHECK_RE.search(body):
                offenders.append(f"{path.name}:{block[0].strip()[:80]}")
    assert not offenders, "以下下载步骤没有 sha256 校验：" + " | ".join(offenders)


def test_every_branch_clone_is_pinned_to_a_commit() -> None:
    offenders: list[str] = []
    for path in _dockerfiles():
        for block in _run_blocks(path.read_text(encoding="utf-8")):
            body = "\n".join(block)
            if CLONE_RE.search(body) and not HEAD_PIN_RE.search(body):
                offenders.append(f"{path.name}:{block[0].strip()[:80]}")
    assert not offenders, "以下 clone --branch 未把 HEAD 与钉住的 commit 比对：" + " | ".join(offenders)
