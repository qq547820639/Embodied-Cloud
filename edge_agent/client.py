"""`X-Agent-Token` 鉴权的最小 HTTP 客户端（标准库 urllib，无第三方依赖）。

三件事值得写明：
- **token 不入异常文本**（SECURITY.md T2/T5）：`AgentClientError` 只带状态码、
  路径和服务端 detail，不带请求头。
- **不下载完再算摘要**：取件边写盘边算 sha256，所以"看到的字节"与"落盘的字节"
  是同一份；核对不过就删掉半成品，绝不留给驱动去加载。
- **响应体大小按服务端声明的上限熔断**：`X-Artifact-Size` 缺失时按累计字节数熔断，
  不给对端一个"无限写满设备磁盘"的通道。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TOKEN_HEADER = "X-Agent-Token"  # noqa: S105 头名，不是凭据
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024


class AgentClientError(RuntimeError):
    """控制面返回了非 2xx，或线协议读数与预期不符。"""

    def __init__(self, status: int | None, url: str, detail: str) -> None:
        self.status = status
        self.url = url
        self.detail = detail
        super().__init__(f"{status if status is not None else 'transport'} {url}: {detail}")


class ArtifactIntegrityError(AgentClientError):
    """落盘字节的 sha256 ≠ 部署记录里的期望值：产物被换过或传坏了。

    抛出时半成品已经被删掉，调用方拿不到路径——**没有**"先留下文件再报错"
    这种中间态，否则下一次运行会加载一份被污染的模型。
    """


@dataclass(frozen=True)
class ArtifactFetch:
    path: Path
    sha256: str
    size_bytes: int


class AgentClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # fail-closed：不接受 file:// 之类的 base_url，否则这个"取件客户端"就变成
        # 任意文件读取器（urlopen 允许的方案集合比直觉宽）。
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(f"server must be http(s)://, got {base_url!r}")
        self._token = token
        self.timeout = timeout
        self.max_artifact_bytes = max_artifact_bytes

    # ------------------------------------------------------------------
    def url(self, path: str) -> str:
        return f"{self.base_url}/api{path}"

    def _open(self, req: Request) -> Any:
        """发请求；把 HTTPError 折成 AgentClientError（保留状态码与服务端 detail）。"""
        try:
            return urlopen(req, timeout=self.timeout)  # noqa: S310 固定 base_url + https/http
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise AgentClientError(exc.code, req.full_url, _detail_of(body)) from exc
        except URLError as exc:
            raise AgentClientError(None, req.full_url, str(exc.reason)) from exc

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {TOKEN_HEADER: self._token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        # 方案集合已在 __init__ 收窄到 http/https，这里不拼用户可控 scheme
        req = Request(self.url(path), data=data, headers=headers, method=method)  # noqa: S310
        resp = self._open(req)
        if resp.status not in (200, 201):
            body = resp.read().decode("utf-8", "replace")
            raise AgentClientError(resp.status, req.full_url, _detail_of(body))
        return resp

    def _json(self, method: str, path: str, payload: dict | None = None) -> Any:
        with self._request(method, path, payload) as resp:
            raw = resp.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentClientError(resp.status, self.url(path), f"non-JSON body: {exc}") from exc

    # ------------------------------------------------------------------
    # 设备侧契约（与 app/routers/edge.py、app/routers/deployments.py 一一对应）
    # ------------------------------------------------------------------
    def heartbeat(self, agent_id: str, device_info: dict | None = None) -> dict:
        return self._json("POST", f"/edge/agents/{agent_id}/heartbeat", {"device_info": device_info or {}})

    def telemetry(self, agent_id: str, kind: str, payload: dict) -> dict:
        return self._json("POST", f"/edge/agents/{agent_id}/telemetry", {"kind": kind, "payload": payload})

    def list_assigned(self, agent_id: str) -> list[dict]:
        body = self._json("GET", f"/edge/agents/{agent_id}/deployments/assigned")
        return list(body) if isinstance(body, list) else []

    def begin(self, agent_id: str, deployment_id: str) -> dict:
        return self._json(
            "POST", f"/edge/agents/{agent_id}/deployments/{deployment_id}/begin", {}
        )

    def report_checksum(self, deployment_id: str, actual_sha256: str) -> dict:
        return self._json(
            "POST",
            f"/deployments/{deployment_id}/report-checksum",
            {"actual_sha256": actual_sha256},
        )

    def fetch_artifact(
        self, deployment_id: str, dest: Path, expected_sha256: str | None = None
    ) -> ArtifactFetch:
        """流式取件 → 落 `dest`（先写 .part 再原子改名），返回**实际**摘要。

        `expected_sha256` 应由调用方传**部署记录里的 checksum**（那是登记产物时
        算的），服务端头里的 `X-Artifact-Sha256` 只作交叉核对：摘要的权威是那条
        记录，不是同一个响应的另一个字段——两者若不一致，说明服务端自己就乱了。
        不匹配（或交叉核对不过）时删除半成品并抛 `ArtifactIntegrityError`。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        written = 0
        advertised: str | None = None
        try:
            with self._request("GET", f"/deployments/{deployment_id}/artifact") as resp:
                advertised = resp.headers.get("X-Artifact-Sha256")
                declared = _int_or_none(resp.headers.get("X-Artifact-Size"))
                if declared is not None and declared > self.max_artifact_bytes:
                    raise AgentClientError(
                        resp.status, resp.url or "", f"artifact too large: {declared} bytes"
                    )
                with partial.open("wb") as fh:
                    while True:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > self.max_artifact_bytes:
                            raise AgentClientError(
                                resp.status,
                                resp.url or "",
                                f"artifact exceeds {self.max_artifact_bytes} bytes",
                            )
                        digest.update(chunk)
                        fh.write(chunk)
            actual = digest.hexdigest()
            if advertised is not None and advertised != actual:
                raise ArtifactIntegrityError(
                    resp.status,
                    resp.url or "",
                    f"body {actual} != advertised X-Artifact-Sha256 {advertised}",
                )
            if expected_sha256 is not None and expected_sha256 != actual:
                raise ArtifactIntegrityError(
                    None, str(dest), f"body {actual} != deployment checksum {expected_sha256}"
                )
            partial.replace(dest)  # 原子改名：驱动永远看不到半成品
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return ArtifactFetch(path=dest, sha256=actual, size_bytes=written)


def _int_or_none(value: str | None) -> int | None:
    """响应头是外部输入：非数字就当没写，不能让一个坏头把取件变成 ValueError。"""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _detail_of(body: str) -> str:
    """FastAPI 的错误体是 {"detail": ...}；取不到就原样截断（不返回整页内容）。"""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body[:200] or "empty body"
    if isinstance(parsed, dict) and "detail" in parsed:
        return str(parsed["detail"])[:200]
    return body[:200] or "empty body"
