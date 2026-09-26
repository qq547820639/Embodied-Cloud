"""把 `TemplateVersion.image_digest` 用到运行时镜像引用上。

这一列自 v0.4 起就在模型与文档里（§15「记录 image/image_digest/…」），但本轮普查读数是
**0 处读者**——列存在不等于镜像被钉住：provider 一直用的是 `image`（一个可移动 tag）。
本模块是唯一消费点，落在 workspace 快照那一刻（见 `orchestrator.create`），
所以 docker/k8s 两条 provider 路径不需要各自再学一遍"要不要钉"。
"""

import re

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# `image` 列是 String(255)：拼上 digest 后缀（71 字符）可能溢出，PG 侧会是插入报错，
# SQLite 侧静默截断。宁可在这里说清楚是谁超限。
MAX_REF_LEN = 255


class ImageDigestError(ValueError):
    """声明了 digest 却用不上：形制不对、与 image 里已有的 digest 冲突、或引用过长。"""


def pinned_ref(image: str | None, digest: str | None) -> str | None:
    """`image` + `image_digest` → 不可变引用。

    - `image is None`：原样返回 None，让 provider 走它的下一档 fallback
      （`template.image` → `settings.workspace_image`），这里不越权造值。
    - `digest` 为空：返回 `image` 本身，与本轮之前的行为逐字相同（seed 出来的版本就是这档）。
    - `image` 自己已经带 digest：相等则幂等返回，不等则**拒绝**——两个 digest 意味着
      "版本记录说内容是 A、镜像引用说内容是 B"，挑任何一个都是猜。
    """
    if image is None:
        return None
    if not digest:
        return image
    if not DIGEST_RE.match(digest):
        raise ImageDigestError(f"image_digest 形制不对（要 sha256: + 64 位十六进制）：{digest!r}")
    _, sep, existing = image.partition("@")
    if sep:
        if existing != digest:
            raise ImageDigestError(f"image 里的 digest 与 image_digest 不一致：{existing!r} != {digest!r}")
        return image
    ref = f"{image}@{digest}"
    if len(ref) > MAX_REF_LEN:
        raise ImageDigestError(f"钉完 digest 的引用长 {len(ref)} 字符，超过 {MAX_REF_LEN} 列宽")
    return ref
