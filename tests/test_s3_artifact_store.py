"""S3 兼容对象存储真实后端档（§21）。

此前 `S3CompatibleArtifactStore` 的四个方法从未执行过：boto3 不在依赖里（懒加载
直接 ImportError），全部"测试"都是 `monkeypatch` 掉 `_client()` 再注入一个自造的
`botocore.exceptions.ClientError`。本档用真实 SDK 打真实服务端（tests/s3_server.py
自起一次性容器），并把同一批读数与第二台独立服务端交叉核对（读数登记在
docs/adr/0008）。

本档实测发现并钉住的两个缺陷：
1. HEAD 响应**没有 body**，所以「对象不存在」与「桶不存在」在 HeadObject 上同形
   （都只给出 `Error.Code == "404"`）。旧 `exists()` 只看 Code，于是桶被删/配错时
   返回 False = "产物不存在"，正是它 docstring 声称要避免的误判；旧的 fake 测试
   看不见，是因为 fake 给 HEAD 编造了 `NoSuchBucket` 响应体。
2. Local 用 ArtifactStoreError 表达「对象不存在」，S3 却把裸 ClientError 抛给调用方
   ——同一个 Protocol 两种异常类型；`verify_checksum` 因此把任何存储故障一律写成
   "artifact object missing" 并把部署推进不可重验的 FAILED 终态。
"""

import hashlib

import httpx
import pytest
from botocore.exceptions import ClientError

from app.services.artifact_store import (
    ArtifactNotFoundError,
    ArtifactStoreError,
    ArtifactStoreUnavailableError,
    LocalArtifactStore,
    S3CompatibleArtifactStore,
)
from tests.s3_server import (
    ACCESS_KEY,
    GATE_SENTINEL,
    SECRET_KEY,
    S3Server,
    gate_reason,
    unique_bucket,
)

pytestmark = pytest.mark.s3_integration

KEY = "ws-1/checkpoints/model.bin"


@pytest.fixture(scope="session")
def s3():
    reason = gate_reason()
    if reason is not None:
        pytest.skip(f"{GATE_SENTINEL}: {reason}")
    server = S3Server().start()
    yield server
    server.stop()


def _empty_and_delete(server: S3Server, bucket: str) -> None:
    """清空并删除桶；桶已不存在则视为完成（用例中途会自己删桶，teardown 要幂等）。"""
    client = server.client()
    try:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            keys = [obj["Key"] for obj in page.get("Contents", [])]
            if keys:
                client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys]})
        client.delete_bucket(Bucket=bucket)
    except ClientError as exc:
        if _code(exc) != "NoSuchBucket":
            raise


def _code(exc: ClientError) -> str:
    return str(((exc.response or {}).get("Error") or {}).get("Code", ""))


def _store_for(server: S3Server, bucket: str, secret_key: str = SECRET_KEY) -> S3CompatibleArtifactStore:
    return S3CompatibleArtifactStore(
        bucket=bucket,
        endpoint_url=server.endpoint_url(),
        access_key=ACCESS_KEY,
        secret_key=secret_key,
    )


@pytest.fixture
def bucket(s3):
    """每个用例一个独立桶：桶级隔离，同时让"桶消失"成为可构造的场景。"""
    name = unique_bucket()
    s3.client().create_bucket(Bucket=name)
    yield name
    _empty_and_delete(s3, name)


@pytest.fixture
def store(s3, bucket):
    return _store_for(s3, bucket)


# ---------------------------------------------------------------------------
# 先证明「这一档真的打在一台进程外的 S3 服务端上」，否则下面全部读数无意义
# ---------------------------------------------------------------------------


def test_endpoint_is_a_real_http_s3_server_that_demands_signature(s3, bucket):
    """匿名请求被拒（实测 403）⇒ 端点上真有一个会验签的 HTTP 服务在监听。

    如果哪天 SDK 出口被换成内存 fake，这条会先红。
    """
    anonymous = httpx.get(s3.endpoint_url(), timeout=10)
    assert anonymous.status_code == 403, anonymous.text
    # 桶确实建在服务端上（不是客户端本地记账）
    assert bucket in {b["Name"] for b in s3.client().list_buckets()["Buckets"]}
    assert s3.container_state() == "true"
    assert s3.running_image().startswith("sha256:")


