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

import json
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
#
# 现在这张表是**空的**：两个外部基础镜像（isaac-sim 与 python:3.12-slim）都已钉上多架构
# 索引 digest。空表本身不能让对账判据失效——它由下面那支注入式夹具常驻钉住
# （`test_exception_reconciliation_fires_in_both_directions`：漏登记、死登记各开一次火，
# 合规侧不开火）。把判据从"表里恰好有一项"改成"两侧集合必须相等 + 判据可被注入证伪"，
# 是因为表格清空后，`assert unpinned == set(UNPINNED_EXCEPTIONS)` 会与恒真同形。
EVIDENCE_GRADES = {"authoritative-reading-not-obtained", "third-party-reading-only", "accepted-risk"}
UNPINNED_EXCEPTIONS: dict[str, dict[str, str]] = {}


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


def _exception_entry_offenders(ref: str, entry: dict[str, str], doc: str) -> list[str]:
    """一条例外登记项自己合不合规：等级在词表内、理由够长、且与本文逐字对得上。"""
    offenders: list[str] = []
    if entry.get("grade") not in EVIDENCE_GRADES:
        offenders.append(f"{ref} 的证据等级 {entry.get('grade')!r} 不在词表内")
    if len(entry.get("reason", "")) < 40:
        offenders.append(f"{ref} 的例外理由过短，不足以支撑免检")
    if ref not in doc:
        offenders.append(f"{ref} 未登记在 docs/SUPPLY_CHAIN.md")
    return offenders


def _exception_table_offenders(unpinned: set[str], registered: set[str]) -> list[str]:
    """双向对账的纯函数：多登记与漏登记分别点名，两侧都空时不开火。

    抽成纯函数是为了能注入两个方向的夹具——真实树现在没有任何未钉镜像，
    若判据只写成 `unpinned == set(UNPINNED_EXCEPTIONS)`，清空后的表会让它恒真。
    """
    offenders = [f"死登记（已钉上或已删，却还占着免检名额）：{ref}" for ref in sorted(registered - unpinned)]
    offenders += [
        f"漏登记（新引入的裸 tag，既没钉 digest 也没有分级理由）：{ref}" for ref in sorted(unpinned - registered)
    ]
    return offenders


def test_base_image_criterion_has_scope() -> None:
    """作用域与"能不能匹配"是两件事：这里只断言作用域非空 + 配方侧已到全钉状态。"""
    dockerfiles = _dockerfiles()
    refs = [r for _, r in _base_refs(dockerfiles)]
    assert refs, "runtime/Dockerfile* 里一个 FROM 都没解析到——钉 digest 的判据会恒真"
    assert any(_is_pinned(r) for r in refs), "没有任何已钉 digest 的基础镜像——逐字相等判据会恒真"
    # 主判据：真实树里**不该再有**未钉的外部基础镜像。例外判据的可证伪性由
    # test_exception_reconciliation_fires_in_both_directions 的注入夹具常驻承担。
    unpinned = _unpinned_external(dockerfiles)
    assert not unpinned, (
        f"这些外部基础镜像没钉 digest：{sorted(unpinned)}；"
        "确实拿不到权威读数时，必须按固定词表定级后登记进 UNPINNED_EXCEPTIONS 并写进 SUPPLY_CHAIN §2"
    )


def test_external_base_images_are_digest_pinned() -> None:
    doc = (REPO_ROOT / "docs" / "SUPPLY_CHAIN.md").read_text(encoding="utf-8")
    offenders = [
        o for ref, entry in UNPINNED_EXCEPTIONS.items() for o in _exception_entry_offenders(ref, entry, doc)
    ]
    # 已钉的那份 digest 也必须出现在文档里：文档只写 tag 就等于把移动的东西当成钉死的
    for pinned in {r for group in _pinned_authorities(_dockerfiles()).values() for r in group}:
        if pinned not in doc:
            offenders.append(f"{pinned} 已钉进 Dockerfile，但 docs/SUPPLY_CHAIN.md 未逐字记录")
    offenders += _exception_table_offenders(_unpinned_external(_dockerfiles()), set(UNPINNED_EXCEPTIONS))
    assert not offenders, "供应链配方层不合规：" + " | ".join(offenders)


