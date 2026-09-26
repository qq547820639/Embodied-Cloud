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
import subprocess
import sys
from pathlib import Path
from typing import Any

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
COPY_FROM_RE = re.compile(r"^[ \t]*COPY[ \t]+--from=(?P<ref>[A-Za-z0-9._\-/:@]+)", re.IGNORECASE | re.MULTILINE)
STAGE_AS_RE = re.compile(r"^[ \t]*FROM[ \t]+\S+[ \t]+AS[ \t]+(?P<stage>[A-Za-z0-9._\-]+)", re.IGNORECASE | re.MULTILINE)
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
# 现在这张表是**空的**：三份外部引用（isaac-sim、python:3.12-slim、builder 用的 uv）都钉上了多架构
# 索引 digest。空表本身不能让对账判据失效——它由下面那支注入式夹具常驻钉住
# （`test_exception_reconciliation_fires_in_both_directions`：漏登记、死登记各开一次火，
# 合规侧不开火）。把判据从"表里恰好有一项"改成"两侧集合必须相等 + 判据可被注入证伪"，
# 是因为表格清空后，`assert unpinned == set(UNPINNED_EXCEPTIONS)` 会与恒真同形。
EVIDENCE_GRADES = {"authoritative-reading-not-obtained", "third-party-reading-only", "accepted-risk"}
UNPINNED_EXCEPTIONS: dict[str, dict[str, str]] = {}


def _base_refs(dockerfiles: list[Path]) -> list[tuple[str, str]]:
    """(文件标签, 镜像引用) 列表：`FROM image`、`FROM --platform=… image` 与 `COPY --from=…`。

    `COPY --from=` 必须在扫面上：多阶段配方里 builder 拉的那份工具镜像（uv）就是构建真正
    消费的字节，只在 FROM 行上找的话，"把 uv 换成裸 latest"这种改动能一路绿过所有钉死判据。
    这里**不**再套 shell 侧那套"像不像镜像"的形状启发：Dockerfile 的 `--from=` 只能是
    本文件声明过的阶段名或一个镜像引用，二者按名字分开就够（shell 那边需要形状判据是因为
    挂载路径 `$PWD/.trivy-cache` 与引用同形，这里不存在这个问题）。
    """
    out: list[tuple[str, str]] = []
    for path in dockerfiles:
        text = path.read_text(encoding="utf-8")
        stages = {m.group("stage").lower() for m in STAGE_AS_RE.finditer(text)}
        for m in FROM_RE.finditer(text):
            out.append((path.name, m.group("ref")))
        for m in COPY_FROM_RE.finditer(text):
            ref = m.group("ref")
            if ref.lower() in stages:
                continue
            out.append((path.name, ref))
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
TRIVY_TOOL_REF_SCRIPT = SCRIPTS / "trivy_tool_ref.sh"
# 配方层判据的覆盖面：两份消费方 + 那份唯一的工具引用定义（source 进来的）。
TOOL_SCRIPTS = (
    IMAGE_SBOM_SCRIPT,
    SCRIPTS / "image_cve.sh",
    TRIVY_TOOL_REF_SCRIPT,
)

# 判据看的是**这一步实际拉起来的东西**，不是措辞：
# - 折掉 `\` 续行（脚本里的 `docker run` 就是三行折一句），去掉整行注释
#   （注释里记着三条通道的注册表名，把它们当成"消费中的引用"就变成对措辞的红）；
# - 只看 `docker run` / `docker pull` 这些逻辑行上的镜像来源——字面量，以及这一行引用到的
#   变量在文件里的默认值。只认"变量赋值那一行"是不够的：把变量改名成 `TRIVY_IMG=`、
#   或者直接在这行写 `aquasec/trivy:latest`，都能绕过赋值形状检查而真的把没钉的字节拉下来。
DOCKER_PULL_RE = re.compile(r"\bdocker\s+(?:run|pull)\b")
IMAGE_REF_RE = re.compile(
    r"""(?<![\w/.$-])                                   # 前面不能是词字符/斜杠/点/$（排除路径、挂载与变量展开）
    (?:
        [A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9.-]*/[A-Za-z0-9._\-/:@]+  # host/repo[:tag][@digest]
      | [a-z0-9][a-z0-9._-]{1,62}/[a-z0-9][a-z0-9._-]*(?::[A-Za-z0-9._-]+)?  # 两段名（Docker Hub）
    )""",
    re.VERBOSE,
)
VAR_REF_RE = re.compile(r"\$\{?(\w+)")
# 两种赋值形状各自解析，不用一条带可选前缀的 regex：合成一条时 `${CONTROL_IMAGE:-embodiedcloud/x:1}`
# 的前缀会留在捕获值里（本轮真踩过），于是"自有命名空间"的过滤看不见它，判据在自己的
# 生产脚本上直接误报。
ASSIGN_DEFAULT_RE = re.compile(r'^\s*(\w+)="\$\{[^:}]+:-(?P<ref>[^"}]*)\}"\s*$', re.MULTILINE)
ASSIGN_LITERAL_RE = re.compile(r'^\s*(\w+)="(?P<ref>[^"$]*)"\s*$', re.MULTILINE)
# 首段是仓库里的目录名时它是路径不是镜像（`dist/sbom.image.cdx.json` 之类）。
PATH_FIRST_SEGMENTS = {"scripts", "dist", "docs", "tests", "app", "runtime", "alembic", "edge_agent"}
FILE_SUFFIXES = (".py", ".sh", ".json", ".sock", ".toml", ".log", ".txt", ".tmp")


