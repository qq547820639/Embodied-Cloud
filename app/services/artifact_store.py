"""ArtifactStore：对象存储抽象（§21）。

- LocalArtifactStore：开发/本地（MinIO 或本机目录）
- S3CompatibleArtifactStore：生产（凭据未配置时明确 BLOCKED，不假装可用）
- 防 path traversal：object_key 规范化并拒绝逃逸
- 生产逻辑不得依赖控制面直接读取每个 Workspace PVC（对象存储解耦）
- 异常分两型（ArtifactNotFoundError / ArtifactStoreUnavailableError）：
  「产物不存在」是可判定的业务结论，「存储不可用」不是——两者在 S3 协议上
  只在带 body 的请求里天然可分，HEAD 一律 404，故 exists() 需二次探桶。
"""

from pathlib import Path, PurePosixPath
from typing import Protocol


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactNotFoundError(ArtifactStoreError):
    """对象确实不存在（存储本身可用）——是可判定的业务结论。"""


class ArtifactStoreUnavailableError(ArtifactStoreError):
    """存储不可用/不可判定：鉴权失败、桶不存在、网络错误、缺 SDK。

    与「对象不存在」必须分开：把故障读成不存在，会让部署校验把一次网络抖动
    永久写成 FAILED（终态不可重试），而真实缺失本来就该 FAILED。
    """


