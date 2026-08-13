"""ArtifactStore 测试（§21）：Local 读写 + path traversal 防护 + S3 BLOCKED 语义。"""


import sys
import types

import pytest

from app.services.artifact_store import (
    ArtifactStoreError,
    LocalArtifactStore,
    S3CompatibleArtifactStore,
)


def test_local_store_roundtrip(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    store.put("ws-1/checkpoints/model.pt", b"model-bytes", content_type="application/octet-stream")
    assert store.exists("ws-1/checkpoints/model.pt")
    assert store.get("ws-1/checkpoints/model.pt") == b"model-bytes"
    store.delete("ws-1/checkpoints/model.pt")
    assert not store.exists("ws-1/checkpoints/model.pt")


def test_local_store_missing_object_raises(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    with pytest.raises(ArtifactStoreError, match="not found"):
        store.get("nope")


@pytest.mark.parametrize(
    "bad_key",
    [
        "../etc/passwd",
        "a/../../etc/passwd",
        "/abs/path",
        "..",
        "",
    ],
)
def test_path_traversal_rejected(tmp_path, bad_key):
    store = LocalArtifactStore(tmp_path / "store")
    with pytest.raises(ArtifactStoreError, match=r"invalid object key|escapes"):
        store.put(bad_key, b"x")
    with pytest.raises(ArtifactStoreError, match=r"invalid object key|escapes"):
        store.get(bad_key)


def test_s3_store_requires_credentials():
    """无凭据 → BLOCKED_EXTERNAL_DEPENDENCY（不允许静默降级）。"""
    with pytest.raises(ArtifactStoreError, match="BLOCKED_EXTERNAL_DEPENDENCY"):
        S3CompatibleArtifactStore(bucket="ec-artifacts")
    with pytest.raises(ArtifactStoreError, match="BLOCKED_EXTERNAL_DEPENDENCY"):
        S3CompatibleArtifactStore(
            bucket="ec-artifacts", endpoint_url="http://minio:9000", access_key="", secret_key=""
        )


def test_s3_store_traversal_rejected_before_client_call(tmp_path):
    """即使配置了凭据（未真正连接），traversal key 也在客户端调用前被拒绝。"""
    store = S3CompatibleArtifactStore(
        bucket="ec-artifacts",
        endpoint_url="http://minio:9000",
        access_key="ak",
        secret_key="sk",  # noqa: S106 测试数据
    )
    with pytest.raises(ArtifactStoreError):
        store.put("../escape", b"x")
    with pytest.raises(ArtifactStoreError):
        store.get("../escape")


# ---------------------------------------------------------------------------
# S3.exists 语义：仅「不存在」（404/NoSuchKey）→ False；其余异常上抛
# ---------------------------------------------------------------------------


def _install_fake_botocore(monkeypatch):
    """botocore/boto3 未安装时，注入最小 fake ClientError 供 exists 懒加载导入。"""
    exceptions = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        def __init__(self, error_response, operation_name):
            super().__init__(operation_name)
            self.response = error_response

    exceptions.ClientError = ClientError
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    return ClientError


class _FakeS3Client:
    def __init__(self, head_error=None):
        self.head_error = head_error
        self.called = False

    def head_object(self, **kwargs):
        self.called = True
        if self.head_error is not None:
            raise self.head_error
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def _s3_store() -> S3CompatibleArtifactStore:
    return S3CompatibleArtifactStore(
        bucket="ec-artifacts",
        endpoint_url="http://minio:9000",
        access_key="ak",
        secret_key="sk",  # noqa: S106 测试数据
    )


def test_s3_exists_found_returns_true(monkeypatch):
    store = _s3_store()
    client = _FakeS3Client()
    monkeypatch.setattr(store, "_client", lambda: client)

    assert store.exists("obj") is True
    assert client.called


def test_s3_exists_404_returns_false(monkeypatch):
    ClientError = _install_fake_botocore(monkeypatch)
    err = ClientError(
        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "HeadObject",
    )
    store = _s3_store()
    monkeypatch.setattr(store, "_client", lambda: _FakeS3Client(head_error=err))

    assert store.exists("obj") is False


def test_s3_exists_nosuchkey_returns_false(monkeypatch):
    ClientError = _install_fake_botocore(monkeypatch)
    err = ClientError(
        {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "HeadObject",
    )
    store = _s3_store()
    monkeypatch.setattr(store, "_client", lambda: _FakeS3Client(head_error=err))

    assert store.exists("obj") is False


def test_s3_exists_403_raises(monkeypatch):
    """鉴权失败（403）→ 上抛 ArtifactStoreError，不误判为「不存在」。"""
    ClientError = _install_fake_botocore(monkeypatch)
    err = ClientError(
        {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        "HeadObject",
    )
    store = _s3_store()
    monkeypatch.setattr(store, "_client", lambda: _FakeS3Client(head_error=err))

    with pytest.raises(ArtifactStoreError):
        store.exists("obj")


def test_s3_exists_nosuchbucket_raises(monkeypatch):
    """桶不存在（NoSuchBucket，HTTP 404）→ 上抛，不误判为「对象不存在」。

    S3 的 NoSuchBucket 错误 HTTP 状态码同样为 404，判定必须只看 Error Code。
    """
    ClientError = _install_fake_botocore(monkeypatch)
    err = ClientError(
        {"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "HeadObject",
    )
    store = _s3_store()
    monkeypatch.setattr(store, "_client", lambda: _FakeS3Client(head_error=err))

    with pytest.raises(ArtifactStoreError):
        store.exists("obj")


def test_s3_exists_other_error_raises(monkeypatch):
    """网络/连接错误（非 ClientError）→ 上抛 ArtifactStoreError。"""
    store = _s3_store()
    monkeypatch.setattr(
        store, "_client", lambda: _FakeS3Client(head_error=ConnectionError("timeout"))
    )

    with pytest.raises(ArtifactStoreError):
        store.exists("obj")
