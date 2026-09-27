"""产物 sha 的「可复算」主张必须有牙（N-34 的未闭半边）。

判据分三层：时间口径只有一份 → 主张由实测生成 → 清单与实测双向对账。
N-60 之后 sdist 的实测结论从"漂"翻成"可复算"：归一那一步在 `scripts/sdist_normalize.py`，
本文件同时钉它两头——归一吃掉了哪些差异、以及不许靠改内容来换确定性。
它钉的还是同一句话："谁说了什么，就得能被现测推翻"。
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


def _mk_tar(entries: list[tuple[str, bytes]], *, mtime: int, mode: int, order_note: str,
            uid: int = 1000, uname: str = "packer", fmt: int | None = None) -> bytes:
    """造一个 tar.gz 的字节，成员头部的可变项按参数给（顺序、mtime、mode、属主都由调用方定）。"""
    import io
    import tarfile
    from gzip import GzipFile

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=fmt or tarfile.PAX_FORMAT) as out:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                info.size = 0
            else:
                info.size = len(payload)
            info.mtime = mtime
            info.mode = mode
            info.uid = uid
            info.gname = info.uname = uname
            info.pax_headers = {"mtime": f"{mtime}.123456"} if fmt is None else {}
            out.addfile(info, io.BytesIO(payload))
    gz = io.BytesIO()
    # 故意把输出路径写进 gzip 头部：这正是本机第一版归一失败的原因（成员全等而 sha 不等）
    with GzipFile(fileobj=gz, mode="wb", filename=order_note, mtime=mtime, compresslevel=9) as fh:
        fh.write(raw.getvalue())
    return gz.getvalue()


def test_normalize_turns_two_variable_builds_into_one_shape() -> None:
    """必开火：两次构建在 mtime／成员顺序／mode／属主／gzip 文件名上都不同，归一后必须逐字节相同。

    这一支钉的是"归一确实吃掉了那些差异"，不是"归一没弄坏东西"（那是旁边那支）。
    """
    sn = _load("sdist_normalize")
    epoch = 1790000000
    a = _mk_tar([("pkg-1.0/b.py", b"B\n"), ("pkg-1.0/a.py", b"A\n"), ("pkg-1.0/", b"")],
                mtime=epoch + 7, mode=0o664, order_note="run1.tar.gz")
    b = _mk_tar([("pkg-1.0/a.py", b"A\n"), ("pkg-1.0/", b""), ("pkg-1.0/b.py", b"B\n")],
                mtime=epoch + 91, mode=0o666, order_note="place/elsewhere/run2.tar.gz",
                uid=501, uname="someone")
    import hashlib
    assert hashlib.sha256(a).hexdigest() != hashlib.sha256(b).hexdigest(), "夹具本身没有差异，这支控制失去意义"
    na, nb = sn.normalize_bytes(a, epoch), sn.normalize_bytes(b, epoch)
    assert na == nb, "归一后仍不同：还有没被钉住的头部字段"
    assert sn.normalize_bytes(na, epoch) == na, "归一不是幂等的"


def test_normalized_sdist_keeps_every_member_byte_for_byte() -> None:
    """不许靠改内容来换确定性：成员清单与每个成员的内容 sha 必须逐个相等。"""
    import hashlib
    import io
    import tarfile

    sn = _load("sdist_normalize")
    src = _mk_tar([("pkg-1.0/PKG-INFO", b"Name: pkg\n"), ("pkg-1.0/mod.py", b"x = 1\n")],
                  mtime=1790000999, mode=0o664, order_note="x.tar.gz")
    out = sn.normalize_bytes(src, 1790000000)

    def index(blob: bytes) -> dict[str, str]:
        out: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            for m in tf.getmembers():
                handle = tf.extractfile(m)
                out[m.name] = hashlib.sha256(handle.read() if handle else b"").hexdigest()
        return out

    before, after = index(src), index(out)
    assert sorted(before) == sorted(after), f"成员清单变了：{sorted(before)} vs {sorted(after)}"
    assert before == after, "有成员内容被改写：确定性不能靠动内容换"


def test_gzip_header_carries_no_filename_and_the_pinned_mtime(tmp_path: Path) -> None:
    """把本机踩过的坑钉成判据：gzip 的 FNAME 位会把**输出路径**写进头部。

    第一版归一后成员全等、两份 .tar.gz 的 sha 仍不同，就差在这一个标志位上。
    """
    import struct

    sn = _load("sdist_normalize")
    src = _mk_tar([("pkg-1.0/a.py", b"A\n")], mtime=1790000555, mode=0o664,
                  order_note="/some/where/out.tar.gz")
    head = sn.normalize_bytes(src, 1790000000)[:10]
    assert head[:2] == b"\x1f\x8b", "不是 gzip"
    assert not head[3] & sn.FNAME_FLAG, f"FLG 里 FNAME 位还置着：{head[3]:#04x}"
    assert struct.unpack("<I", head[4:8])[0] == 1790000000, "gzip mtime 没钉到基准"

    # 另一头：这一位不是想象出来的危险——用**有名字的文件句柄**且不传 filename 时它真的会出现。
    # 没有这一臂，上面那条"没有 FNAME"可能只是恰好走运（本实现写进 BytesIO），
    # 也就说不清 `filename=""` 到底在防什么。
    from gzip import GzipFile

    named = tmp_path / "named-handle.bin"
    with open(named, "wb") as fh, GzipFile(fileobj=fh, mode="wb", mtime=1790000000) as gz:
        gz.write(b"x")
    leaked = named.read_bytes()[3]
    assert leaked & sn.FNAME_FLAG, f"这一臂没复现出 FNAME（FLG={leaked:#04x}）：机制说明作废，删掉它"
    pinned = tmp_path / "pinned.bin"
    with open(pinned, "wb") as fh, GzipFile(fileobj=fh, mode="wb", filename="", mtime=1790000000) as gz:
        gz.write(b"x")
    assert not pinned.read_bytes()[3] & sn.FNAME_FLAG


def test_epoch_is_required_not_assumed() -> None:
    """拿不到 SOURCE_DATE_EPOCH 时必须失败，不许悄悄退回"打包那一刻"（那就等于没归一）。"""
    sn = _load("sdist_normalize")
    try:
        sn.epoch_from_env({})
    except RuntimeError as exc:
        assert "SOURCE_DATE_EPOCH" in str(exc), exc
    else:
        raise AssertionError("空环境竟然取到了时间基准：主张会被写成不可复算的那一种")


def test_the_epoch_and_the_payload_both_have_to_match() -> None:
    """两支"必须翻"的控制：确定性不是白来的，也不是靠无视内容换来的。

    ① 换 epoch ⇒ sha 必须变。它不变就说明归一根本没在钉时间（或整个函数是常量）。
    ② 换一成员的内容 ⇒ sha 必须变。它不变就说明"可复算"是靠把内容丢掉/固定换来的。
    """
    import hashlib

    sn = _load("sdist_normalize")
    src = _mk_tar([("pkg-1.0/mod.py", b"x = 1\n")], mtime=1790000111, mode=0o664,
                  order_note="e.tar.gz")
    a = sn.normalize_bytes(src, 1790000000)
    b = sn.normalize_bytes(src, 1790000001)
    assert hashlib.sha256(a).hexdigest() != hashlib.sha256(b).hexdigest(), "epoch 变了 sha 还相同：归一没在钉时间"
    other = _mk_tar([("pkg-1.0/mod.py", b"x = 2\n")], mtime=1790000111, mode=0o664,
                    order_note="e.tar.gz")
    assert sn.normalize_bytes(other, 1790000000) != a, "内容变了 sha 还相同：确定性是靠无视内容换来的"


def test_the_normalizer_has_exactly_two_consumers() -> None:
    """发布路径与探针必须走同一份实现（两处各写一遍就是 N-57/N-59 反复拆的那种形状）。"""
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    probe = (SCRIPTS / "artifact_reproducibility.py").read_text(encoding="utf-8")
    assert "scripts/sdist_normalize.py" in make, "make build 没接归一：发出去的字节与探针量的不是同一个形状"
    assert "import sdist_normalize" in probe and "sdist_normalize.normalize(" in probe
    assert 'env["SOURCE_DATE_EPOCH"]' in probe, "探针另读了一次环境：归一与构建可能钉到不同基准"