def test_exception_entry_criteria_fire_on_injected_entries() -> None:
    """登记表为空时，"每条登记项都要定级/给理由/进文档"这三把判据也无事可做——注入证伪。"""
    doc = "…只有 example.registry/team/app:1.0.0 出现在这里…"
    good = {"grade": "authoritative-reading-not-obtained", "reason": "x" * 40}
    assert _exception_entry_offenders("example.registry/team/app:1.0.0", good, doc) == []
    bad_grade = _exception_entry_offenders(
        "example.registry/team/app:1.0.0", {"grade": "trust-me", "reason": "x" * 40}, doc
    )
    assert len(bad_grade) == 1 and "不在词表内" in bad_grade[0], bad_grade
    short = _exception_entry_offenders(
        "example.registry/team/app:1.0.0", {"grade": "accepted-risk", "reason": "先放着"}, doc
    )
    assert len(short) == 1 and "过短" in short[0], short
    undocumented = _exception_entry_offenders(
        "other.registry/team/app:2.0.0", dict(good), doc
    )
    assert len(undocumented) == 1 and "未登记在" in undocumented[0], undocumented


def test_exception_reconciliation_fires_in_both_directions() -> None:
    """例外判据的两个方向各注入一次，另配两侧都空的合规档——空表不得等于免检通道被打开。

    真实树现在既无未钉镜像也无登记项，`unpinned == registered == set()` 那一行天然成立；
    没有这支注入夹具，"以后有人加了裸 tag 却忘了登记"与"以后有人钉上了却忘了删登记"
    都会静默通过。
    """
    bare = "example.registry/team/app:1.0.0"
    # 漏登记：配方里出现裸 tag，表里没有它
    missing = _exception_table_offenders({bare}, set())
    assert len(missing) == 1 and "漏登记" in missing[0], missing
    # 死登记：表里登记着，但配方里已经没有这个未钉镜像（钉上了或删了）
    dead = _exception_table_offenders(set(), {bare})
    assert len(dead) == 1 and "死登记" in dead[0], dead
    # 两边都有＝已经合规，不该开火（否则双向对账只是恒真）
    assert _exception_table_offenders({bare}, {bare}) == []
    # 真实树的现状：两侧皆空，且判据在这种形状下也不开火
    assert _exception_table_offenders(_unpinned_external(_dockerfiles()), set(UNPINNED_EXCEPTIONS)) == []


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


# ---------------------------------------------------------------------------
# 镜像层 SBOM 步骤（SUPPLY_CHAIN §5 / §8 第 3 条）
# ---------------------------------------------------------------------------

IMAGE_SBOM_SCRIPT = SCRIPTS / "image_sbom.sh"

# 只认"默认值即生效引用"这一条赋值形状（`FOO_IMAGE="${FOO_IMAGE:-<ref>}"`），不扫全文：
# 脚本正文的注释里会提到别的注册表名（三通道实测记录），把它们也当成"消费中的引用"
# 会把判据变成对措辞的红——那是判错对象。
TOOL_IMAGE_ASSIGN_RE = re.compile(r'^\w+_IMAGE="\$\{\w+:-(?P<ref>[^"}]+)\}"$', re.MULTILINE)


def _tool_image_refs(text: str) -> set[str]:
    """这一步真正拉起来用的外部工具镜像引用（自有命名空间不算）。"""
    refs = {m.group("ref") for m in TOOL_IMAGE_ASSIGN_RE.finditer(text)}
    return {r for r in refs if not r.startswith(OWN_NAMESPACE)}


