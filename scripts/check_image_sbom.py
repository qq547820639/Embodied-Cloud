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

# 被审镜像必须以内容摘要身份出现在这份文档里：只写 tag 的 SBOM 描述的是一个会移动的东西。
IMAGE_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}")


def _components(doc: dict[str, Any]) -> list[dict[str, Any]]:
    comps = doc.get("components")
    return [c for c in comps if isinstance(c, dict)] if isinstance(comps, list) else []


def _purls(doc: dict[str, Any]) -> list[str]:
    return [str(c.get("purl", "")) for c in _components(doc)]


def sbom_offenders(doc: dict[str, Any], image_ref: str) -> list[str]:
    """返回这份 SBOM 不满足的主张列表；空列表＝它配得上"控制面镜像的镜像层清单"这句话。

    逐条都能单独开火：`--self-test` 与常驻用例各注入一处反例，其余保持合规，
    以免某条判据被邻居条款顺手救活（那样它其实是恒真的）。
    """
    offenders: list[str] = []

    if doc.get("bomFormat") != "CycloneDX":
        offenders.append(f"bomFormat={doc.get('bomFormat')!r}，不是 CycloneDX——下游按 CycloneDX 读的判据全部失效")

    raw_metadata = doc.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_subject = metadata.get("component")
    subject: dict[str, Any] = raw_subject if isinstance(raw_subject, dict) else {}
    if subject.get("type") != "container":
        offenders.append(
            f"metadata.component.type={subject.get('type')!r}，这份文档没有把自己绑定到一个被审镜像"
            "（扫文件系统/目录也会产出结构完整的 CycloneDX，但那不是镜像层清单）"
        )
    subject_purl = str(subject.get("purl", ""))
    if not IMAGE_DIGEST_RE.search(subject_purl):
        offenders.append(
            f"metadata.component.purl={subject_purl!r} 里没有 @sha256: 摘要——"
            "只用 tag 命名的 SBOM 会在 tag 移动后描述一份不是构建时那份内容的镜像"
        )
    # 注意这一条的证据强度有限：trivy 把命令行给它的那个引用原样写进 metadata.component.name，
    # 所以它防的是"把别处抄来的 SBOM 当成本次产物"，真正的字节绑定是上面那条摘要判据。
    if image_ref and image_ref not in {str(subject.get("name", "")), subject_purl}:
        offenders.append(f"这份 SBOM 描述的对象不是 {image_ref!r}（自报 name={subject.get('name')!r}）")

    purls = _purls(doc)
    if not purls:
        offenders.append("components 为空——一份什么都不说的清单")
    else:
        if not any(p.startswith("pkg:deb/") for p in purls):
            offenders.append("没有任何 pkg:deb/ 组件：Debian 基础镜像的 OS 包层没被解出来（或扫的不是这个镜像）")
        if not any(p.startswith("pkg:pypi/") for p in purls):
            offenders.append("没有任何 pkg:pypi/ 组件：pip 装进镜像的 wheel 层没被解出来，"
                             "这份清单与 `make sbom` 的 wheel 级 SBOM 没有区别，不配叫镜像层清单")

    vulns = doc.get("vulnerabilities")
    if isinstance(vulns, list) and vulns:
        offenders.append(
            f"文档里带了 {len(vulns)} 条 vulnerabilities：本步骤只出清单"
            "（`--format cyclonedx` 按 trivy 自己的日志会关掉扫描），漏洞结论请另存并按另一条主张命名"
        )

    return offenders


def check_file(path: Path, image_ref: str) -> list[str]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{path} 不存在——上游步骤声称做了却没交出东西（挂载目录不共享时就会这样）"]
    except json.JSONDecodeError as exc:
        return [f"{path} 不是合法 JSON：{exc}"]
    if not isinstance(doc, dict):
        return [f"{path} 的顶层不是对象"]
    return sbom_offenders(doc, image_ref)


# --- 判据自测：每条主张都要能单独开火，合规夹具要整体不开火 ---------------------

