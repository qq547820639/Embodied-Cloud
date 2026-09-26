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
REPO_ROOT = Path(__file__).resolve().parents[1]

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


def _dockerfiles(root: Path = RUNTIME) -> list[Path]:
    return sorted(root.glob("Dockerfile*"))


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


# ---------------------------------------------------------------------------
# 发布脚本的 heredoc：反引号会被 bash 当命令替换执行掉
# ---------------------------------------------------------------------------

UNQUOTED_HEREDOC_RE = re.compile(
    r"<<(?P<delim>(?!')[A-Za-z_]\w*)\n(?P<body>.*?)\n(?P=delim)\n",
    re.DOTALL,
)


def _unquoted_heredoc_bodies(text: str) -> list[str]:
    return [m.group("body") for m in UNQUOTED_HEREDOC_RE.finditer(text)]


def test_release_script_heredocs_carry_no_backticks() -> None:
    """模板文本里一个反引号就能让发布脚本执行任意命令并把输出抄进工件。

    本轮真实踩过：BLOCKED 表里写 `make test-k8s-control-plane`，release.sh 于是
    **又跑了一遍那个档位**，并把 "7 passed, 451 deselected ... in 64.66s" 写进
    dist/VALIDATION_STATUS.md；同一轮的 `nvidia.com/gpu` 则报
    "No such file or directory" 后被替换成空字符串。脚本自身全程退出码 0。
    """
    bodies = _unquoted_heredoc_bodies((REPO_ROOT / "scripts" / "release.sh").read_text(encoding="utf-8"))
    assert bodies, "没解析到未加引号的 heredoc（判据会恒真）"
    offenders = [i for i, body in enumerate(bodies) if "`" in body]
    assert not offenders, f"release.sh 第 {offenders} 个 heredoc 含反引号（会被 bash 执行）"


# ---------------------------------------------------------------------------
# 外部基础镜像必须钉 digest（SUPPLY_CHAIN §2）
# ---------------------------------------------------------------------------

# `FROM [--platform=…] [--as name] <ref>`：先把可选 flag 跳过，否则 --platform 会被当成引用。
FROM_RE = re.compile(r"^[ \t]*FROM(?:\s+--[^\s]+)*\s+(?P<ref>[^\s]+)", re.IGNORECASE | re.MULTILINE)
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
# Docker 参考文法允许的字符集：引号、反斜杠、$、括号、全角括号都不在其中，
# 因此在 shell 脚本里能把一个完整 token 干净切出来（不会把 `${VAR:-` 的收尾符号吞进去，
# 也不会像前缀匹配那样让短 token 吃掉长 token）。
REF_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*\.[A-Za-z][A-Za-z0-9\-]*/[A-Za-z0-9._\-/:@]+")
# 本仓库自有命名空间：由本仓库构建、digest 由构建流水线回填，不归"配方层钉基础镜像"管。
OWN_NAMESPACE = "embodiedcloud/"
SCRIPTS = REPO_ROOT / "scripts"
# 逐字核对的范围除脚本外还包含运维手册：手册里那一行 `docker run` 就是一次真实拉取，
# 它写裸 tag 就等于让运维侧绕过配方钉的那份内容。
SAMENESS_DOCS = (REPO_ROOT / "docs" / "GPU_HOST.md",)

# 未钉 digest 的外部基础镜像 = 例外登记处。**双向核对**：登记表多一项（那项其实已经钉上或
# 已删）与少一项（新引入的裸 tag）都必须红，否则例外会变成永久免检通道。
# grade 只允许固定词表，且必须与 docs/SUPPLY_CHAIN.md 的例外表逐字对得上。
EVIDENCE_GRADES = {"authoritative-reading-not-obtained", "third-party-reading-only", "accepted-risk"}
UNPINNED_EXCEPTIONS: dict[str, dict[str, str]] = {
    "python:3.12-slim": {
        "grade": "authoritative-reading-not-obtained",
        "reason": (
            "Docker Hub 的三个端点本机实测均不可达（auth.docker.io 与 hub.docker.com 都是 "
            "curl 28 超时），拿不到该 tag 当前指向的索引 digest。"
            "public.ecr.aws 的 Docker 官方镜像镜像站给出 sha256:f77ac9e4…"
            "（body 重算 sha256 与该读数一致，OCI index，16 个子清单），但它是第三方镜像而非权威源；"
            "且它 amd64/arm64 子清单的 config digest（9e87977b… / 8630ab77…）都不等于本机缓存那份 "
            "python:3.12-slim 的 config（2f17fc04…）——两个来源互不印证，正好说明该 tag 会移动，"
            "而今天权威侧指向哪个 digest 未证实。钉一个未证实的 digest 会让构建直接失败，故按例外登记。"
        ),
    },
}


