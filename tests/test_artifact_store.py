"""ArtifactStore 测试（§21）：Local 读写 + path traversal 防护 + S3 BLOCKED 语义。"""

from pathlib import Path

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
    with pytest.raises(ArtifactStoreError, match="invalid object key|escapes"):
        store.put(bad_key, b"x")
    with pytest.raises(ArtifactStoreError, match="invalid object key|escapes"):
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
        secret_key="sk",
    )
    with pytest.raises(ArtifactStoreError):
        store.put("../escape", b"x")
    with pytest.raises(ArtifactStoreError):
        store.get("../escape")
