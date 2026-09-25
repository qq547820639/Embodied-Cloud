"""注册/鉴权请求的共享核心。

四个模块里的 `_register` 过去逐字节相同（只差最后那行返回值投影），`_auth` 四处
完全相同：注册契约一变（端点、载荷字段、201 期望、密码长度下限）就得同时改四个
文件，而漏改的那几个只会以"测试通过"的形式继续跑旧契约。

这里只共享**会漂移的那部分**（发请求 + 断状态码）；每个模块保留自己的一行投影，
因为返回 token / token+id / 元组 是用例自己的需要，不是重复。
"""

from fastapi.testclient import TestClient

PASSWORD = "password123"


def register_body(client: TestClient, email: str, username: str) -> dict:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "username": username, "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