def _base_refs(dockerfiles: list[Path]) -> list[tuple[str, str]]:
    """(文件标签, FROM 引用) 列表；`FROM image` 与 `FROM --platform=… image` 都能解析。"""
    out: list[tuple[str, str]] = []
    for path in dockerfiles:
        for m in FROM_RE.finditer(path.read_text(encoding="utf-8")):
            out.append((path.name, m.group("ref")))
    return out


def _is_pinned(ref: str) -> bool:
    return bool(DIGEST_RE.search(ref))


def _repo_of(ref: str) -> str:
    """去掉 @sha256:… 之后的 repo[:tag] 部分。"""
    return ref.split("@", 1)[0]


def _image_path(ref: str) -> str:
    r"""归属键：`[registry[:port]/]repo`，**不含 tag 也不含 digest**。

    键里不能留 tag，否则"脚本把 2.0.0 写成 1.9.9"这种最常见的漂移会因为键不相等而看不见。
    去掉 tag 时只能在**最后一个 `/` 之后**找 `:`：registry 主机可以带端口（`host:5000/repo`），
    那个冒号不属于 tag。
    """
    head, sep, tail = _repo_of(ref).rpartition("/")
    return head + sep + tail.rsplit(":", 1)[0] if ":" in tail else head + sep + tail


def _unpinned_external(dockerfiles: list[Path]) -> set[str]:
    return {
        r
        for _, r in _base_refs(dockerfiles)
        if not _is_pinned(r) and not r.startswith(OWN_NAMESPACE)
    }


def _pinned_authorities(dockerfiles: list[Path]) -> dict[str, set[str]]:
    """{镜像路径 -> 该镜像在配方里被钉死的完整引用集合}。"""
    out: dict[str, set[str]] = {}
    for _, ref in _base_refs(dockerfiles):
        if _is_pinned(ref):
            out.setdefault(_image_path(ref), set()).add(ref)
    return out


def _script_texts(root: Path = SCRIPTS, docs: tuple[Path, ...] = SAMENESS_DOCS) -> list[tuple[Path, str]]:
    """被逐字核对的消费侧：`root/*.sh` 全部 + `docs` 里点名的手册。"""
    out = [(p, p.read_text(encoding="utf-8")) for p in sorted(root.glob("*.sh"))]
    out += [(p, p.read_text(encoding="utf-8")) for p in docs]
    return out


def _sameness_offenders(dockerfiles: list[Path], scripts: list[tuple[Path, str]]) -> list[str]:
    """脚本里引用同一个基础镜像时，必须与 Dockerfile 钉死的那份逐字相等。

    权威侧是 Dockerfile（真正被构建的就是它）。脚本各自带一份"更宽松的默认值"是另一类
    缺陷：G2 冒烟测的镜像与构建用的镜像不是同一个，读数与产物无关。
    """
    authority = _pinned_authorities(dockerfiles)
    offenders: list[str] = []
    for path, text in scripts:
        for token in REF_TOKEN_RE.findall(text):
            allowed = authority.get(_image_path(token))
            if allowed is not None and token not in allowed:
                offenders.append(f"{path.name}: {token} != {sorted(allowed)}")
    return offenders


def test_image_path_key_strips_tag_but_not_registry_port() -> None:
    """归属键是纯文本函数，单独验它能不能匹配——否则"抓不到漂移"其实是键算错。"""
    assert _image_path("nvcr.io/nvidia/isaac-sim:6.0.1@sha256:" + "ab" * 32) == "nvcr.io/nvidia/isaac-sim"
    assert _image_path("example.registry/team/app:1.9.9") == "example.registry/team/app"
    assert _image_path("example.registry/team/app") == "example.registry/team/app"
    # 带端口的 registry：那个冒号不是 tag 分隔符，只在最后一个 / 之后才找冒号
    assert _image_path("localhost:5000/team/app:2.0.0") == "localhost:5000/team/app"
    assert _image_path("localhost:5000/team/app") == "localhost:5000/team/app"


def test_base_image_criterion_has_scope() -> None:
    """三个作用域都必须非空，否则下面的判据全部恒真。"""
    dockerfiles = _dockerfiles()
    refs = [r for _, r in _base_refs(dockerfiles)]
    assert refs, "runtime/Dockerfile* 里一个 FROM 都没解析到——钉 digest 的判据会恒真"
    assert any(_is_pinned(r) for r in refs), "没有任何已钉 digest 的基础镜像——逐字相等判据会恒真"
    assert _unpinned_external(dockerfiles), "没有任何未钉的外部基础镜像——例外登记表的核对会恒真"