def _logical_lines(text: str) -> list[str]:
    """折掉行尾的续行反斜杠、丢掉整行注释，返回可以执行的逻辑行。"""
    out: list[str] = []
    buf = ""
    for line in text.splitlines():
        if not buf and not line.lstrip().startswith("#"):
            buf = line.strip()
            if buf.endswith("\\"):
                buf = buf[:-1].strip()
                continue
            out.append(buf)
            buf = ""
        elif buf:
            buf += " " + line.strip()
            if buf.endswith("\\"):
                buf = buf[:-1].strip()
                continue
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _looks_like_image(ref: str) -> bool:
    ref = ref.strip()
    if not ref or "/" not in ref or ref[0] in "./":
        return False
    # 还带 `$` 的是文件系统路径或变量展开（`$PWD/.trivy-cache` 这类缓存目录），
    # 末段以点开头的也是 dot 目录/文件——两者都不是能被"钉 digest"的东西。
    if "$" in ref or ref.rsplit("/", 1)[-1].startswith("."):
        return False
    head = ref.split("/", 1)[0]
    if head in PATH_FIRST_SEGMENTS:
        return False
    # 带点的头是 registry 主机名，必须凑满 host/namespace/repo 三段；两段名的头不许带点，
    # 否则 `.venv/bin/python` 这类宿主路径会被读成"一个镜像引用"。
    if "." in head and ref.count("/") < 2:
        return False
    return not ref.rsplit("/", 1)[-1].split(":", 1)[0].endswith(FILE_SUFFIXES)


def _assigned_defaults(text: str) -> dict[str, str]:
    """`NAME="字面量"` 与 `NAME="${OTHER:-…}"` 两张默认值表（只收看起来像镜像引用的）。"""
    out: dict[str, str] = {}
    for regex in (ASSIGN_LITERAL_RE, ASSIGN_DEFAULT_RE):
        for m in regex.finditer(text):
            ref = m.group("ref").strip()
            if _looks_like_image(ref):
                out[m.group(1)] = ref
    return out


def _tool_image_refs_in(texts: list[str]) -> set[str]:
    """跨文件合并后的"会被真拉起来的外部引用"集合（工具镜像与漏洞库都算）。

    为什么要跨文件：`TRIVY_IMAGE` 与 `TRIVY_DB_REPOSITORY` 现在只写在一处
    （`scripts/trivy_tool_ref.sh`，两个步骤 source 它）。只扫单个文件的话，
    "同一个事实两份默认值各存一份"这类缺陷反而会被判据鼓励。
    """
    defaults: dict[str, str] = {}
    for text in texts:
        defaults.update(_assigned_defaults(text))
    refs: set[str] = set()
    for text in texts:
        for line in _logical_lines(text):
            if not DOCKER_PULL_RE.search(line):
                continue
            for token in IMAGE_REF_RE.findall(line):
                if _looks_like_image(token):
                    refs.add(token.strip())
            for var in VAR_REF_RE.findall(line):
                if var in defaults:
                    refs.add(defaults[var])
    return {r for r in refs if r and not r.startswith(OWN_NAMESPACE)}


def _tool_image_refs(text: str) -> set[str]:
    """单文件版（注入夹具用它；生产判据走 `_tool_image_refs_in`）。"""
    return _tool_image_refs_in([text])


# 漏洞库通道**故意不钉 digest**：钉住就等于每天拿一份过期的库去说"没有漏洞"。
# 免检不能靠沉默——要登记、定级、给理由，并与扫描到的集合双向对账（漏登记／死登记都红），
# 用的是基础镜像例外表同一套词表与形状。
TOOL_UNPINNED_EXCEPTIONS: dict[str, dict[str, str]] = {
    "public.ecr.aws/aquasecurity/trivy-db:2": {
        "grade": "accepted-risk",
        "reason": (
            "漏洞库按设计要每天更新：钉 digest 会让扫描长期停在一份过期库上，"
            "把「库里还没有这条 CVE」读成「镜像没有漏洞」。承担风险的方式是留痕而不是冻结："
            "工具镜像本身仍钉 digest，trivy 关于库下载的日志随报告一起落盘。"
        ),
    }
}


def _tool_exception_offenders(refs: set[str], registered: dict[str, dict[str, str]], doc: str) -> list[str]:
    """例外表自己的主张：等级在词表内、理由够长、引用逐字进文档，且不许留死项。"""
    offenders: list[str] = []
    for ref, entry in registered.items():
        if ref not in refs:
            offenders.append(f"死登记（这一步已经不拉 {ref} 了，却还占着免检名额）")
        if entry.get("grade") not in EVIDENCE_GRADES:
            offenders.append(f"{ref} 的证据等级 {entry.get('grade')!r} 不在词表内")
        if len(entry.get("reason", "")) < 40:
            offenders.append(f"{ref} 的例外理由过短，不足以支撑免检")
        if ref not in doc:
            offenders.append(f"{ref} 免检却没登记在 docs/SUPPLY_CHAIN.md")
    return offenders