def _tool_image_offenders(refs: set[str], doc: str) -> list[str]:
    """工具镜像的两条主张：钉 digest，且钉的那份逐字进文档。

    为什么这一条比基础镜像更要紧：本机三通道里到得了的注册表是**第三方公开镜像**
    （实测读数记在脚本头注释与 §5），字节不经过我们自己的构建流水线；摘要就是把
    "拿到的东西"和"想要的东西"对上的唯一手段。文档只写 tag 等于把移动的东西当成钉死的。
    """
    offenders: list[str] = []
    for ref in sorted(refs):
        if not _is_pinned(ref):
            offenders.append(f"镜像层 SBOM 步骤的工具镜像 {ref} 没钉 digest")
        elif ref not in doc:
            offenders.append(f"{ref} 已钉进 scripts/image_sbom.sh，但 docs/SUPPLY_CHAIN.md 未逐字记录")
    return offenders


def _load_image_sbom_validator():
    """按仓库既有做法（tests/test_version_consistency.py）从路径加载量具，不复制实现。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_image_sbom", IMAGE_SBOM_SCRIPT.parent / "check_image_sbom.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_image_sbom_step_exists_and_is_pinned() -> None:
    doc = (REPO_ROOT / "docs" / "SUPPLY_CHAIN.md").read_text(encoding="utf-8")
    assert IMAGE_SBOM_SCRIPT.exists(), "scripts/image_sbom.sh 不存在——镜像层 SBOM 步骤消失了"
    refs = _tool_image_refs(IMAGE_SBOM_SCRIPT.read_text(encoding="utf-8"))
    assert refs, "image_sbom.sh 里解析不到任何工具镜像赋值——下面的判据会恒真"
    offenders = _tool_image_offenders(refs, doc)
    assert not offenders, "镜像层 SBOM 步骤的配方层不合规：" + " | ".join(offenders)


def test_tool_image_criterion_fires_in_both_directions() -> None:
    """注入三档：裸 tag、钉了但没进文档、钉了且进了文档——第三档必须不开火。"""
    doc = "…只有 example.registry/team/tool:1.0.0@sha256:" + "ab" * 32 + " 出现在这里…"
    bare = _tool_image_offenders({"example.registry/team/tool:1.0.0"}, doc)
    assert len(bare) == 1 and "没钉 digest" in bare[0], bare
    undocumented = _tool_image_offenders({"example.registry/team/tool:2.0.0@sha256:" + "cd" * 32}, doc)
    assert len(undocumented) == 1 and "未逐字记录" in undocumented[0], undocumented
    assert _tool_image_offenders({"example.registry/team/tool:1.0.0@sha256:" + "ab" * 32}, doc) == []
    # 自有命名空间的赋值不参与这条判据（工具镜像不可能是我们自己的）
    assert _tool_image_refs('IMAGE="${IMAGE:-embodiedcloud/control-plane:0.7.0}"\n') == set()


def test_image_sbom_validator_fires_per_clause() -> None:
    """产出侧判据的每一条都要单独开火，合规夹具整体不开火——与量具自己的 --self-test 同源。"""
    v = _load_image_sbom_validator()
    ref = "embodiedcloud/control-plane:0.7.0"
    good = v._compliant()
    assert v.sbom_offenders(good, ref) == []

    no_pypi = json.loads(json.dumps(good))
    no_pypi["components"] = [c for c in no_pypi["components"] if not str(c.get("purl")).startswith("pkg:pypi/")]
    one = v.sbom_offenders(no_pypi, ref)
    assert len(one) == 1 and "pkg:pypi" in one[0], one

    no_deb = json.loads(json.dumps(good))
    no_deb["components"] = [c for c in no_deb["components"] if not str(c.get("purl")).startswith("pkg:deb/")]
    one = v.sbom_offenders(no_deb, ref)
    assert len(one) == 1 and "pkg:deb" in one[0], one

    floating = json.loads(json.dumps(good))
    floating["metadata"]["component"]["purl"] = "pkg:oci/control-plane?arch=arm64"
    one = v.sbom_offenders(floating, ref)
    assert len(one) == 1 and "sha256" in one[0], one

    # 落盘失败这一类：判据必须报「文件不存在」，而不是抛异常或读成合规
    missing = v.check_file(REPO_ROOT / "definitely-not-here.cdx.json", ref)
    assert len(missing) == 1 and "不存在" in missing[0], missing

