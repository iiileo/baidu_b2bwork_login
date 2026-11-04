# 重复 username、并发请求与幂等重试

## 规则

`POST /v1/sessions` 会在 SQLite 写事务中检查并创建会话。相同 `username` 只允许一个未完成会话（状态为 `queued`、`running`、`waiting_captcha` 或 `waiting_security`）：

- 已存在未完成会话、且本次不是同一幂等请求：返回 HTTP `409 Conflict`，不取消旧会话，也不创建/排入新会话。响应 `detail.code` 为 `active_session_exists`，并包含旧会话的 `session_id` 和 `status`。
- 同一 `username`、同一 `Idempotency-Key` 且请求内容一致：返回原会话（HTTP `202`，`duplicate: true`），不会重复执行登录。旧会话即使已结束，也会在记录保留期间作为幂等结果返回。
- 同一 `username`、同一 `Idempotency-Key` 但请求内容不同：返回 HTTP `409 Conflict`，`detail.code` 为 `idempotency_key_reused`。
- 未提供 `Idempotency-Key` 时，重复的活跃 username 一律返回 `409`。旧会话进入终态后可再次创建。

幂等键限定在 username 范围内；服务端只持久化请求内容的 SHA-256 摘要用于检查键是否被错误复用，不保存额外明文副本。幂等记录随会话记录保留，过期清理后同一键不再保证可重放。

## 并发保障

检查活跃 username 和写入新会话在同一个 `BEGIN IMMEDIATE` SQLite 事务中完成。并发请求会被数据库串行化：一个请求创建成功后，其他不同请求会看到该活跃会话并收到 `409`；同一幂等请求则复用刚创建的会话。数据库的 `(username, idempotency_key)` 唯一索引也会阻止幂等键被重复占用。

## 示例

第一次提交和超时后的安全重试都使用相同 key 与相同请求体：

```bash
curl -i -X POST "$HOST/v1/sessions" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Idempotency-Key: login-order-2026-001" \
  -H "Content-Type: application/json" \
  -d '{"username":"acct","password":"secret","priority":2,"client_ref":"order-001"}'
```

创建成功返回例如：

```json
{
  "id": "a1b2c3...",
  "status": "queued",
  "queue_position": 1,
  "duplicate": false
}
```

使用相同 key 和相同请求体重试，会返回相同 `id`，`duplicate` 为 `true`。不同请求撞到已有活跃 username 时返回例如：

```json
{
  "detail": {
    "code": "active_session_exists",
    "message": "该用户名已有未完成会话",
    "session_id": "a1b2c3...",
    "status": "running"
  }
}
```
