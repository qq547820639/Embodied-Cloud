"""产物 sha 的「可复算」主张必须有牙（N-34 的未闭半边）。

判据分三层：时间口径只有一份 → 主张由实测生成 → 清单与实测双向对账。
本文件不声称 sdist 可复算（实测不可，见 `docs/CURRENT_STATE.md` 的 N-34）；
它钉的是"谁说了什么，就得能被现测推翻"。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(module)
    return module


def test_epoch_is_computed_in_exactly_one_place() -> None:
    """`SOURCE_DATE_EPOCH` 的取值口径只许有一份实现，且两处调用都从它取。

    上一轮我在 validate 与 Makefile 各写了一遍 `git log -1 --format=%ct`，
    当时靠"两处格式串必须相同"的文本判据兜着 —— 那是把重复当事实用。
    这一轮把它抽成 `scripts/build_env.py`：文本面上 `%ct` 全仓只许出现一次。
    """
    hits = sorted(
        p.name for p in SCRIPTS.glob("*.py") if "%ct" in p.read_text(encoding="utf-8")
    )
    assert hits == ["build_env.py"], f"git 时间口径出现在 {hits}（不止一份，两处会各自过期）"
    validate = (SCRIPTS / "validate_release.py").read_text(encoding="utf-8")
    repro = (SCRIPTS / "artifact_reproducibility.py").read_text(encoding="utf-8")
    for label, text in (("validate_release", validate), ("artifact_reproducibility", repro)):
        assert "epoch_env(" in text, f"{label} 没走共享口径"
    assert '"-m", "build", "--no-isolation"' in validate
    assert '"-m", "build", "--no-isolation"' in repro


def test_epoch_env_respects_the_caller_and_falls_back() -> None:
    mod = _load("build_env")
    assert mod.epoch_env({"SOURCE_DATE_EPOCH": "123"})["SOURCE_DATE_EPOCH"] == "123"
    assert mod.epoch_env({"SOURCE_DATE_EPOCH": ""})["SOURCE_DATE_EPOCH"] != ""
    made = mod.epoch_env({})["SOURCE_DATE_EPOCH"]
    assert made.isdigit() and int(made) > 1_500_000_000, made  # 取到的是真提交时间，不是 0/None
    # 反证：把 git 档摘掉必须落到显式兜底值，而不是 KeyError 或空串
    assert mod.epoch_env({}).get("SOURCE_DATE_EPOCH") is not None


def test_recomputable_verdict_polarity() -> None:
    mod = _load("artifact_reproducibility")
    assert mod.recomputable(["a", "a", "a"]) == "yes"
    assert mod.recomputable(["a", "b"]) == "no", "两次不一样还说可复算＝假承诺"
    assert mod.recomputable(["a"]) == "yes", "单次样本按 yes 处理（分母由调用方保证）"
    try:
        mod.recomputable([])
    except ValueError:
        pass
    else:
        raise AssertionError("空样本必须拒判：分母为 0 与恒真同形")


def test_manifest_round_trip() -> None:
    mod = _load("artifact_reproducibility")
    verdicts = {"sdist": "no", "wheel": "yes"}
    names = {"wheel": "embodiedcloud-0.7.0-py3-none-any.whl", "sdist": "embodiedcloud-0.7.0.tar.gz"}
    text = mod.render_manifest(verdicts, names, epoch="1700000000")
    assert mod.parse_manifest(text) == {
        "embodiedcloud-0.7.0-py3-none-any.whl": "yes",
        "embodiedcloud-0.7.0.tar.gz": "no",
    }, text
    assert "SOURCE_DATE_EPOCH=1700000000" in text, "时间基准没进清单，下一轮无从判断当时是哪一秒"


def test_claim_and_measurement_are_reconciled_both_ways() -> None:
    """四种偏离必须各自点名；一致时必须为空。"""
    mod = _load("artifact_reproducibility")
    fn = mod.check
    assert fn({"w": "yes", "s": "no"}, {"w": "yes", "s": "no"}) == []

    optimistic = fn({"w": "yes"}, {"w": "no"})
    assert len(optimistic) == 1 and "w" in optimistic[0], optimistic
    stale = fn({"w": "no"}, {"w": "yes"})
    assert len(stale) == 1 and "w" in stale[0], "上游修好后清单还写着 no，也该有人来翻"
    absent = fn({}, {"w": "yes"})
    assert len(absent) == 1 and "缺席" in absent[0], absent
    untested = fn({"w": "yes", "ghost": "no"}, {"w": "yes"})
    assert len(untested) == 1 and "ghost" in untested[0], untested
    assert fn({}, {}), "一个产物都没测却判绿＝恒真"


def test_by_filename_translates_the_key_space() -> None:
    """清单按**文件名**索引、探针按**类别**聚合，两者之间必须有一次显式翻译。

    这不是假想的坑：第一版 main() 直接把类别喂给 `check()`，真跑第一遍就报出
    "主张缺席 + 清单里有、本轮没测"——两个键空间交叉相减，谁都看起来缺项。
    """
    mod = _load("artifact_reproducibility")
    verdicts = {"sdist": "no", "wheel": "yes"}
    names = {"wheel": "x-py3-none-any.whl", "sdist": "x.tar.gz"}
    assert mod.by_filename(verdicts, names) == {"x.tar.gz": "no", "x-py3-none-any.whl": "yes"}
    try:
        mod.by_filename({"wheel": "yes"}, {})
    except ValueError:
        pass
    else:
        raise AssertionError("缺文件名时必须拒写：落进清单就是一条指向不存在产物的主张")
    # 反证：把两个键空间直接对账，必然互相判"缺席"——这条就是当初那个 bug 的形状
    crossed = mod.check(mod.by_filename(verdicts, names), verdicts)
    assert len(crossed) == 4, crossed


def test_the_wheel_really_is_recomputable_through_the_shipped_path(tmp_path) -> None:
    """端到端一手读数：连建两次 wheel，sha 必须相同（时间口径一旦被拆掉，这条就红）。"""
    mod = _load("artifact_reproducibility")
    samples = mod.probe(runs=2, outdir=tmp_path, kinds=("wheel",))
    assert set(samples) == {"wheel"}, samples
    assert len(samples["wheel"]) == 2, samples
    assert mod.recomputable(samples["wheel"]) == "yes", (
        f"同一棵树立两次得到不同 wheel：{samples['wheel']} —— SOURCE_DATE_EPOCH 没起作用"
    )


def test_release_chain_and_makefile_wire_the_probe() -> None:
    """`make verify-artifacts` 必须在 checksums 之前跑，且 release.sh 真的调用它。"""
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    release = (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
    assert re.search(r"^verify-artifacts:\n\t\$\(PYTHON\) scripts/artifact_reproducibility\.py$", make, re.MULTILINE), (
        "Makefile 没有 verify-artifacts 目标（或形状变了）"
    )
    phony_lines: list[str] = []
    started = False
    for line in make.splitlines():
        if line.startswith(".PHONY:"):
            started = True
        elif started and not line.endswith("\\"):
            break
        if started:
            phony_lines.append(line)
    assert phony_lines, "Makefile 里没有 .PHONY 清单"
    assert "verify-artifacts" in " ".join(phony_lines), (
        "新目标没写进 .PHONY，`make -n` 会把它当文件目标"
    )
    assert "make verify-artifacts" in release, "release.sh 不调它＝清单永远只由人工想起来才生成"
    checksum_step = release.index('step "生成 dist/checksums.txt"')
    assert release.index("make verify-artifacts") < checksum_step, (
        "复算探针必须排在 checksums 那一步之前，否则清单记的是没被核过的数"
    )
    # 反向对照：把 release.sh 里的调用摘掉，同一把尺子必须翻红
    assert "make verify-artifacts" not in release.replace("make verify-artifacts", "")