def _tool_image_offenders(
    refs: set[str], doc: str, registered: dict[str, dict[str, str]] | None = None
) -> list[str]:
    """工具镜像的两条主张：钉 digest，且钉的那份逐字进文档。

    为什么这一条比基础镜像更要紧：本机三通道里到得了的注册表是**第三方公开镜像**
    （实测读数记在脚本头注释与 §5），字节不经过我们自己的构建流水线；摘要就是把
    "拿到的东西"和"想要的东西"对上的唯一手段。文档只写 tag 等于把移动的东西当成钉死的。
    """
    offenders: list[str] = []
    for ref in sorted(refs):
        if ref in (TOOL_UNPINNED_EXCEPTIONS if registered is None else registered):
            continue
        if not _is_pinned(ref):
            offenders.append(f"这一步要拉起的 {ref} 没钉 digest，也没登记免检例外")
        elif ref not in doc:
            offenders.append(f"{ref} 已被脚本拉起，但 docs/SUPPLY_CHAIN.md 未逐字记录")
    offenders += _tool_exception_offenders(refs, TOOL_UNPINNED_EXCEPTIONS if registered is None else registered, doc)
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
    missing = [str(q) for q in TOOL_SCRIPTS if not q.exists()]
    assert not missing, f"缺了工具引用覆盖的文件：{missing}"
    refs = _tool_image_refs_in([path.read_text(encoding="utf-8") for path in TOOL_SCRIPTS])
    assert refs, "这三份脚本的 docker run/pull 行上解析不到任何外部引用——判据会恒真"
    # 作用域非空之外再钉两点"谁在被扫"：SBOM 侧的工具镜像与 CVE 侧的漏洞库通道。
    # 少任何一个都说明 source/赋值形状被改坏了，判据会悄悄退化成只看一边。
    assert any(r.startswith("public.ecr.aws/aquasecurity/trivy:") and _is_pinned(r) for r in refs), (
        f"钉 digest 的 trivy 工具引用没被扫到，扫描面漏了：{sorted(refs)}"
    )
    assert any(r.endswith("trivy-db:2") for r in refs), f"漏洞库通道没被扫到，免检表就无人对账：{sorted(refs)}"
    offenders = _tool_image_offenders(refs, doc)
    assert not offenders, "镜像清单／漏洞扫描步骤的配方层不合规：" + " | ".join(offenders)


def test_tool_image_criterion_fires_in_both_directions() -> None:
    """每个绕过形状都各注入一次，合规档不开火。

    这些档不是装饰：`_tool_image_refs` 若退化成只扫赋值行，(a)/(b)/(c) 三档就会静默通过，
    而它们正是"脚本真的把一个没钉的镜像拉下来"的三种写法。
    """
    pinned = "example.registry/team/tool:1.0.0@sha256:" + "ab" * 32
    doc = f"…只有 {pinned} 出现在这里…"

    def offenders_of(body: str, registered: dict[str, dict[str, str]] | None = None) -> list[str]:
        return _tool_image_offenders(_tool_image_refs(body), doc, registered or {})

    # (a) 改名后的赋值变量 + 没钉的默认值：赋值形状不是判据，被拉起来的引用才是
    renamed = (
        'TRIVY_IMG="${TRIVY_IMG:-example.registry/team/tool:9.9.9}"\n'
        'docker run --rm "$TRIVY_IMG" image x\n'
    )
    assert len(offenders_of(renamed)) == 1, renamed
    # (b) 直接写在 run 行上的字面量（Docker Hub 两段名，最容易漏的一种形状）
    assert len(offenders_of('docker run --rm aquasec/trivy:latest image x\n')) == 1
    # (c) 反斜杠续行：判据必须折行才看得见第二行上的变量
    folded = ('T_IMAGE="${T_IMAGE:-example.registry/team/tool:0.0.1}"\n'
              'docker run --rm \\\n  -v /var/run/x:/y \\\n  "$T_IMAGE" image x\n')
    assert len(offenders_of(folded)) == 1, folded
    # (d) 钉上了但文档没逐字记（文档只写 tag＝把移动的东西当成钉死的）
    assert len(offenders_of(f'docker run --rm example.registry/team/tool:2.0.0@sha256:{"cd" * 32} image x\n')) == 1
    # 合规档：钉上且进文档，不开火
    assert offenders_of(f'docker run --rm {pinned} image x\n') == []
    # 注释里的注册表名不参与（判措辞会误伤实测记录）
    assert _tool_image_refs(f'# 另一条通道是 aquasec/trivy:latest\ndocker run --rm {pinned} image x\n') == {pinned}
    # 挂载点、宿主路径与变量展开都不能被当成镜像（这是判据的假红侧）
    assert _tool_image_refs('docker run --rm -v "$PWD/.trivy-cache:/root/.cache/" '
                            f'{pinned} image x\n') == {pinned}

    assert _tool_image_refs("docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "
                            f'{pinned} image x > "$OUT.tmp"\n') == {pinned}
    # 免检例外表自己的四档：没登记＝红、登记了等级不在词表＝红、理由过短＝红、
    # 表里留着这一步已经不拉的引用（死登记）＝红；全部合规时不开火。
    unpinned_db = "example.registry/team/db:2"
    assert len(offenders_of(f'docker pull {unpinned_db}\n', {})) == 1
    doc2 = f"…{unpinned_db}…"
    good = {unpinned_db: {"grade": "accepted-risk", "reason": "x" * 40}}
    assert _tool_image_offenders({unpinned_db}, doc2, good) == []
    bad_grade = {unpinned_db: {"grade": "trust-me", "reason": "x" * 40}}
    assert any("不在词表内" in o for o in _tool_image_offenders({unpinned_db}, doc2, bad_grade))
    short = {unpinned_db: {"grade": "accepted-risk", "reason": "先放着"}}
    assert any("过短" in o for o in _tool_image_offenders({unpinned_db}, doc2, short))
    dead = {unpinned_db: {"grade": "accepted-risk", "reason": "x" * 40}}
    assert any("死登记" in o for o in _tool_image_offenders(set(), doc2, dead))
    # 自有命名空间的引用确实被扫到、再被规则排除（不是"根本没匹配"）
    scanned = _tool_image_refs('docker run --rm embodiedcloud/control-plane:0.7.0 image x\n')
    assert scanned == set(), scanned
    assert "embodiedcloud/control-plane:0.7.0" in {
        t for line in _logical_lines('docker run --rm embodiedcloud/control-plane:0.7.0 image x\n')
        for t in IMAGE_REF_RE.findall(line)
    }