def test_sdk_is_the_real_one_not_an_injected_fake(s3):
    import botocore.client

    client = s3.client()
    assert isinstance(client, botocore.client.BaseClient)
    # 真实 SigV4 往返会留下服务端响应元数据；手搓的 fake client 给不出这些
    meta = client.list_buckets()["ResponseMetadata"]
    assert meta["HTTPStatusCode"] == 200
    assert meta["HTTPHeaders"], meta


# ---------------------------------------------------------------------------
# 正常闭包：读写删与内容保真
# ---------------------------------------------------------------------------


def test_roundtrip_binary_and_unicode_key(store):
    payload = bytes(range(256)) * 4096  # 1 MiB，非 UTF-8
    key = "课程 作业/ws-1 模型 v2.bin"
    assert store.exists(key) is False
    store.put(key, payload, content_type="application/octet-stream")
    assert store.exists(key) is True
    assert store.get(key) == payload
    assert hashlib.sha256(store.get(key)).hexdigest() == hashlib.sha256(payload).hexdigest()
    store.delete(key)
    assert store.exists(key) is False


def test_content_type_survives_the_wire(store, s3, bucket):
    """ContentType 必须真的写进对象元数据（部署侧按它决定下游怎么读）。"""
    body = b'{"ok": true}'
    store.put("ws-1/report.json", body, content_type="application/json")
    head = s3.client().head_object(Bucket=bucket, Key="ws-1/report.json")
    assert head["ContentType"] == "application/json"
    assert head["ContentLength"] == len(body)


