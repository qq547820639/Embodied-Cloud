#!/usr/bin/env python3
"""Release validation：自动运行全部软件 gate 并产出 docs/VALIDATION.json / VALIDATION.md。

Gate：test collected/passed/skipped/failed、lint、type、migration、build、
GPU/K8s/Streaming/Robot 物理状态。CURRENT_STATE 引用本文件输出；CI 检查 freshness。

用法：python scripts/validate_release.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable  # 当前解释器（venv）


def run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    # S603: cmd 由本脚本受控常量构造（[sys.executable, "-m", pytest/ruff/mypy/...]），无用户输入
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)  # noqa: S603
    return result.returncode, (result.stdout + result.stderr).strip()


def count_tests_junit(report_path: Path) -> dict:
    """§16：用 JUnit XML 的 testsuite 属性稳定计数（不解析 pytest 文本输出）。"""
    import xml.etree.ElementTree as ET

    tree = ET.parse(report_path)  # noqa: S314 pytest 生成的本地 JUnit（受控输出，非外部输入）
    root = tree.getroot()
    # pytest 产出 <testsuites><testsuite .../></testsuites>
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return {"collected": 0, "passed": 0, "skipped": 0, "failed": 0}
    return {
        "collected": int(suite.get("tests", 0)),
        "passed": int(suite.get("tests", 0))
        - int(suite.get("failures", 0))
        - int(suite.get("errors", 0))
        - int(suite.get("skipped", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "failed": int(suite.get("failures", 0)) + int(suite.get("errors", 0)),
    }


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
    }


def main() -> int:
    checks: dict[str, dict] = {}
    gates = _integration_gates()
    import tempfile

    # 1) 全量 test run（JUnit 报告 → 稳定计数）
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        code, _ = run([PYTHON, "-m", "pytest", "--junitxml", str(junit)])
        if junit.exists():
            counts = count_tests_junit(junit)
            checks["test_collected"] = {"status": "PASS", "count": counts["collected"]}
            checks["test_run"] = {
                "status": "PASS" if code == 0 else "FAIL",
                "passed": counts["passed"],
                "skipped": counts["skipped"],
                "failed": counts["failed"],
            }
            checks.update({f"integration_{k}": v for k, v in integration_gate_statuses(junit, gates).items()})
        else:
            checks["test_collected"] = {"status": "FAIL", "count": 0}
            checks["test_run"] = {"status": "FAIL", "passed": 0, "skipped": 0, "failed": 0}
            checks.update({
                f"integration_{k}": {"status": "NOT_RUN", "note": "pytest 未产出 JUnit 报告"}
                for k in gates
            })

    # 3) lint / type / migration / build
    code, _ = run([PYTHON, "-m", "ruff", "check", "app", "tests"])
    checks["lint"] = {"status": "PASS" if code == 0 else "FAIL"}

    code, _ = run([PYTHON, "-m", "mypy", "app"])
    checks["typecheck"] = {"status": "PASS" if code == 0 else "FAIL"}

    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{tmp}/validate.db"
        env = dict(os.environ, EMBODIEDCLOUD_DATABASE_URL=db_url)
        result = subprocess.run(  # noqa: S603 受控常量参数（alembic CLI），无用户输入
            [PYTHON, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT, text=True, capture_output=True, env=env, timeout=300,
        )
        checks["migration"] = {"status": "PASS" if result.returncode == 0 else "FAIL"}

    code, _ = run([PYTHON, "-m", "build"])
    checks["build"] = {"status": "PASS" if code == 0 else "FAIL"}

    # 4) 物理 gate：仍缺硬件的档保持 NOT_RUN（不假装 PASS，也不倒推原因）。
    #    K8s/Postgres 不再写死在这里 —— 它们由上面的 integration_* 从 JUnit 现算。
    for name in ("gpu", "streaming", "robot"):
        checks[f"physical_{name}"] = {
            "status": "NOT_RUN",
            "note": "需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行",
        }

    # 5) 汇总
    software_failed = any(
        c["status"] == "FAIL" for c in checks.values()
    )
    overall = "FAIL" if software_failed else "PASS_WITH_PHYSICAL_PENDING"
    # 注意：报告内容必须**确定性**（不含时间戳）——CI freshness 门禁是
    # `make validate && git diff --exit-code docs/VALIDATION.*`，任何每次运行
    # 都变化的内容（如生成时间）都会让门禁必然失败。生成时间不入报告。
    report = {
        "version": _read_version(),
        "overall": overall,
        "checks": checks,
    }

    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "VALIDATION.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (docs / "VALIDATION.md").write_text(_render_md(report))
    print(f"[validate] overall={overall} tests={checks['test_run']}")
    return 0 if not software_failed else 1


def _read_version() -> str:
    init = ROOT / "app" / "__init__.py"
    for line in init.read_text().splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    return "unknown"


def _render_md(report: dict) -> str:
    lines = [
        "# VALIDATION — EmbodiedCloud",
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
    lines.append(
        f"| Test run | {tr['status']} | passed={tr['passed']} skipped={tr['skipped']} failed={tr['failed']} |"
    )
    for key in ("lint", "typecheck", "migration", "build"):
        lines.append(f"| {key} | {c[key]['status']} | |")
    # 集成档按 checks 里实际存在的键派生，避免"新增一档忘了加进渲染表"
    for key in sorted(k[len("integration_"):] for k in c if k.startswith("integration_")):
        item = c[f"integration_{key}"]
        lines.append(f"| Integration {key} | {item['status']} | {item.get('note', '')} |")
    for key in ("gpu", "streaming", "robot"):
        item = c[f"physical_{key}"]
        lines.append(f"| Physical {key} | {item['status']} | {item.get('note', '')} |")
    lines.append("")
    lines.append("> 由 `python scripts/validate_release.py` 生成；CI 校验 freshness（重新生成无 diff）。")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