def test_image_sbom_validator_fires_per_clause() -> None:
    """产出侧判据的每一条都要单独开火且**只开自己那一枪**——与量具 --self-test 同源。"""
    v = _load_image_sbom_validator()
    ref = "embodiedcloud/control-plane:0.7.0"
    good = v._compliant()
    good_id = "sha256:" + "ab" * 32
    assert v.sbom_offenders(good, ref, good_id) == []

    def subject(**kv: Any) -> dict[str, Any]:
        doc = json.loads(json.dumps(good))
        doc["metadata"]["component"].update(kv)
        return doc

    arms: list[tuple[str, dict[str, Any], str, str, int]] = [
        ("缺 pkg:pypi（只解出 OS 包）", _drop_purl_prefix(good, "pkg:pypi/"), ref, good_id, 1),
        ("缺 pkg:deb（只解出 wheel）", _drop_purl_prefix(good, "pkg:deb/"), ref, good_id, 1),
        ("purl 没有摘要", subject(purl="pkg:oci/control-plane?arch=arm64"), ref, "", 1),
        ("摘要落在查询参数位", subject(purl=f"pkg:oci/control-plane?tag=latest@sha256:{'ab' * 32}"), ref, "", 1),
        ("type 不是 container", subject(type="file"), ref, "", 1),
        ("描述的是别的镜像（名字回声）", subject(name="somebody-else/image:9.9.9"), ref, "", 1),
        ("inspect 的 Id 与清单自报不符", good, ref, "sha256:" + "cd" * 32, 1),
        ("调用方没给被审镜像名字", good, "", good_id, 1),
        ("components 为空", _empty_components(good), ref, good_id, 1),
        ("bomFormat 不是 CycloneDX", _with_bom_format(good, "SPDX"), ref, good_id, 1),
        ("漏洞结论（列表形）", _with_vulnerabilities(good, [{"id": "CVE-0000-0000"}]), ref, good_id, 1),
        ("漏洞结论（对象形）",
         _with_vulnerabilities(good, {"vulnerabilities": [{"id": "CVE-0001-0001"}]}), ref, good_id, 1),
    ]
    for label, doc, arm_ref, arm_id, expected in arms:
        got = v.sbom_offenders(doc, arm_ref, arm_id)
        assert len(got) == expected, f"{label}：实际 {len(got)} 条，期望 {expected} 条 → {got}"

    # 落盘失败这一类：判据必须报「文件不存在」，而不是抛异常或读成合规
    missing = v.check_file(REPO_ROOT / "definitely-not-here.cdx.json", ref)
    assert len(missing) == 1 and "不存在" in missing[0], missing


def _drop_purl_prefix(doc: dict[str, Any], prefix: str) -> dict[str, Any]:
    new = json.loads(json.dumps(doc))
    new["components"] = [c for c in new["components"] if not str(c.get("purl", "")).startswith(prefix)]
    return new


def _empty_components(doc: dict[str, Any]) -> dict[str, Any]:
    new = json.loads(json.dumps(doc))
    new["components"] = []
    return new


def _with_bom_format(doc: dict[str, Any], fmt: str) -> dict[str, Any]:
    new = json.loads(json.dumps(doc))
    new["bomFormat"] = fmt
    return new


def _with_vulnerabilities(doc: dict[str, Any], value: Any) -> dict[str, Any]:
    new = json.loads(json.dumps(doc))
    new["vulnerabilities"] = value
    return new



# ---------------------------------------------------------------------------
# 锁 ↔ 镜像内容一致性（SUPPLY_CHAIN §8 第 6 项 / 登记表 N-22）
# ---------------------------------------------------------------------------

CONTROL_DOCKERFILE = RUNTIME / "Dockerfile.control-plane"
PREFIX_DRIFT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sbom.image.prefix-drift.json"

