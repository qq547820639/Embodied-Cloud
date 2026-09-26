"""镜像层 SBOM（CycloneDX）的形状判据：一份"生成了 SBOM"的读数要能说明它真的说了什么。

为什么需要这一层：`trivy image --format cyclonedx` 退出码 0 并不保证产出了一份说得出事实的清单。
本轮踩到／看到的两类形状——

1. 被挂载进去的目录其实不是共享的（这台机器 colima 的 `/tmp` 就不共享：容器内写成功、
   宿主看不见），于是"产物在不在"这件事根本不该由路径存在性来判；
2. 扫错对象（例如扫了基础镜像而不是构建产物）照样产出一份结构完整的 CycloneDX，
   只是里面没有本项目那两层内容：Debian 的 OS 包与 pip 装进去的 wheel。

所以判据按"这份文档必须说得出哪些事实"写，而不是按"文件存在/JSON 能解析"写。
所有期望形状都来自本机真跑 trivy 的读数（见 docs/SUPPLY_CHAIN.md §5 同一行），
不是照着 CycloneDX 规范想象出来的。

用法：
    python scripts/check_image_sbom.py dist/sbom.image.cdx.json embodiedcloud/control-plane:0.7.0
    python scripts/check_image_sbom.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# 被审镜像必须以内容摘要身份出现在这份文档里，而且摘要要落在 purl 的 `@` 位上：
# 只写 tag 的 SBOM 描述的是一个会移动的东西；而"串里任意位置有 64 位十六进制就算数"的话，
# `pkg:oci/x?tag=1@sha256:<64>` 这种"标签在前、摘要当查询参数"的写法也会蒙混过关。
# 收紧后的形状就是本机真跑 trivy 给出的那个：pkg:oci/<name>@sha256:<64>[?…]。
OCI_SUBJECT_RE = re.compile(r"^pkg:oci/[^@?]+@sha256:[0-9a-f]{64}(\?.*)?$")
SHA256_RE = re.compile(r"sha256:([0-9a-f]{64})")

# 锁文件里每个包都是连续的三行（`[[package]]` / `name` / `version`），按这个结构抽。
# 不用 TOML 解析器：这一份文件同时要在装不到 tomli 的最小环境里能用，而结构本身是 uv 自己写的。
LOCK_PACKAGE_RE = re.compile(
    r'^\[\[package\]\]\nname = "(?P<name>[^"]+)"\nversion = "(?P<version>[^"]+)"',
    re.MULTILINE,
)
# purl 兜底形状：pkg:pypi/<name>@<version>[?…]
PYPI_PURL_RE = re.compile(r"^pkg:pypi/(?P<name>[^@?]+)@(?P<version>[^?;]+)")


def _canonical(name: str) -> str:
    """PEP 503 的归一化（小写 + 连续分隔符收成 `-`），够用来把 SBOM 的名字对上锁文件。"""
    return re.sub(r"[-_.]+", "-", name).lower()


# 基础镜像自带、不由本项目锁文件决定的 wheel。`python:3.12-slim` 的 site-packages 里就有 pip
# （实测镜像层清单里的 `pkg:pypi/pip@25.0.1`，PkgPath 指向 usr/local/lib/python3.12/），
# 而 `grep -c '^name = "pip"$' uv.lock` 是 0——它是配方的**邻居**，不是漂移。
# 这张表要求逐字命中：镜像里多出一个未登记的锁外 wheel 会红，把 pip 删掉也会红（它确实在场）。
BASE_IMAGE_WHEELS = frozenset({"pip"})


def locked_versions(lock_text: str) -> dict[str, set[str]]:
    """`uv.lock` 的 规范名 → 版本集合。

    用集合而不是单值：universal 锁允许同名在不同 marker 下钉不同版本，"命中其中任何一个"
    才是合规，最后一行覆盖前一行会造出假红。实测当前这份锁 70 个 `[[package]]` 对应 70 个
    名字（无重名），取集合不改变今天的判定。
    """
    out: dict[str, set[str]] = {}
    for m in LOCK_PACKAGE_RE.finditer(lock_text):
        out.setdefault(_canonical(m.group("name")), set()).add(m.group("version"))
    return out


def lock_offenders(doc: dict[str, Any], lock_text: str) -> list[str]:
    """镜像里装的每个 wheel 都必须由锁文件那一行决定——这是 N-22 那条缺陷的反面。

    为什么单独立一条：`make verify-lock` 全绿只保证**仓库里的解析**与 pyproject 一致，
    管不到镜像里实际装上了什么。旧配方 `pip install ".[postgres]"` 在构建时现解析，
    于是镜像里 sqlalchemy 2.1.1（锁钉 2.1.0）这种事没人看得见；改配方成 `uv sync --frozen`
    之后，"漂移"从一句担心变成一条会红的判据。判据读的是**产物**（清单里的版本），
    不是措辞——它不看 Dockerfile 写了什么旗标。
    """
    offenders: list[str] = []
    if not lock_text:
        return ["调用方没给出 uv.lock 的内容——镜像 wheel 与锁文件的一致性此刻无人检查"]
    locked = locked_versions(lock_text)
    if not locked:
        return ["从给出的锁文件文本里解析出 0 个包——解析式失效，这条判据会与恒真同形"]

    for comp in _components(doc):
        purl = str(comp.get("purl", ""))
        if not purl.startswith("pkg:pypi/"):
            continue
        name = _canonical(str(comp.get("name", "")))
        version = str(comp.get("version", ""))
        if not name or not version:
            # 名字/版本只在 purl 里也得照样查——否则"字段缺了"会被读成"没有需要核对的东西"。
            m = PYPI_PURL_RE.match(purl)
            if m:
                name = name or _canonical(m.group("name"))
                version = version or m.group("version")
        if name in BASE_IMAGE_WHEELS:
            continue
        if name not in locked:
            offenders.append(
                f"镜像里的 {name}=={version} 不在锁文件里（purl={purl}）——"
                "它不是由 uv.lock 决定的内容，发布物里出现了一个没人描述的包"
            )
        elif version not in locked[name]:
            offenders.append(
                f"镜像里的 {name} 是 {version}，而 uv.lock 钉的是 {sorted(locked[name])}——"
                "构建时对 PyPI 现解析了，锁文件描述不了这份镜像"
            )
    return offenders



def _components(doc: dict[str, Any]) -> list[dict[str, Any]]:
    comps = doc.get("components")
    return [c for c in comps if isinstance(c, dict)] if isinstance(comps, list) else []


def _purls(doc: dict[str, Any]) -> list[str]:
    return [str(c.get("purl", "")) for c in _components(doc)]


def _declared_image_id(subject: dict[str, Any]) -> str:
    """文档自己说它扫的是哪个镜像 ID（trivy 写进 metadata.component.properties）。"""
    for prop in subject.get("properties") or []:
        if isinstance(prop, dict) and prop.get("name") == "aquasecurity:trivy:ImageID":
            return str(prop.get("value", ""))
    return ""


def sbom_offenders(doc: dict[str, Any], image_ref: str, image_id: str = "") -> list[str]:
    """返回这份 SBOM 不满足的主张列表；空列表＝它配得上"控制面镜像的镜像层清单"这句话。

    逐条都能单独开火：`--self-test` 与常驻用例各注入一处反例，其余保持合规，
    以免某条判据被邻居条款顺手救活（那样它其实是恒真的）。

    `image_id` 是"扫错对象"唯一的真防线。名字那条比不出什么：trivy 把命令行给它的引用原样
    写进 `metadata.component.name`，所以拿它对比"我传进去的名字"只是回声（下面那条已注明）。
    而把 `docker image inspect` 的 `.Id` 拿过来一起比，就钉住了"这份清单描述的就是你手上
    那个镜像的字节"。实测（本机构建物）：`.Id`＝trivy 的 ImageID 属性＝purl 里的摘要，三处同值
    `sha256:cd371b31…`；而基础镜像那份清单的摘要是 `f77ac9e4…`，两者一比就分开了。
    """
    offenders: list[str] = []

    if doc.get("bomFormat") != "CycloneDX":
        offenders.append(f"bomFormat={doc.get('bomFormat')!r}，不是 CycloneDX——下游按 CycloneDX 读的判据全部失效")

    raw_metadata = doc.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_subject = metadata.get("component")
    subject: dict[str, Any] = raw_subject if isinstance(raw_subject, dict) else {}
    if not image_ref:
        offenders.append("调用方没给出被审镜像的名字——对象一致性此刻无人检查")
    if subject.get("type") != "container":
        offenders.append(
            f"metadata.component.type={subject.get('type')!r}，这份文档没有把自己绑定到一个被审镜像"
            "（扫文件系统/目录也会产出结构完整的 CycloneDX，但那不是镜像层清单）"
        )
    subject_purl = str(subject.get("purl", ""))
    purl_hit = SHA256_RE.search(subject_purl)
    if not OCI_SUBJECT_RE.match(subject_purl):
        offenders.append(
            f"metadata.component.purl={subject_purl!r} 不是 pkg:oci/<name>@sha256:<64> 的形状——"
            "只用 tag 命名的 SBOM 会在 tag 移动后描述一份不是构建时那份内容的镜像"
        )
    # 这一条防的是"把别处抄来的 SBOM 当成本次产物"，防不了"扫错了对象"（见 docstring）。
    if image_ref and image_ref not in {str(subject.get("name", "")), subject_purl}:
        offenders.append(f"这份 SBOM 描述的对象不是 {image_ref!r}（自报 name={subject.get('name')!r}）")
    if image_id:
        wanted = image_id.removeprefix("sha256:")
        declared = _declared_image_id(subject).removeprefix("sha256:")
        stated = (purl_hit.group(1) if purl_hit else "") or declared
        if stated != wanted:
            offenders.append(
                f"清单自报的镜像字节与被审镜像不符：inspect 给的 Id 是 {wanted[:12]}…，"
                f"文档里是 purl={purl_hit.group(1)[:12] + '…' if purl_hit else '无'}"
                f"／ImageID={declared[:12] + '…' if declared else '无'}——这就是「扫错对象」的形状"
            )

    purls = _purls(doc)
    if not purls:
        offenders.append("components 为空——一份什么都不说的清单")
    else:
        if not any(p.startswith("pkg:deb/") for p in purls):
            offenders.append("没有任何 pkg:deb/ 组件：Debian 基础镜像的 OS 包层没被解出来（或扫的不是这个镜像）")
        if not any(p.startswith("pkg:pypi/") for p in purls):
            offenders.append("没有任何 pkg:pypi/ 组件：构建时装进镜像的 wheel 层没被解出来，"
                             "这份清单与 `make sbom` 的 wheel 级 SBOM 没有区别，不配叫镜像层清单")

    vulns = doc.get("vulnerabilities")
    declared_vulns = (
        len(vulns) if isinstance(vulns, list)
        else len(vulns.get("vulnerabilities") or []) if isinstance(vulns, dict)
        else 0
    )
    if declared_vulns:
        offenders.append(
            f"文档里带了 {declared_vulns} 条 vulnerabilities：本步骤只出清单"
            "（`--format cyclonedx` 按 trivy 自己的日志会关掉扫描），漏洞结论请另存并按另一条主张命名"
        )

    return offenders


def check_file(path: Path, image_ref: str, image_id: str = "", lock_text: str = "") -> list[str]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{path} 不存在——上游步骤声称做了却没交出东西（挂载目录不共享时就会这样）"]
    except json.JSONDecodeError as exc:
        return [f"{path} 不是合法 JSON：{exc}"]
    if not isinstance(doc, dict):
        return [f"{path} 的顶层不是对象"]
    return sbom_offenders(doc, image_ref, image_id) + lock_offenders(doc, lock_text)


# --- 判据自测：每条主张都要能单独开火，合规夹具要整体不开火 ---------------------

def _compliant() -> dict[str, Any]:
    """最小真形状：字段与本机 trivy 0.74.0 的 cyclonedx 输出一一对应（含 ImageID 属性）。"""
    digest = "ab" * 32
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.7",
        "metadata": {
            "component": {
                "type": "container",
                "name": "embodiedcloud/control-plane:0.7.0",
                "purl": f"pkg:oci/control-plane@sha256:{digest}?arch=arm64",
                "properties": [{"name": "aquasecurity:trivy:ImageID", "value": f"sha256:{digest}"}],
            }
        },
        "components": [
            {
                "type": "operating-system",
                "name": "debian",
                "version": "13.7",
                "properties": [{"name": "aquasecurity:trivy:Class", "value": "os-pkgs"}],
            },
            {"type": "library", "name": "libc6", "version": "2.41", "purl": "pkg:deb/debian/libc6@2.41?arch=arm64"},
            {"type": "library", "name": "fastapi", "version": "0.115.0", "purl": "pkg:pypi/fastapi@0.115.0"},
        ],
        "dependencies": [],
        "vulnerabilities": [],
    }


def _self_test() -> int:
    """每条主张一档反例，且**期望的是精确条数**。

    只判"有没有开火"的话，一条注入顺手打中三条判据也算过——那等于允许某条判据躲在邻居后面
    从来没有单独运行过。image_ref/image_id 也各有自己的档位（缺参与时序不符各一档）。
    """
    ref = "embodiedcloud/control-plane:0.7.0"
    good_id = "sha256:" + "ab" * 32

    def drop_scheme(doc: dict[str, Any], prefix: str) -> dict[str, Any]:
        comps = [c for c in doc["components"] if not str(c.get("purl", "")).startswith(prefix)]
        new = json.loads(json.dumps(doc))
        new["components"] = comps
        return new

    def mutate(doc: dict[str, Any], **kv: Any) -> dict[str, Any]:
        new = json.loads(json.dumps(doc))
        new["metadata"]["component"].update(kv)
        return new

    purl_as_query = mutate(_compliant(), purl=f"pkg:oci/control-plane?tag=latest@sha256={'ab' * 32}")
    wrong_id = _compliant()

    rows: list[tuple[str, dict[str, Any], str, str, int]] = [
        ("合规夹具（真形状，含 Id 一致性）", _compliant(), ref, good_id, 0),
        ("缺 pkg:pypi（只解出 OS 包）", drop_scheme(_compliant(), "pkg:pypi/"), ref, good_id, 1),
        ("缺 pkg:deb（只解出 wheel）", drop_scheme(_compliant(), "pkg:deb/"), ref, good_id, 1),
        ("自报对象没有 digest", mutate(_compliant(), purl="pkg:oci/control-plane?arch=arm64"), ref, "", 1),
        ("digest 落在查询参数位而不是 @ 位", purl_as_query, ref, "", 1),
        ("metadata.component.type 不是 container", mutate(_compliant(), type="file"), ref, "", 1),
        ("描述的是别的镜像（名字回声失效）", mutate(_compliant(), name="somebody-else/image:9.9.9"), ref, "", 1),
        ("inspect 的 Id 与清单自报的字节不符", wrong_id, ref, "sha256:" + "cd" * 32, 1),
        ("调用方没给被审镜像的名字", _compliant(), "", good_id, 1),
        ("components 为空", _with_no_components(), ref, good_id, 1),
        ("bomFormat 不是 CycloneDX", _with_bom_format("SPDX"), ref, good_id, 1),
        ("清单里混进漏洞结论（列表形）", _with_vulns([{"id": "CVE-0000-0000"}]), ref, good_id, 1),
        ("清单里混进漏洞结论（对象形）", _with_vulns({"vulnerabilities": [{"id": "CVE-0001-0001"}]}), ref, good_id, 1),
    ]

    bad = 0
    for label, doc, row_ref, row_id, expected in rows:
        got = sbom_offenders(doc, row_ref, row_id)
        ok = len(got) == expected
        bad += not ok
        print(f"[check_image_sbom] {'OK ' if ok else 'BAD'} {label}：offenders={len(got)} 期望 {expected}")
        if not ok and got:
            print(f"    实际开火：{got[0][:120]}")

    # 反向对照：读一个不存在的路径必须红，否则"文件没落盘"这一类失败看不见
    missing = check_file(Path("definitely-not-here.json"), ref)
    ok = len(missing) == 1
    bad += not ok
    print(f"[check_image_sbom] {'OK ' if ok else 'BAD'} 文件缺失必须红：{missing[:1]}")

    # 锁一致性那一组：判据是另一个函数，夹具各自带锁文本（同样要精确条数）
    lock_fastapi = '[[package]]\nname = "fastapi"\nversion = "0.115.0"\n'
    pip_doc = _compliant()
    pip_doc["components"].append({"type": "library", "name": "pip", "version": "25.0.1", "purl": "pkg:pypi/pip@25.0.1"})
    renamed_pip = _compliant()
    pip_like = {"type": "library", "name": "piq", "version": "25.0.1", "purl": "pkg:pypi/piq@25.0.1"}
    renamed_pip["components"].append(pip_like)
    no_field = _compliant()
    no_field["components"].append({"type": "library", "purl": "pkg:pypi/evil@1.2.3"})
    lock_rows: list[tuple[str, dict[str, Any], str, int]] = [
        ("合规：镜像 wheel 与锁逐名逐版本相等", _compliant(), lock_fastapi, 0),
        ("锁里同名钉着别的版本（真漂移的形状）", _compliant(), '[[package]]\nname = "fastapi"\nversion = "2.1.0"\n', 1),
        ("锁里根本没有这个包", _compliant(), '[[package]]\nname = "uvicorn"\nversion = "0.30.0"\n', 1),
        ("没给锁文本＝此刻无人检查", _compliant(), "", 1),
        ("给了锁文本但解析出 0 个包（判据会与恒真同形）", _compliant(), "# 一个包都没有\n", 1),
        ("基础镜像自带的 pip 走白名单，不该红", pip_doc, lock_fastapi, 0),
        ("白名单只认 pip：改个名就必须红（否则白名单是万能豁免）", renamed_pip, lock_fastapi, 1),
        ("version 字段缺失时按 purl 兜底，不许静默跳过", no_field, lock_fastapi, 1),
        ("同名在锁里有多个 marker 版本，命中其一即合规",
         _compliant(), lock_fastapi + '\n[[package]]\nname = "fastapi"\nversion = "0.99.0"\n', 0),
    ]
    for label, doc, lock_text, expected in lock_rows:
        got = lock_offenders(doc, lock_text)
        ok = len(got) == expected
        bad += not ok
        print(f"[check_image_sbom] {'OK ' if ok else 'BAD'} {label}：offenders={len(got)} 期望 {expected}")
        if not ok and got:
            print(f"    实际开火：{got[0][:120]}")
    return 1 if bad else 0


def _with_no_components() -> dict[str, Any]:
    doc = _compliant()
    doc["components"] = []
    return doc


def _with_bom_format(fmt: str) -> dict[str, Any]:
    doc = _compliant()
    doc["bomFormat"] = fmt
    return doc


def _with_vulns(value: Any) -> dict[str, Any]:
    doc = _compliant()
    doc["vulnerabilities"] = value
    return doc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sbom", nargs="?", type=Path)
    parser.add_argument("image_ref", nargs="?", default="")
    parser.add_argument("--image-id", default="", help="docker image inspect 的 .Id，钉住清单描述的就是这个镜像的字节")
    parser.add_argument("--lock", type=Path, default=None, help="uv.lock 路径；缺了它锁一致性那条主张无人检查（会红）")
    parser.add_argument("--self-test", action="store_true", help="注入反例，验每条判据都能单独开火")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.sbom:
        parser.error("需要 <sbom.json> [image_ref]，或用 --self-test")
        return 2

    # 默认值取"空"而不是"帮你猜一个 uv.lock"：判据的调用面要显式，猜路径会让某次调用
    # 悄悄拿别处的锁来对账（`--lock` 参数没转发的缺陷只能靠这种形状才看得见）。
    lock_text = ""
    lock_problem = ""
    if args.lock is not None:
        try:
            lock_text = args.lock.read_text(encoding="utf-8")
        except OSError as exc:
            lock_problem = f"{args.lock} 读不出来：{exc}——锁一致性这条主张此刻没有基准"
    offenders = check_file(args.sbom, args.image_ref, args.image_id, lock_text)
    if lock_problem:
        offenders.append(lock_problem)
    try:
        doc = json.loads(args.sbom.read_text(encoding="utf-8"))
        comps = _purls(doc) if isinstance(doc, dict) else []
        reading = (
            f"components={len(_components(doc))} "
            f"deb={sum(1 for p in comps if p.startswith('pkg:deb/'))} "
            f"pypi={sum(1 for p in comps if p.startswith('pkg:pypi/'))}"
        )
    except (OSError, json.JSONDecodeError):
        reading = "读不出内容"
    if lock_text:
        reading += f" 锁内包名={len(locked_versions(lock_text))}"
    print(f"[image-sbom] {args.sbom} {reading}")
    for line in offenders:
        print(f"  ✗ {line}")
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
