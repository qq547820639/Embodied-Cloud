"""ArtifactStore 离线档（§21）：Local 读写 + path traversal + S3 语义的协议保真 fake。

这里只放**不需要服务端**的用例。真实服务端档在 tests/test_s3_artifact_store.py
（marker s3_integration，自起一次性容器）。

fake 的一条纪律：异常载荷必须照线上实测形状给（docs/adr/0008 的读数）——
HeadObject 的 404 **没有响应体**，botocore 只能给出 `Error.Code == "404"`，
不可能给出 `NoSuchBucket`。旧版这份文件正是犯了那个错（给 HEAD 编造了
`Code="NoSuchBucket"`），于是「桶不存在必须上抛」在 fake 档长绿、在真服务端上
其实是把桶缺失读成了产物缺失。现在两种 404 同形，分开它们靠的是实现里的
HeadBucket 二次探测，fake 档同样能测到（见 test_head_404_on_dead_bucket_*）。
"""

import sys

import pytest
from botocore.exceptions import ClientError

from app.services.artifact_store import (
    ArtifactNotFoundError,
    ArtifactStoreError,
    ArtifactStoreUnavailableError,
    LocalArtifactStore,
    S3CompatibleArtifactStore,
    build_artifact_store,
)

# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------


def test_local_store_roundtrip(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    store.put("ws-1/checkpoints/model.pt", b"model-bytes", content_type="application/octet-stream")
    assert store.exists("ws-1/checkpoints/model.pt")
    assert store.get("ws-1/checkpoints/model.pt") == b"model-bytes"
    store.delete("ws-1/checkpoints/model.pt")
    assert not store.exists("ws-1/checkpoints/model.pt")


def test_local_store_missing_object_raises_not_found(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    with pytest.raises(ArtifactNotFoundError, match="not found"):
        store.get("nope")


def test_local_and_s3_share_one_exception_family() -> None:
    """Protocol 的异常面必须是同一个家族，否则同一处 `except` 在两种后端下行为不同。"""
    assert issubclass(ArtifactNotFoundError, ArtifactStoreError)
    assert issubclass(ArtifactStoreUnavailableError, ArtifactStoreError)
    assert not issubclass(ArtifactStoreError, ClientError)


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


# ---------------------------------------------------------------------------
# S3：配置与装配（不联网）
# ---------------------------------------------------------------------------


def test_s3_store_requires_credentials():
    """无凭据 → BLOCKED_EXTERNAL_DEPENDENCY（不允许静默降级）。"""
    with pytest.raises(ArtifactStoreError, match="BLOCKED_EXTERNAL_DEPENDENCY"):
        S3CompatibleArtifactStore(bucket="ec-artifacts")
    with pytest.raises(ArtifactStoreError, match="BLOCKED_EXTERNAL_DEPENDENCY"):
        S3CompatibleArtifactStore(
            bucket="ec-artifacts", endpoint_url="http://s3:9000", access_key="", secret_key=""
        )


def test_build_artifact_store_local_is_the_workspace_root_store(tmp_path):
    store = build_artifact_store("local", workspace_root=tmp_path)
    assert isinstance(store, LocalArtifactStore)
    assert store.root == tmp_path  # 与 DeploymentService 的默认 store 同根：接入配置后 local 零变化


def test_build_artifact_store_s3_never_falls_back_to_local(tmp_path):
    """backend=s3 而凭据不全 → 装配期就抛，绝不静默给一个 Local store。

    静默回退会把产物写进控制面文件系统，而 Artifact.store_name 仍记 "s3"，
    部署校验于是去对象存储找一个从来没写进去的键。
    """
    with pytest.raises(ArtifactStoreError, match="BLOCKED_EXTERNAL_DEPENDENCY"):
        build_artifact_store("s3", workspace_root=tmp_path, bucket="ec-artifacts")
    with pytest.raises(ArtifactStoreError, match="unknown artifact store backend"):
        build_artifact_store("glacier", workspace_root=tmp_path)
    assert not list(tmp_path.iterdir())  # 失败路径不留半个目录


def test_build_artifact_store_s3_with_credentials(tmp_path):
    store = build_artifact_store(
        "s3",
        workspace_root=tmp_path,
        bucket="ec-artifacts",
        endpoint_url="http://127.0.0.1:9000",
        access_key="ak",
        secret_key="sk",  # noqa: S106 测试数据
    )
    assert store.name == "s3"
    assert store.bucket == "ec-artifacts"


def test_s3_store_traversal_rejected_before_client_call():
    """即使配置了凭据（未真正连接），traversal key 也在客户端调用前被拒绝。"""
    store = S3CompatibleArtifactStore(
        bucket="ec-artifacts",
        endpoint_url="http://s3:9000",
        access_key="ak",
        secret_key="sk",  # noqa: S106 测试数据
    )
    with pytest.raises(ArtifactStoreError):
        store.put("../escape", b"x")
    with pytest.raises(ArtifactStoreError):
        store.get("../escape")


def test_missing_boto3_is_unavailable_not_silent(monkeypatch):
    """SDK 没装：报 Unavailable（不静默降级），且不假装对象不存在。"""
    monkeypatch.setitem(sys.modules, "boto3", None)  # import boto3 → ImportError
    store = S3CompatibleArtifactStore(
        bucket="ec-artifacts", endpoint_url="http://s3:9000", access_key="ak", secret_key="sk"  # noqa: S106
    )
    with pytest.raises(ArtifactStoreUnavailableError, match="boto3 not installed"):
        store.exists("obj")


# ---------------------------------------------------------------------------
# S3：协议保真 fake
# ---------------------------------------------------------------------------


def _head_404() -> ClientError:
    """实测形状：HEAD 无 body ⇒ botocore 只把 HTTP 状态码填进 Code。

    对照 docs/adr/0008：两台独立服务端对「key 不存在」与「桶不存在」的 HeadObject
    都返回完全相同的 `{"Code": "404", "Message": "Not Found"}`。
    """
    return ClientError(
        {
            "Error": {"Code": "404", "Message": "Not Found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )


def _get_error(code: str, message: str) -> ClientError:
    """带 body 的请求（GET/PUT/DELETE）才有真正的错误码。"""
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": 404 if code in {"NoSuchKey", "NoSuchBucket"} else 403},
        },
        "GetObject",
    )


class _FakeS3Client:
    """只实现被测代码用到的四个操作，并把「服务端怎么答」做成参数。"""

    def __init__(self, head_error=None, get_error=None, put_error=None, delete_error=None, bucket_live=True):
        self.head_error = head_error
        self.get_error = get_error
        self.put_error = put_error
        self.delete_error = delete_error
        self.bucket_live = bucket_live
        self.head_calls: list[str] = []
        self.bucket_probe_calls: list[str] = []

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs["Key"])
        if self.head_error is not None:
            raise self.head_error
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def head_bucket(self, **kwargs):
        self.bucket_probe_calls.append(kwargs["Bucket"])
        if not self.bucket_live:
            raise _get_error("404", "Not Found")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object(self, **kwargs):
        if self.get_error is not None:
            raise self.get_error
        raise AssertionError("fake get_object needs an error fixture")

    def put_object(self, **kwargs):
        if self.put_error is not None:
            raise self.put_error
        return None

    def delete_object(self, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        return None


def _patched(monkeypatch, client: _FakeS3Client) -> S3CompatibleArtifactStore:
    store = S3CompatibleArtifactStore(
        bucket="ec-artifacts", endpoint_url="http://s3:9000", access_key="ak", secret_key="sk"  # noqa: S106
    )
    monkeypatch.setattr(store, "_client", lambda: client)
    return store


def test_s3_exists_found_returns_true_without_probing_bucket(monkeypatch):
    client = _FakeS3Client()
    store = _patched(monkeypatch, client)
    assert store.exists("obj") is True
    assert client.head_calls == ["obj"]
    assert client.bucket_probe_calls == []  # 命中就不必再探桶


def test_s3_head_404_on_live_bucket_probes_bucket_and_returns_false(monkeypatch):
    store = _patched(monkeypatch, _FakeS3Client(head_error=_head_404(), bucket_live=True))
    assert store.exists("obj") is False


def test_s3_head_404_on_dead_bucket_is_unavailable_not_absent(monkeypatch):
    """与真服务端档 test_missing_bucket_is_not_reported_as_absent_object 同判据的离线镜像。

    变异对照：去掉 exists() 的 head_bucket 探测 → 本条返回 False → 红。
    """
    store = _patched(monkeypatch, _FakeS3Client(head_error=_head_404(), bucket_live=False))
    with pytest.raises(ArtifactStoreUnavailableError, match="head_bucket"):
        store.exists("obj")


def test_s3_head_403_does_not_probe_bucket(monkeypatch):
    """鉴权失败：既不是"在"也不是"不在"，直接上抛，且不浪费一次探桶请求。"""
    client = _FakeS3Client(head_error=_get_error("403", "Forbidden"))
    store = _patched(monkeypatch, client)
    with pytest.raises(ArtifactStoreUnavailableError, match="head_object"):
        store.exists("obj")
    assert client.bucket_probe_calls == []


def test_s3_get_missing_key_is_not_found(monkeypatch):
    missing = _get_error("NoSuchKey", "The specified key does not exist.")
    store = _patched(monkeypatch, _FakeS3Client(get_error=missing))
    with pytest.raises(ArtifactNotFoundError, match="object not found"):
        store.get("obj")


def test_s3_get_dead_bucket_is_unavailable(monkeypatch):
    dead = _get_error("NoSuchBucket", "The specified bucket does not exist.")
    store = _patched(monkeypatch, _FakeS3Client(get_error=dead))
    with pytest.raises(ArtifactStoreUnavailableError, match="get_object"):
        store.get("obj")


@pytest.mark.parametrize(
    ("attrs", "call", "op"),
    [
        ({"put_error": _get_error("NoSuchBucket", "nope")}, lambda s: s.put("obj", b"x"), "put_object"),
        ({"delete_error": _get_error("NoSuchBucket", "nope")}, lambda s: s.delete("obj"), "delete_object"),
    ],
)
def test_s3_write_failures_are_translated(monkeypatch, attrs, call, op):
    """写/删的 SDK 异常必须换成本地家族，且带上是哪个操作（运维要能分辨）。"""
    store = _patched(monkeypatch, _FakeS3Client(**attrs))
    with pytest.raises(ArtifactStoreUnavailableError, match=op) as exc:
        call(store)
    assert not isinstance(exc.value, ClientError)


def test_s3_bucket_and_key_are_reported_back(monkeypatch):
    """错误信息带桶与键：跨环境排障时这是唯一的定位线索。"""
    store = _patched(monkeypatch, _FakeS3Client(head_error=_head_404(), bucket_live=False))
    with pytest.raises(ArtifactStoreUnavailableError, match=r"bucket='ec-artifacts' key='deep/obj\.bin'"):
        store.exists("deep/obj.bin")


def test_s3_network_error_is_unavailable(monkeypatch):
    store = _patched(monkeypatch, _FakeS3Client(head_error=ConnectionError("connection refused")))
    with pytest.raises(ArtifactStoreUnavailableError, match="head_object"):
        store.exists("obj")


def test_s3_non_clienterror_on_read_is_unavailable(monkeypatch):
    """非 ClientError（网络/SDK 自身缺陷）不得被降格成"对象不存在"。"""
    store = _patched(monkeypatch, _FakeS3Client(get_error=ConnectionError("reset by peer")))
    with pytest.raises(ArtifactStoreUnavailableError, match="get_object"):
        store.get("obj")


def test_test_tier_image_tag_is_pinned():
    """常驻门禁不许用可变 tag：服务端语义会随 tag 漂移（与产品镜像禁 latest 同一条规则）。"""
    from tests.s3_server import image_name

    name = image_name()
    assert ":" in name and not name.endswith(":latest"), name
