"""ArtifactStore：对象存储抽象（§21）。

- LocalArtifactStore：开发/本地（MinIO 或本机目录）
- S3CompatibleArtifactStore：生产（凭据未配置时明确 BLOCKED，不假装可用）
- 防 path traversal：object_key 规范化并拒绝逃逸
- 生产逻辑不得依赖控制面直接读取每个 Workspace PVC（对象存储解耦）
"""

from pathlib import Path, PurePosixPath
from typing import Protocol


class ArtifactStoreError(RuntimeError):
    pass


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
            raise ArtifactStoreError(f"object not found: {object_key!r}")
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
    """

    name = "s3"

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
            raise ArtifactStoreError("boto3 not installed; cannot use S3 store") from exc
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
        )

    def put(self, object_key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        _safe_key(object_key)
        self._client().put_object(Bucket=self.bucket, Key=object_key, Body=data, ContentType=content_type)

    def get(self, object_key: str) -> bytes:
        _safe_key(object_key)
        response = self._client().get_object(Bucket=self.bucket, Key=object_key)
        return response["Body"].read()

    def exists(self, object_key: str) -> bool:
        """对象存在性探测：仅 S3「不存在」语义（404/NoSuchKey）返回 False。

        鉴权失败（403）、桶不存在（NoSuchBucket）、网络错误等一律上抛
        ArtifactStoreError —— 把「故障」误判为「对象不存在」会让调用方静默
        吞掉真实错误，破坏部署校验/回滚等链路。
        """
        _safe_key(object_key)
        # 先取 client：boto3 未安装时 _client() 已抛 ArtifactStoreError（不假装可用）
        client = self._client()
        try:
            client.head_object(Bucket=self.bucket, Key=object_key)
            return True
        except Exception as exc:
            # 懒加载 botocore；只有 ClientError 的「不存在」语义幂等返回 False，
            # 其余（403 鉴权 / NoSuchBucket / 网络错误等）一律上抛。
            try:
                from botocore.exceptions import ClientError
            except ImportError:
                raise ArtifactStoreError(f"S3 head_object failed: {exc}") from exc
            if isinstance(exc, ClientError):
                error = (exc.response or {}).get("Error") or {}
                code = error.get("Code", "")
                # 仅按 Error Code 判定「不存在」：NoSuchBucket 的 HTTP 状态码也是 404，
                # 若按 status==404 判定会把「桶不存在」误判为「对象不存在」。
                if code in {"404", "NoSuchKey"}:
                    return False
            raise ArtifactStoreError(f"S3 head_object failed: {exc}") from exc

    def delete(self, object_key: str) -> None:
        _safe_key(object_key)
        self._client().delete_object(Bucket=self.bucket, Key=object_key)