# 冻结的锁基准，**故意不读真实 uv.lock**：真实锁哪天把 sqlalchemy 升到 2.1.1，
# 这条反证就会悄悄不再开火，而它必须永远能开火。夹具侧的版本也取自同一夜的真读数
# （fastapi 0.141.1 就是当时锁里钉的那份），所以合规侧同样是真的。
FIXED_LOCK = (
    '[[package]]\nname = "sqlalchemy"\nversion = "2.1.0"\n\n'
    '[[package]]\nname = "fastapi"\nversion = "0.141.1"\n'
)

# 构建时现解析的安装行：不带锁基准的 pip 安装。允许两种合规形状——
# 从 requirements 装（`-r`）或哈希校验模式（`--require-hashes`），它们都不"现解析"。
LIVE_RESOLVE_RE = re.compile(
    r"^[ \t]*RUN[^\n]*\bpip install\b(?![^\n]*(?:-r\s|--require-hashes))(?P<line>[^\n]*)",
    re.IGNORECASE | re.MULTILINE,
)
# 合法形状有三种：`uv sync --frozen`／`uv export --frozen`（导出后再装）／`uv pip sync --frozen`。
# 判"有没有 uv 读锁"而不是"是不是那一条子命令"——这条判据要钉的性质是"这一组依赖由 uv.lock
# 决定"，而能读 uv.lock 的只有 uv（`uv pip sync` 的文档把格式枚举成 requirements.txt/pylock.toml
# 等，不含 uv.lock），所以留 uv 是必要的，钉死子命令是多余的。
LOCK_SYNC_RE = re.compile(r"\buv\s+(?:sync|export|pip\s+sync)\b[^\n]*--frozen")


def _dockerfile_logic(text: str) -> str:
    """去掉整行注释、折掉 `\\` 续行之后的配方正文。

    去注释是挣出来的：注释里那句"`uv sync --frozen` 的语义正是反过来"会被判据当成一条安装
    指令——本轮注入的反例亲手抓到过（把 RUN 行里的 `--frozen` 摘掉，注释还在替它背书，读成合规）。
    折续行挣的是**反方向**：`RUN pip install ` 行尾带续行反斜杠、下一行才接 `-r reqs.txt`，折开之后
    不折就会只看见上半句而误红（常驻控制里带这条）。
    """
    kept = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    folded: list[str] = []
    buf = ""
    for ln in kept:
        if ln.rstrip().endswith("\\"):
            buf += ln.rstrip()[:-1] + " "
            continue
        folded.append(buf + ln)
        buf = ""
    if buf:
        folded.append(buf)
    return "\n".join(folded)


def _lock_install_offenders(text: str) -> list[str]:
    """配方层：依赖层的安装必须挂在那把锁上，且不许出现现解析的安装行。

    两条一起才够用——只查"有没有 `uv sync --frozen`"，那么有人再补一条 `RUN pip install X`
    也能绿；只查"有没有裸 pip install"，那么把锁换成别的解析器（poetry install、pdm）
    也会绿。判的是谓词（装的那一组由谁决定），不是工具名字。
    """
    body = _dockerfile_logic(text)
    offenders: list[str] = []
    if not LOCK_SYNC_RE.search(body):
        offenders.append(
            "配方里没有 uv 读锁的安装步骤（`uv sync`/`uv export` 带 `--frozen`）——依赖层不是由 uv.lock 决定的"
        )
    for m in LIVE_RESOLVE_RE.finditer(body):
        offenders.append(f"配方里有一条不带锁基准的现解析安装：RUN{m.group('line')[:70]}")
    return offenders


def test_control_plane_recipe_installs_from_the_lock() -> None:
    """N-22 的配方侧：`pip install ".[postgres]"` 那种"构建时现解析"不许回来。"""
    text = CONTROL_DOCKERFILE.read_text(encoding="utf-8")
    offenders = _lock_install_offenders(text)
    assert not offenders, "控制面配方的依赖层不合规：" + " | ".join(offenders)

    # 反例一：退回旧配方（真实历史形状，不是编的）
    old = text.replace("RUN uv sync", "RUN pip install --no-cache-dir \".[postgres]\"\\nRUN true && uv sync")
    fired = _lock_install_offenders(old)
    assert len(fired) == 1 and "现解析" in fired[0], fired

    # 反例二：把 --frozen 摘掉（同一行、同一个工具，但锁不再是真源）
    unfrozen = text.replace("--frozen ", "")
    fired2 = _lock_install_offenders(unfrozen)
    assert len(fired2) == 1 and "读锁" in fired2[0], fired2

    # 合规变体：哈希校验模式的 pip 安装不该开火（证明它判的是谓词而不是 "pip" 这个词）
    alt = text.replace(
        "RUN uv sync --frozen --no-dev --extra postgres --no-install-project --no-editable",
        'RUN uv export --frozen --no-emit-project -o /tmp/r.txt && pip install --require-hashes -r /tmp/r.txt',
    )
    assert _lock_install_offenders(alt) == [], _lock_install_offenders(alt)
    assert alt != text, "替换没生效，这支合规变体其实是原文件"

    # 折续行那一半的对照（钉的是"不许误红"，与上面"不许漏红"是一对）：
    # 合规形状写在两行上——行尾续行反斜杠、下一行才出现 `-r`。不折行就只能看见上半句，
    # 于是这条判据会把一份合法配方判成违规；那种红不是"抓到问题"，是判据自己坏了。
    wrapped = text.replace(
        "RUN uv sync --frozen --no-dev --extra postgres --no-install-project --no-editable",
        "RUN uv sync --frozen --no-dev --extra postgres --no-install-project --no-editable\n"
        "RUN pip install \\\n    --require-hashes -r /tmp/reqs.txt",
    )
    assert wrapped != text, "替换没落地"
    assert _lock_install_offenders(wrapped) == [], _lock_install_offenders(wrapped)
    # 反向对照：把折叠那一步拿掉（只去注释），同一份文本就必须误红——证明上面那条绿是折叠挣来的
    unfolded = "\n".join(ln for ln in wrapped.splitlines() if not ln.strip().startswith("#"))
    assert re.search(r"^[ \t]*RUN[^\n]*\bpip install\b(?!.*(?:-r\s|--require-hashes))", unfolded, re.I | re.M), (
        "去掉折叠后这条误红没出现，说明上面那条绿不是折叠挣来的（控制失效）"
    )


