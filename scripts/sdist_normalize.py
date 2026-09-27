"""sdist 的头部归一：把"同一份源码两次构建得到同一字节"这件事变成可交付的主张。

为什么需要它（N-34 留下的那一半，本轮 N-60 闭上）：`dist/checksums.txt` 交出去的是一行
sha256，第三方能不能重建出同样的字节？setuptools 84.0.0（本机装的与 PyPI 最新同版本，
2026-08-08 发布）**不**把 sdist 的目录条目、`PKG-INFO` 与各子目录的 mtime 夹到
`SOURCE_DATE_EPOCH`，pax 记录里还带小数秒，于是 tar 头记的是"打包那一秒"。
本轮一手实测（`SOURCE_DATE_EPOCH` 固定、同一棵树立两次）：
未归一 `6b6d115a25` 对 `ad66f49101`（漂）；归一后 `35e21305d96142c1` 两次逐字节相同、
成员 137 个、每个成员的内容 sha 与归一前逐个相等（只动头部不动内容）。

语义上的出处是 **Ansible 的发布脚本**：`packaging/release.py` 里
`create_reproducible_sdist` 配 `create_reproducible_tar_info`，逐条改写 TarInfo 的
mtime/mode/uid/gid/uname/gname，再以该 epoch 作 gzip mtime 重新压缩，并且整个发布链把
`SOURCE_DATE_EPOCH` 取成 commit 时间——与本文件同一套动作。本轮只借形状不引代码（那边是
GPL-2.0/3 的项目、且它是发布工具链而非库），实现只用标准库 `tarfile` + `gzip`。

顺手排除一个看着更省事的候选：Debian 的 `strip-nondeterminism`（salsa
reproducible-builds/strip-nondeterminism，master 分支，`COPYING` 为 GPL-3）**没有 tar 处理器**——
它的 handlers 是 ar/bflt/cpio/gettext/gzip/jar/javadoc/jmod/png/pyzip/uimage/zip，
`gzip.pm` 只重写 gzip 那 10 字节头部，管不到 tar 成员的 mtime/属主/顺序。所以它做不成这件事，
与"要不要引一个 GPL 工具"无关。

两个坑都进了常驻判据，因为它们各是一次真实的失败形状：
1. `gzip` 的 FNAME 字段会写进**输出文件路径**：`GzipFile` 在没传 `filename` 时从
   `fileobj.name` 推断（本机单变量实测：有名字的文件句柄 + 不传 ⇒ FLG=0x08；传
   `filename=""` ⇒ FLG=0x00）。本机第一版就栽在这——成员逐个逐字节相同，两份
   `.tar.gz` 的 sha 仍不同。本实现写进 `BytesIO`（没有 `.name`），所以恰好不会踩；
   `filename=""` 因此是**把它钉死的显式保证**，而不是当前形状的承重墙——
   判据两头都留了：一头核我们的输出里没有这一位，一头用有名字的句柄复现这一位会出现。
2. tar 格式必须钉死（这里用 GNU_FORMAT）：默认的 pax 格式会按成员字段决定要不要写
   pax 扩展记录，一旦某条成员的 uid/uname 长短变化，头部布局就跟着变。

主张的口径也在这里说清：`checksums.txt` 里 sdist 那行是**归一后**产物的 sha。
第三方要复算，得走同一条 `make build`（含这一步归一）+ 同一个 `SOURCE_DATE_EPOCH`，
而且是**同一个 Python/zlib**：gzip 那一层的字节由 zlib 的压缩参数与版本决定，tar 的 GNU 头部布局也由
stdlib 实现决定——换解释器或换 zlib 版本都可能改 sha 而没有任何东西坏掉。这一步是公开、确定性、
只依赖标准库的，所以"可复算"仍然是一句可被推翻的话，而不是把产物换成黑盒。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tarfile
from gzip import GzipFile
from pathlib import Path

DEFAULT_EPOCH_ENV = "SOURCE_DATE_EPOCH"
# gzip 头部 FLG 的 FNAME 位（RFC 1952）：置位就表示头部带了一个以 NUL 结尾的文件名。
FNAME_FLAG = 0x08


def epoch_from_env(env: dict[str, str] | None = None) -> int:
    """取时间基准。读不到就直接失败：默默用"现在"就是把主张写成不可复算。"""
    raw = (env if env is not None else os.environ).get(DEFAULT_EPOCH_ENV, "").strip()
    if not raw:
        raise RuntimeError(
            f"{DEFAULT_EPOCH_ENV} 未设置：归一无时间基准可钉，宁可失败也不退回打包那一刻"
        )
    return int(raw)


def normalize_bytes(payload: bytes, epoch: int) -> bytes:
    """把一个 .tar.gz 归档的头部归一到 epoch，内容逐字节保留。

    成员顺序按名字排（setuptools 侧的目录遍历顺序不保证稳定），属主/组归零并清空
    uname/gname，mode 去掉写位与粘滞位以外的差异（& 0o755），pax 扩展记录一律不落
    （格式钉成 GNU_FORMAT，长名由 tar 自己的 GNU 扩展承载）。
    """
    with tarfile.open(fileobj=io.BytesIO(payload)) as tf:
        members = sorted(tf.getmembers(), key=lambda m: m.name)
        rebuilt = io.BytesIO()
        with tarfile.open(fileobj=rebuilt, mode="w", format=tarfile.GNU_FORMAT) as out:
            for info in members:
                handle = tf.extractfile(info) if info.isreg() else None
                data = handle.read() if handle is not None else None
                info.mtime = epoch
                info.mode &= 0o755
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.pax_headers = {}
                if data is None:
                    out.addfile(info)
                else:
                    info.size = len(data)
                    out.addfile(info, io.BytesIO(data))
        gz = io.BytesIO()
        with GzipFile(fileobj=gz, mode="wb", filename="", mtime=epoch, compresslevel=9) as fh:
            fh.write(rebuilt.getvalue())
        return gz.getvalue()


def normalize(path: Path, epoch: int | None = None) -> bytes:
    """就地归一一个 sdist 文件（读→归一→覆盖），并返回归一后的字节。"""
    out = normalize_bytes(path.read_bytes(), epoch if epoch is not None else epoch_from_env())
    path.write_bytes(out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sdist 头部归一（不动内容）")
    ap.add_argument("sdist", type=Path, nargs="+", help="要就地归一的 *.tar.gz")
    args = ap.parse_args(argv)
    try:
        epoch = epoch_from_env()
    except (RuntimeError, ValueError) as exc:
        print(f"[sdist-normalize] {exc}", file=sys.stderr)
        return 2
    for target in args.sdist:
        if not target.exists():
            print(f"[sdist-normalize] 文件不存在：{target}", file=sys.stderr)
            return 2
        normalize(target, epoch)
        print(f"[sdist-normalize] {target.name} 头部已钉到 SOURCE_DATE_EPOCH={epoch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