def _compliant() -> dict[str, Any]:
    """最小真形状：字段与本机 trivy 0.74.0 的 cyclonedx 输出一一对应。"""
    digest = "ab" * 32
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.7",
        "metadata": {
            "component": {
                "type": "container",
                "name": "embodiedcloud/control-plane:0.7.0",
                "purl": f"pkg:oci/control-plane@sha256:{digest}?arch=arm64",
            }
        },
        "components": [
            {
                "type": "operating-system",
                "name": "debian",
                "version": "13.7",
                "properties": [{"name": "aquasecurity:trivy:Class", "value": "os-pkgs"}],
            },
            {"type": "library", "name": "libc6", "purl": "pkg:deb/debian/libc6@2.41?arch=arm64"},
            {"type": "library", "name": "fastapi", "purl": "pkg:pypi/fastapi@0.115.0"},
        ],
        "dependencies": [],
        "vulnerabilities": [],
    }


def _self_test() -> int:
    ref = "embodiedcloud/control-plane:0.7.0"
    rows: list[tuple[str, dict[str, Any], int]] = [("合规夹具（真形状）", _compliant(), 0)]

    def drop_scheme(doc: dict[str, Any], prefix: str) -> dict[str, Any]:
        comps = [c for c in doc["components"] if not str(c.get("purl", "")).startswith(prefix)]
        new = json.loads(json.dumps(doc))
        new["components"] = comps
        return new

    rows.append(("缺 pkg:pypi（只解出 OS 包）", drop_scheme(_compliant(), "pkg:pypi/"), 1))
    rows.append(("缺 pkg:deb（只解出 wheel）", drop_scheme(_compliant(), "pkg:deb/"), 1))

    no_digest = _compliant()
    no_digest["metadata"]["component"]["purl"] = "pkg:oci/control-plane?arch=arm64"
    rows.append(("自报对象没有 digest", no_digest, 1))

    wrong_type = _compliant()
    wrong_type["metadata"]["component"]["type"] = "file"
    rows.append(("metadata.component.type 不是 container", wrong_type, 1))

    wrong_subject = _compliant()
    wrong_subject["metadata"]["component"]["name"] = "somebody-else/image:9.9.9"
    rows.append(("描述的是别的镜像", wrong_subject, 1))

    empty = _compliant()
    empty["components"] = []
    rows.append(("components 为空", empty, 1))

    not_cdx = _compliant()
    not_cdx["bomFormat"] = "SPDX"
    rows.append(("bomFormat 不是 CycloneDX", not_cdx, 1))

    with_vulns = _compliant()
    with_vulns["vulnerabilities"] = [{"id": "CVE-0000-0000"}]
    rows.append(("清单里混进漏洞结论", with_vulns, 1))

    bad = 0
    for label, doc, expected in rows:
        got = len(sbom_offenders(doc, ref))
        ok = (got > 0) == (expected > 0)
        bad += not ok
        print(f"[check_image_sbom] {'OK ' if ok else 'BAD'} {label}：offenders={got} 期望{'开火' if expected else '不开火'}")

    # 反向对照：读一个不存在的路径必须红，否则"文件没落盘"这一类失败看不见
    missing = check_file(Path("definitely-not-here.json"), ref)
    ok = bool(missing)
    bad += not ok
    print(f"[check_image_sbom] {'OK ' if ok else 'BAD'} 文件缺失必须红：{missing[:1]}")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sbom", nargs="?", type=Path)
    parser.add_argument("image_ref", nargs="?", default="")
    parser.add_argument("--self-test", action="store_true", help="注入反例，验每条判据都能单独开火")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.sbom:
        parser.error("需要 <sbom.json> [image_ref]，或用 --self-test")
        return 2

    offenders = check_file(args.sbom, args.image_ref)
    doc = json.loads(args.sbom.read_text(encoding="utf-8")) if not offenders else {}
    comps = _purls(doc)
    print(
        f"[image-sbom] {args.sbom} components={len(_components(doc))} "
        f"deb={sum(1 for p in comps if p.startswith('pkg:deb/'))} "
        f"pypi={sum(1 for p in comps if p.startswith('pkg:pypi/'))}"
    )
    for line in offenders:
        print(f"  ✗ {line}")
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
