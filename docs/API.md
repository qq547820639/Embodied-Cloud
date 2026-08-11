# API 快速参考

- `GET /api/health`
- `GET /api/templates`
- `GET /api/workspaces`
- `POST /api/workspaces`
- `GET /api/workspaces/{id}`
- `GET /api/workspaces/{id}/access` — 返回 IDE URL / 临时 v0.1 密码 / stream 信息
- `POST /api/workspaces/{id}/start`
- `POST /api/workspaces/{id}/stop`
- `DELETE /api/workspaces/{id}`
- `GET /api/usage`

示例：

```bash
curl -X POST http://127.0.0.1:8000/api/workspaces \
  -H 'Content-Type: application/json' \
  -d '{"template_id":"newton-cartpole-smoke","auto_start":true}'
```

> `/access` 在 v0.1 可信单团队模式会返回 code-server 密码。进入多租户 Beta 后必须替换成 OIDC/短期签名 token，不保留“任意 API 调用者都能拿明文密码”的模型。
