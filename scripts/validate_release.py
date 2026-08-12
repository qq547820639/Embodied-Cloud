#!/usr/bin/env python3
"""Release validation：自动运行全部软件 gate 并产出 docs/VALIDATION.json / VALIDATION.md。

Gate：test collected/passed/skipped/failed、lint、type、migration、build、
GPU/K8s/Streaming/Robot 物理状态。CURRENT_STATE 引用本文件输出；CI 检查 freshness。

用法：python scripts/validate_release.py
"""

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable  # 当前解释器（venv）


def run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    return result.returncode, (result.stdout + result.stderr).strip()


def main() -> int:
    checks: dict[str, dict] = {}

    # 1) test collection
    code, out = run([PYTHON, "-m", "pytest", "--collect-only"])
    collected = 0
    for line in out.splitlines():
        if "tests collected" in line:
            collected = int(line.split()[0])
    checks["test_collected"] = {"status": "PASS" if code == 0 else "FAIL", "count": collected}

    # 2) full test run
    code, out = run([PYTHON, "-m", "pytest"])
    passed = skipped = failed = 0
    for line in out.splitlines():
        if "passed" in line:
            for part in line.split(","):
                part = part.strip()
                if part.endswith("passed"):
                    passed = int(part.split()[0])
                elif part.endswith("skipped"):
                    skipped = int(part.split()[0])
                elif part.endswith("failed"):
                    failed = int(part.split()[0])
    checks["test_run"] = {
        "status": "PASS" if code == 0 else "FAIL",
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
    }

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
        result = subprocess.run(
            [PYTHON, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT, text=True, capture_output=True, env=env, timeout=300,
        )
        checks["migration"] = {"status": "PASS" if result.returncode == 0 else "FAIL"}

    code, _ = run([PYTHON, "-m", "build"])
    checks["build"] = {"status": "PASS" if code == 0 else "FAIL"}

    # 4) 物理 gate（无硬件 → PENDING，不假装 PASS）
    physical = {
        "GPU": "GPU_PHYSICAL_VALIDATION_PENDING",
        "K8s": "K8S_PHYSICAL_VALIDATION_PENDING",
        "Streaming": "STREAMING_PHYSICAL_VALIDATION_PENDING",
        "Robot": "ROBOT_PHYSICAL_VALIDATION_PENDING",
    }
    for name, tag in physical.items():
        checks[f"physical_{name.lower()}"] = {"status": tag, "note": "需要真实硬件验证"}

    # 5) 汇总
    all_pass = all(
        c.get("status") in {"PASS", "PHYSICAL_PENDING"} or c["status"].endswith("_PENDING")
        for c in checks.values()
    )
    for c in checks.values():
        if c["status"].endswith("_PENDING"):
            c["status"] = "PHYSICAL_VALIDATION_PENDING"
    software_failed = any(
        c["status"] == "FAIL" for c in checks.values()
    )
    overall = "FAIL" if software_failed else "PASS_WITH_PHYSICAL_PENDING"
    report = {
        "version": _read_version(),
        "generated_at": datetime.now(UTC).isoformat(),
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
        f"> 版本：{report['version']} · 生成：{report['generated_at']}（自动生成，勿手改）",
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
    for key in ("gpu", "k8s", "streaming", "robot"):
        item = c[f"physical_{key}"]
        lines.append(f"| Physical {key} | {item['status']} | {item.get('note', '')} |")
    lines.append("")
    lines.append("> 由 `python scripts/validate_release.py` 生成；CI 校验 freshness（重新生成无 diff）。")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