def test_lock_criterion_fires_on_a_real_drifted_artifact() -> None:
    """判据必须拿**真产物**开火，而不是只在手写夹具里开火。

    那份 JSON 是从改造前真的控制面镜像（`sha256:cd371b31…`，用旧配方构建）上跑真 trivy 得到的
    逐字节选：镜像里的 SQLAlchemy 是 2.1.1，而 uv.lock 钉 2.1.0——N-22 就是这条读数。
    名字那侧还顺手钉了一件事：真产物写的是 `SQLAlchemy`（大写），锁里是 `sqlalchemy`，
    所以按大小写敏感的裸名比对会把它读成"锁里没这个包"（假红）。判据走的是 PEP 503 归一化。
    """
    validator = _load_image_sbom_validator()
    raw = PREFIX_DRIFT_FIXTURE.read_text(encoding="utf-8")
    doc = json.loads(raw)

    offenders = validator.lock_offenders(doc, FIXED_LOCK)
    assert len(offenders) == 1, offenders
    assert "sqlalchemy" in offenders[0] and "2.1.1" in offenders[0] and "2.1.0" in offenders[0], offenders[0]

    # 合规侧：把那一处版本改回锁里钉的那份，同一把尺子必须整体不开火
    assert validator.lock_offenders(json.loads(raw.replace("2.1.1", "2.1.0")), FIXED_LOCK) == []

    # 白名单侧：夹具里的 pip 25.0.1 不在 FIXED_LOCK 里，却不开火——它是基础镜像自带的邻居。
    # 这一条同时钉住"锁文本里少一个包名不会让判据失声"（上面两档都只有 sqlalchemy/fastapi）。
    only_sql = '[[package]]\nname = "sqlalchemy"\nversion = "2.1.0"\n'
    assert [o for o in validator.lock_offenders(doc, only_sql) if "pip" in o] == []

    # 真实锁的作用域：解析式哪天失效（uv 改了锁的书写形状），这里先红而不是让判据变恒真
    real = validator.locked_versions((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert len(real) >= 50, f"从真实 uv.lock 只解析出 {len(real)} 个包名——解析式可能失效了"
    assert real.get("sqlalchemy") == {"2.1.0"}, real.get("sqlalchemy")


def test_image_sbom_step_forwards_the_lock_to_the_criterion() -> None:
    """新参数不转发就是死缝：`--lock` 必须由产出这一步真的传进去。

    判据侧缺省会红（那条自己有档位），但**接线**这一面只有在这里才看得见。形状按文本判，
    不按 AST：被读的是 `image_sbom.sh`（bash，仓里没有它的语法树可用），而"参数没转发"本来就
    落在一条调用行上。因此判据被刻意绑在**同一逻辑行**内——只让续行反斜杠跨过换行，不用 ±N 行
    窗口，也不允许顺着后面某条不相干命令里的 `--lock` 判成绿。两张开火对照把这两条都钉住。
    """
    text = IMAGE_SBOM_SCRIPT.read_text(encoding="utf-8")
    call = re.compile(r"check_image_sbom\.py[^\n]*(?:\\\n[^\n]*)*?--lock\s+uv\.lock")
    assert call.search(text), (
        "image_sbom.sh 没把 uv.lock 交给判据——锁一致性那条主张在生产路径上无人调用"
    )
    # 开火一：摘掉旗标本身。若哪天有人把判据放宽成"看见 check_image_sbom.py 就算"，这里先红。
    assert text.count("--lock uv.lock") == 1, "变异靶点不唯一，下面两张开火对照读数为无效"
    dropped = text.replace("--lock uv.lock", "--lock-removed-by-fixture")
    assert dropped != text and not call.search(dropped), "摘掉 --lock 之后仍判为已转发：恒真断言"
    # 开火二：把旗标从续行上挪成独立一行（真实 shell 语义里那是另一条命令）。
    # 若判据退化成"整份文件里两个 token 都出现过"，这一支会读成绿——它证明的是"同一逻辑行"这半边。
    joined = text.replace(" \\\n  --lock uv.lock", "\n  --lock uv.lock")
    assert joined != text and not call.search(joined), (
        "旗标挪出续行后仍判为已转发：判据没绑在同一逻辑行，会顺着别条命令误判成绿"
    )


def test_copy_from_refs_are_under_the_same_pin_rule(tmp_path: Path) -> None:
    """多阶段配方的工具镜像也在钉 digest 的扫面里，但阶段名不算引用。"""
    pinned = tmp_path / "Dockerfile.pinned"
    pinned.write_text(
        "FROM python:3.12-slim@sha256:" + "ab" * 32 + " AS builder\n"
        "COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:" + "cd" * 32 + " /uv /bin/\n"
        "RUN uv sync --frozen\n"
        "FROM python:3.12-slim@sha256:" + "ab" * 32 + "\n"
        "COPY --from=builder /app/.venv /app/.venv\n",
        encoding="utf-8",
    )
    refs = [r for _, r in _base_refs([pinned])]
    assert len(refs) == 3, refs  # 两个 FROM + 一份外部工具镜像；builder 那条不算
    assert "builder" not in refs and all("astral-sh/uv" not in r or _is_pinned(r) for r in refs), refs

    drifting = tmp_path / "Dockerfile.drift"
    drifting.write_text(
        "FROM python:3.12-slim@sha256:" + "ab" * 32 + " AS builder\n"
        "COPY --from=alpine:3.20 /uv /bin/\n"
        "FROM python:3.12-slim@sha256:" + "ab" * 32 + "\n"
        "COPY --from=builder /app/.venv /app/.venv\n",
        encoding="utf-8",
    )
    unpinned = _unpinned_external([drifting])
    assert unpinned == {"alpine:3.20"}, f"未钉的工具镜像没被抓到：{unpinned}"
    assert _unpinned_external([pinned]) == set(), _unpinned_external([pinned])

    # 真实树此刻的状态：控制面配方里那份 uv 引用必须在册且已钉死
    uv_refs = [r for tag, r in _base_refs([CONTROL_DOCKERFILE]) if "astral-sh/uv" in r]
    assert len(uv_refs) == 1 and _is_pinned(uv_refs[0]), uv_refs
    doc = (REPO_ROOT / "docs" / "SUPPLY_CHAIN.md").read_text(encoding="utf-8")
    assert uv_refs[0] in doc, "配方钉死的 uv 引用没逐字进 SUPPLY_CHAIN.md（文档只写 tag 就等于没钉）"


# ---------------------------------------------------------------------------
# 镜像配方的 extra ⊆ 发布 SBOM 的 extra（SUPPLY_CHAIN §4 / 登记表 N-23 留的那一格）
# ---------------------------------------------------------------------------

MAKEFILE = REPO_ROOT / "Makefile"

# 两种 shell 写法都要认：`--extra postgres`（空格分隔）与 `--extras=s3`（等号）。
# 挡住 `--extra-index-url`／`--all-extras`／`--no-extra` 的是**强制的分隔符**（`=` 或空白，且名字
# 紧跟其后），不是 lookaround——实测把这些 lookaround 逐个删掉，那三类输入的结果都不变，
# 所以这里不留不起作用的装饰。唯一挣得出差别的是前置 `(?<![\w-])`：删了它，`x--extra grpc`
# 与 `---extra grpc` 会被读成"有人声明了一组 grpc"（常驻控制里带这两条反例）。
EXTRA_FLAG_RE = re.compile(
    r"(?<![\w-])--extras?[ \t]*(?:=[ \t]*|[ \t]+)(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
)
# 只取 `sbom:` 目标的配方块，不扫整份 Makefile：别的目标传 `--extra` 是合法的（例如给 dev 档
# 另出一份清单），整文件扫会把它们并进同一份分母，清单侧就永远比配方宽、判据失去方向。
SBOM_RECIPE_RE = re.compile(r"^sbom:[ \t]*\n(?P<recipe>(?:[ \t][^\n]*\n?)*)", re.MULTILINE)


def _extra_names(text: str) -> set[str]:
    """text 里每个 `--extra`/`--extras` 旗标所传的值，去重成集合。"""
    return {m.group("name") for m in EXTRA_FLAG_RE.finditer(text)}


def _sbom_recipe(makefile_text: str) -> str:
    match = SBOM_RECIPE_RE.search(makefile_text)
    return match.group("recipe") if match else ""


def _undeclared_extra_offenders(dockerfile_text: str, makefile_text: str) -> list[str]:
    """镜像真装的每一组 extra 都必须被发布清单声明过；返回违例说明，空表即通过。

    两侧都必须非空：∅ 是任何集合的子集，任一侧塌成空集都会让子集判据静默恒真——而"N-23 修完
    之后又少报一组"恰好就是从空集开始的（`--all-extras` 改名、目标被删，读数和"合规"同形）。
    消息带 `[空集合]`／`[漏声明]` 前缀：两档守卫开火的含义不同，反证必须能指名是哪一档开的火。
    """
    image = _extra_names(_dockerfile_logic(dockerfile_text))
    recipe = _sbom_recipe(makefile_text)
    sbom = _extra_names(recipe)
    offenders: list[str] = []
    if not recipe.strip():
        offenders.append("[空集合] Makefile 里解析不到 `sbom:` 目标的配方——清单侧没有分母")
    if not image:
        offenders.append("[空集合] 控制面配方解析不到任何 `--extra`——子集判据退化成空对空")
    if not sbom:
        offenders.append("[空集合] `make sbom` 解析不到任何 `--extra`——发布清单不再声明任何 extra")
    for name in sorted(image - sbom):
        offenders.append(f"[漏声明] 镜像装了 extra `{name}`，而 `make sbom` 没有声明它")
    return offenders


def test_image_sbom_validator_self_test_runs_in_this_gate() -> None:
    """量具自己那 23 档反证必须**每轮被跑**，而不是只存在于"每条都能开火"这句注释里。

    起因是一处真实的措辞与事实不符：`test_image_sbom_validator_fires_per_clause` 只驱动
    `sbom_offenders`，`lock_offenders`（锁一致性那一族）的档位从来没被任何常驻用例、Makefile
    目标或 CI 步骤调用过——判据哪天退化成恒真，只有人手跑一次 `--self-test` 才知道。
    这一支把脚本自己的自测接进每轮：它无网络、无 docker，纯内存跑，代价是一次子进程。
    """
    # 起子进程跑的这份量具脚本在本仓库内、参数固定为 --self-test（无用户输入），S603 不适用
    out = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS / "check_image_sbom.py"), "--self-test"],
        capture_output=True, text=True, timeout=120,
    )
    rows = [ln for ln in out.stdout.splitlines() if ln.startswith("[check_image_sbom]")]
    fired = sum(1 for ln in rows if "] OK " in ln)
    broken = [ln for ln in rows if "] BAD " in ln]
    assert out.returncode == 0, f"自测退 {out.returncode}：{broken or rows[-3:]}\n{out.stderr[-300:]}"
    # 只判"全 OK"不够：档位数掉到 1 也会全 OK。分母由脚本自己打出来，这里钉一个不低于当前基线的下界。
    assert not broken, broken
    assert fired >= 23, f"自测只跑了 {fired} 档，低于基线 23（有一族判据可能不再被驱动）"