def test_multi_mib_object_matches_byte_for_byte(store):
    """4 MiB 走真实传输路径（分块、Content-Length、md5 校验都在这里）。"""
    payload = (hashlib.sha256(b"seed").digest() * (4 * 1024 * 1024 // 32 + 1))[: 4 * 1024 * 1024]
    store.put("ws-1/big.bin", payload)
    got = store.get("ws-1/big.bin")
    assert len(got) == len(payload) == 4 * 1024 * 1024
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_overwrite_is_last_write_wins(store):
    store.put(KEY, b"v1")
    store.put(KEY, b"v2-longer")
    assert store.get(KEY) == b"v2-longer"


def test_delete_is_idempotent_on_absent_key(store):
    store.delete("ws-1/never-written.bin")  # S3 语义：删不存在的键不报错


def test_get_missing_key_on_live_bucket_is_not_found(store):
    with pytest.raises(ArtifactNotFoundError, match="object not found"):
        store.get("ws-1/nope.bin")


# ---------------------------------------------------------------------------
# 缺陷 1：「对象不存在」与「存储不可用」必须分开
# ---------------------------------------------------------------------------


def test_missing_bucket_is_not_reported_as_absent_object(s3):
    """桶不存在时 exists() 必须上抛，而不是 False（HEAD 无 body ⇒ 只能二次探桶）。

    变异对照：删掉 exists() 里的 head_bucket 探测，本条即红（返回 False）。
    """
    ghost = _store_for(s3, unique_bucket("ec-never-created"))
    with pytest.raises(ArtifactStoreUnavailableError, match="head_bucket"):
        ghost.exists(KEY)


def test_read_from_a_vanished_bucket_raises_unavailable_not_notfound(store, s3, bucket):
    """桶在写入之后被删：读它必须报「不可用」，不能报「产物不存在」。"""
    store.put(KEY, b"model")
    _empty_and_delete(s3, bucket)
    with pytest.raises(ArtifactStoreUnavailableError, match="get_object") as exc:
        store.get(KEY)
    assert not isinstance(exc.value, ArtifactNotFoundError)
    with pytest.raises(ArtifactStoreUnavailableError, match="head_bucket"):
        store.exists(KEY)


def test_write_and_delete_on_a_vanished_bucket_raise_unavailable(store, s3, bucket):
    """写/删同理：故障不能被降格成"没有这个对象"。"""
    _empty_and_delete(s3, bucket)
    with pytest.raises(ArtifactStoreUnavailableError, match="put_object"):
        store.put(KEY, b"x")
    with pytest.raises(ArtifactStoreUnavailableError, match="delete_object"):
        store.delete(KEY)


def test_bad_signature_is_unavailable_not_absent(store, s3, bucket):
    """403 既不是"在"也不是"不在"：用错 secret 必须上抛 Unavailable。"""
    store.put(KEY, b"present")
    wrong = _store_for(s3, bucket, secret_key="nope")  # noqa: S106 故意的错凭据
    with pytest.raises(ArtifactStoreUnavailableError, match="head_object"):
        wrong.exists(KEY)
    with pytest.raises(ArtifactStoreUnavailableError, match="get_object"):
        wrong.get(KEY)


def test_unreachable_endpoint_is_unavailable(s3):
    """端口没人监听：网络故障必须读成 Unavailable（不是 False、也不是裸 SDK 异常）。"""
    dead = S3CompatibleArtifactStore(
        bucket=unique_bucket("ec-dead"),
        endpoint_url="http://127.0.0.1:1",
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
    )
    with pytest.raises(ArtifactStoreUnavailableError):
        dead.exists(KEY)
    assert s3.container_state() == "true"  # 容器还在，说明失败的是那个假端点


def test_sdk_exceptions_never_escape_the_protocol(s3, bucket):
    """Protocol 的异常面只有 ArtifactStoreError 家族：裸 ClientError 泄漏会让 Local
    与 S3 两种后端在同一处 `except` 下行为不同。"""
    _empty_and_delete(s3, bucket)
    for call in (
        lambda: _store_for(s3, bucket).get(KEY),
        lambda: _store_for(s3, bucket).put(KEY, b"x"),
        lambda: _store_for(s3, bucket).delete(KEY),
        lambda: _store_for(s3, bucket).exists(KEY),
    ):
        with pytest.raises(ArtifactStoreError) as exc:
            call()
        assert not isinstance(exc.value, ClientError), type(exc.value)


# ---------------------------------------------------------------------------
# 与本地后端的语义平价（同一判据两档并排，防止"只有 S3 侧修好了"）
# ---------------------------------------------------------------------------


def test_local_and_s3_agree_on_absent_and_found_verdicts(store, tmp_path, s3, bucket):
    local = LocalArtifactStore(tmp_path / "store")
    store.put(KEY, b"payload")
    local.put(KEY, b"payload")
    assert store.exists(KEY) is True
    assert local.exists(KEY) is True
    assert store.get(KEY) == local.get(KEY) == b"payload"
    assert store.exists("nope.bin") is False
    assert local.exists("nope.bin") is False
    with pytest.raises(ArtifactNotFoundError) as local_exc:
        local.get("nope.bin")
    with pytest.raises(ArtifactNotFoundError) as s3_exc:
        store.get("nope.bin")
    assert type(local_exc.value) is type(s3_exc.value) is ArtifactNotFoundError
    store.delete(KEY)
    local.delete(KEY)
    assert store.exists(KEY) is False
    assert local.exists(KEY) is False


@pytest.mark.parametrize("bad_key", ["../etc/passwd", "a/../../etc/passwd", "/abs/path", "..", ""])
def test_traversal_rejected_before_any_network_call(bad_key):
    """端点指向一个没人监听的端口：若真发出了请求，异常会是连接错误而不是 key 校验错误。"""
    st = S3CompatibleArtifactStore(
        bucket="ec-any", endpoint_url="http://127.0.0.1:1", access_key="ak", secret_key="sk"  # noqa: S106
    )
    with pytest.raises(ArtifactStoreError, match=r"invalid object key|escapes"):
        st.put(bad_key, b"x")
    with pytest.raises(ArtifactStoreError, match=r"invalid object key|escapes"):
        st.get(bad_key)