def test_external_base_images_are_digest_pinned() -> None:
    doc = (REPO_ROOT / "docs" / "SUPPLY_CHAIN.md").read_text(encoding="utf-8")
    for ref, entry in UNPINNED_EXCEPTIONS.items():
        assert entry["grade"] in EVIDENCE_GRADES, f"{ref} 的证据等级 {entry['grade']!r} 不在词表内"
        assert len(entry["reason"]) >= 40, f"{ref} 的例外理由过短，不足以支撑免检"
        assert ref in doc, f"例外 {ref} 未登记在 docs/SUPPLY_CHAIN.md"
    # 已钉的那份 digest 也必须出现在文档里：文档只写 tag 就等于把移动的东西当成钉死的
    for pinned in {r for group in _pinned_authorities(_dockerfiles()).values() for r in group}:
        assert pinned in doc, f"{pinned} 已钉进 Dockerfile，但 docs/SUPPLY_CHAIN.md 未逐字记录"
    unpinned = _unpinned_external(_dockerfiles())
    assert unpinned == set(UNPINNED_EXCEPTIONS), (
        "未钉 digest 的外部基础镜像与例外登记表不符："
        f"多登记（应删）{sorted(set(UNPINNED_EXCEPTIONS) - unpinned)} / "
        f"漏登记（要么钉要么补理由）{sorted(unpinned - set(UNPINNED_EXCEPTIONS))}"
    )


def test_pinned_base_ref_is_used_verbatim_by_every_consumer() -> None:
    offenders = _sameness_offenders(_dockerfiles(), _script_texts())
    assert not offenders, "以下消费侧引用的镜像与 Dockerfile 钉死的那份不是同一字符串：" + " | ".join(offenders)
    authority = set(_pinned_authorities(_dockerfiles()))
    hit_files = {
        p.name
        for p, text in _script_texts()
        for t in REF_TOKEN_RE.findall(text)
        if _image_path(t) in authority
    }
    # 按"哪些消费侧必须被覆盖"断言，而不是按命中数：数量会随新增消费侧自己涨，
    # 而这个子集检查能在有人改名/删掉引用时立刻暴露判据变空。
    assert {"gpu_acceptance.sh", "isaac_sim_smoke.sh", "release.sh", "GPU_HOST.md"} <= hit_files, (
        f"逐字相等判据没覆盖到全部已知消费侧，实际命中 {sorted(hit_files)}"
    )


def test_digest_criterion_fires_on_an_unpinned_base(tmp_path: Path) -> None:
    """开火／不开火两侧：裸 tag 必须被抓，钉上 digest 与自有命名空间必须放过。"""
    (tmp_path / "Dockerfile.mustfire").write_text(
        "FROM example.registry/team/app:1.0.0\nRUN true\n", encoding="utf-8"
    )
    assert _unpinned_external(_dockerfiles(tmp_path)) == {"example.registry/team/app:1.0.0"}

    (tmp_path / "Dockerfile.mustfire").write_text(
        f"FROM example.registry/team/app:1.0.0@sha256:{'ab' * 32}\nRUN true\n", encoding="utf-8"
    )
    assert _unpinned_external(_dockerfiles(tmp_path)) == set()

    (tmp_path / "Dockerfile.mustfire").write_text(
        "FROM embodiedcloud/control-plane:0.7.0\nRUN true\n", encoding="utf-8"
    )
    assert _unpinned_external(_dockerfiles(tmp_path)) == set()


def test_sameness_criterion_fires_when_a_script_drifts(tmp_path: Path) -> None:
    """只改脚本侧的 tag（digest 留着）也必须红：漂移是引用整体不等，不是缺没缺 digest。"""
    pinned = "example.registry/team/app:2.0.0@sha256:" + "cd" * 32
    (tmp_path / "Dockerfile.probe").write_text(f"FROM {pinned}\n", encoding="utf-8")

    def script(body: str) -> list[tuple[Path, str]]:
        p = tmp_path / "probe.sh"
        p.write_text(body, encoding="utf-8")
        return [(p, body)]

    drifted = script(f'IMAGE="${{IMAGE:-example.registry/team/app:1.9.9@sha256:{"cd" * 32}}}"\n')
    offenders = _sameness_offenders(_dockerfiles(tmp_path), drifted)
    assert len(offenders) == 1, f"漂移未被抓到：{offenders}"

    compliant = script(f'IMAGE="${{IMAGE:-{pinned}}}"\n')
    assert _sameness_offenders(_dockerfiles(tmp_path), compliant) == []

    unrelated = script("docker run --rm other.registry/team/sidecar:1.0.0\n")
    assert _sameness_offenders(_dockerfiles(tmp_path), unrelated) == []