def test_image_recipe_extras_are_declared_in_the_release_sbom() -> None:
    """N-23 留下的那一格：镜像装什么，发布清单就得声明过什么。

    方向是单边子集、权威侧是 `Dockerfile.control-plane`：SBOM 写的是"这个产品可以装哪几组"，
    镜像只是其中一份部署，所以清单比配方宽（`[s3]` 今天就是这样）合法；反过来——配方加了一组
    清单没声明的 extra——就是刚修掉的那个缺陷的形状：交出去的清单少报了一组真进产物的依赖。
    """
    dockerfile = CONTROL_DOCKERFILE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")
    image_extras = _extra_names(_dockerfile_logic(dockerfile))
    sbom_extras = _extra_names(_sbom_recipe(makefile))

    # 两个集合就是判据的分子与分母，钉成实值而不是"非空即可"：哪天这里红了，先回来确认权威侧
    # 还是不是配方（是清单跟着镜像走，还是镜像跟着清单走），想清楚了再改字面量，别只把数 bump 掉。
    assert image_extras == {"postgres"}, image_extras
    assert sbom_extras == {"postgres", "s3"}, sbom_extras

    # 挡住索引地址的是强制分隔符；挡住"前缀粘连的畸形旗标"的是前置 lookaround——两条各测各的，
    # 否则注释里说的"这道闸"其实没人验证过（评审就发现过一条被邻居满足的控制）。
    assert _extra_names("--extra-index-url https://pypi.org/simple --extras=s3") == {"s3"}
    assert _extra_names("x--extra grpc") == set()
    assert _extra_names("---extra grpc") == set()

    assert _undeclared_extra_offenders(dockerfile, makefile) == []

    # 反例一（N-23 的形状）：配方多装一组清单没声明的 extra——只许这一档开火，点的就是 grpc
    widened_image = dockerfile.replace(
        "--extra postgres --no-install-project",
        "--extra postgres --extra grpc --no-install-project",
    )
    assert widened_image != dockerfile, "替换没生效，这支反例其实是原文件"
    fired = _undeclared_extra_offenders(widened_image, makefile)
    assert len(fired) == 1 and fired[0].startswith("[漏声明]") and "grpc" in fired[0], fired

    # 反例二：把 `make sbom` 的两个 extra 摘掉——开的必须是"空集合"那一档。只断言子集会漏掉
    # 真正致命的状态：清单退化成不声明任何 extra 时，光看差集还说得过去，看分母才知道尺子已瞎。
    stripped = makefile.replace("--extra postgres --extra s3 ", "")
    assert stripped != makefile, "替换没生效，这支反例其实是原文件"
    fired2 = _undeclared_extra_offenders(dockerfile, stripped)
    assert any(o.startswith("[空集合]") for o in fired2), fired2
    # 两侧同时为空时只剩空集合守卫：证明它是独立的一道闸，不是子集判据的副产品
    both_empty = _undeclared_extra_offenders(
        dockerfile.replace("--extra postgres --no-install-project", "--no-install-project"),
        stripped,
    )
    assert both_empty and all(o.startswith("[空集合]") for o in both_empty), both_empty

    # 合规变体：清单比配方宽（多声明一组 grpc）不开火——否则这条会被读成"两侧必须相等"，
    # 而真判据要的只是"别少报"，收紧成双向相等会挡住合法的那一半。
    widened_sbom = makefile.replace("--extra postgres --extra s3", "--extra postgres --extra s3 --extra grpc")
    assert widened_sbom != makefile, "替换没生效，这支合规变体其实是原文件"
    assert _undeclared_extra_offenders(dockerfile, widened_sbom) == []