class ArtifactStore(Protocol):
    name: str

    def put(self, object_key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    def get(self, object_key: str) -> bytes: ...
    def exists(self, object_key: str) -> bool: ...
    def delete(self, object_key: str) -> None: ...


def _safe_key(object_key: str) -> PurePosixPath:
    """规范化 object_key 并拒绝逃逸（path traversal 防护）。"""
    if not object_key or object_key.startswith("/"):
        raise ArtifactStoreError(f"invalid object key: {object_key!r}")
    path = PurePosixPath(object_key)
    if ".." in path.parts:
        raise ArtifactStoreError(f"object key escapes store root: {object_key!r}")
    return path


class LocalArtifactStore:
    """本地文件系统实现（开发/单机；数据在 store root 下按 object_key 落盘）。"""

    name = "local"

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _full_path(self, object_key: str) -> Path:
        safe = _safe_key(object_key)
        return (self.root / safe).resolve()

    def put(self, object_key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        full = self._full_path(object_key)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)

    def get(self, object_key: str) -> bytes:
        full = self._full_path(object_key)
        if not full.is_file():
            raise ArtifactNotFoundError(f"object not found: {object_key!r}")
        return full.read_bytes()

    def exists(self, object_key: str) -> bool:
        full = self._full_path(object_key)
        return full.is_file()

    def delete(self, object_key: str) -> None:
        full = self._full_path(object_key)
        if full.is_file():
            full.unlink()


class S3CompatibleArtifactStore:
    """S3/MinIO 兼容实现（生产）。

    依赖 boto3（懒加载）；未配置 endpoint/凭据时明确报 BLOCKED_EXTERNAL_DEPENDENCY，
    不允许静默降级到本地。

    协议事实（两台独立 S3 兼容服务端实测，见 docs/adr/0008）：**HEAD 响应没有 body**，
    所以 HeadObject 在「对象不存在」与「桶不存在」两种情况下都只给出
    `Error.Code == "404"`；只有带 body 的 GET/PUT/DELETE 才会给出 `NoSuchKey` /
    `NoSuchBucket`。因此「404 就当对象不存在」必然把「桶没了」读成「产物没了」，
    本类在 404 分支上再探一次 HeadBucket 来分开两者。
    """

    name = "s3"

    # HeadObject 无 body → "404"；GetObject 有 body → "NoSuchKey"（实测两种都要认）
    _OBJECT_ABSENT_CODES = frozenset({"404", "NoSuchKey"})

    def __init__(self, bucket: str, endpoint_url: str = "", access_key: str = "", secret_key: str = ""):
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key
        if not endpoint_url or not access_key or not secret_key:
            raise ArtifactStoreError(
                "BLOCKED_EXTERNAL_DEPENDENCY: S3-compatible object store credentials "
                "not configured (bucket/endpoint/access_key/secret_key)"
            )

    def _client(self):
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - 依赖未安装
            raise ArtifactStoreUnavailableError("boto3 not installed; cannot use S3 store") from exc
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
        )

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        """取 botocore ClientError 的 Error.Code；非 ClientError 返回空串。

        只读 Code，不看 HTTP status：桶缺失与对象缺失的 status 都是 404。
        """
        try:
            from botocore.exceptions import ClientError
        except ImportError:  # pragma: no cover - boto3 未安装时到不了这里
            return ""
        if not isinstance(exc, ClientError):
            return ""
        return str(((exc.response or {}).get("Error") or {}).get("Code", ""))

    def _unavailable(self, op: str, object_key: str, exc: BaseException) -> ArtifactStoreUnavailableError:
        return ArtifactStoreUnavailableError(
            f"S3 {op} failed (bucket={self.bucket!r} key={object_key!r}): {exc}"
        )

    def put(self, object_key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        _safe_key(object_key)
        try:
            self._client().put_object(Bucket=self.bucket, Key=object_key, Body=data, ContentType=content_type)
        except ArtifactStoreError:
            raise
        except Exception as exc:
            raise self._unavailable("put_object", object_key, exc) from exc

    def get(self, object_key: str) -> bytes:
        _safe_key(object_key)
        try:
            response = self._client().get_object(Bucket=self.bucket, Key=object_key)
            return response["Body"].read()
        except ArtifactStoreError:
            raise
        except Exception as exc:
            if self._error_code(exc) in self._OBJECT_ABSENT_CODES:
                raise ArtifactNotFoundError(f"object not found: {object_key!r}") from exc
            raise self._unavailable("get_object", object_key, exc) from exc

    def exists(self, object_key: str) -> bool:
        """对象存在性探测：只有「桶可用且该 key 不在」才返回 False。

        鉴权失败（403）、桶不存在、网络错误一律上抛
        ArtifactStoreUnavailableError —— 把「故障」误判为「对象不存在」会让调用方
        静默吞掉真实错误，破坏部署校验/回滚等链路。
        """
        _safe_key(object_key)
        # 先取 client：boto3 未安装时 _client() 已抛 Unavailable（不假装可用）
        client = self._client()
        try:
            client.head_object(Bucket=self.bucket, Key=object_key)
            return True
        except Exception as exc:
            absent = self._error_code(exc) in self._OBJECT_ABSENT_CODES
            if not absent:
                # 403 / 网络 / SDK 异常：既不是「在」也不是「不在」
                raise self._unavailable("head_object", object_key, exc) from exc
        # HEAD 无 body ⇒ 「key 不在」与「桶不在」同形，只能再问一次桶
        try:
            client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            raise self._unavailable("head_bucket", object_key, exc) from exc
        return False

    def delete(self, object_key: str) -> None:
        _safe_key(object_key)
        try:
            self._client().delete_object(Bucket=self.bucket, Key=object_key)
        except ArtifactStoreError:
            raise
        except Exception as exc:
            raise self._unavailable("delete_object", object_key, exc) from exc


def build_artifact_store(
    backend: str,
    *,
    workspace_root: Path,
    bucket: str = "",
    endpoint_url: str = "",
    access_key: str = "",
    secret_key: str = "",
) -> "ArtifactStore":
    """按配置装配对象存储（§21）。

    只认显式配置：`s3` 后端缺凭据时 S3CompatibleArtifactStore 直接抛
    BLOCKED_EXTERNAL_DEPENDENCY，**不静默回退本地目录**——回退会把生产产物写进
    控制面文件系统，而部署记录里的 store_name 仍然指向 s3。
    """
    if backend == "local":
        # 与 DeploymentService 的默认 store 同根，接上配置后 local 路径零变化
        return LocalArtifactStore(workspace_root)
    if backend == "s3":
        return S3CompatibleArtifactStore(
            bucket=bucket, endpoint_url=endpoint_url, access_key=access_key, secret_key=secret_key
        )
    raise ArtifactStoreError(f"unknown artifact store backend: {backend!r} (local|s3)")

