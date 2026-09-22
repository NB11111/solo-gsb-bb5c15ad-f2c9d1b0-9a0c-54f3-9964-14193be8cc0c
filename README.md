# 预测封存与按时公开服务（Commit–Reveal）

基于 **FastAPI + PostgreSQL** 的承诺-揭示服务：客户端先提交对预测内容的
SHA-256 承诺（封存），到达揭示期后再公开原文与随机盐。服务端重算哈希进行
核验，**仅首次匹配的揭示生效**，并返回可核验的事件时间线。

## 一条命令启动

```bash
docker compose up --build
```

启动即自动完成：

- 拉取并启动 PostgreSQL 17（数据持久化在 `pgdata` 卷中）；
- 安装 Python 依赖（FastAPI / Uvicorn / psycopg）；
- 自动建表（`CREATE TABLE IF NOT EXISTS events`，无需手动迁移）。

服务地址与端口：

- API 根地址： **http://localhost:8000**
- 交互式文档（Swagger UI）： **http://localhost:8000/docs**
- OpenAPI 描述： **http://localhost:8000/openapi.json**
- 健康检查： **http://localhost:8000/health**

停止服务：`Ctrl+C` 后执行 `docker compose down`（加 `-v` 可同时清空数据）。

## 承诺算法

提交揭示时，服务端严格按以下字节序列重算 SHA-256（与提交阶段一致）：

```
SHA-256( 原文字节长度的 8 字节大端无符号整数
       || 原文的 UTF-8 字节
       || 盐的 UTF-8 字节 )
```

- 长度前缀只计算**原文**的字节数（不含盐、不含前缀自身），固定 8 字节大端；
- 提交承诺时只传十六进制摘要（64 个小写或大写 hex 字符），原文在揭示前不上传。

本地可用 Python 计算承诺：

```python
import hashlib

payload = "BTC 在 2026-10-01 收盘价为 120000"
salt = "r4nd0m-salt-9f3a"

p, s = payload.encode("utf-8"), salt.encode("utf-8")
commitment = hashlib.sha256(len(p).to_bytes(8, "big") + p + s).hexdigest()
print(commitment)
```

## 时间判定规则（服务端 UTC）

所有截止时间均以 **PostgreSQL 服务端时钟（UTC）** 判定，不相信客户端时钟：

| 动作 | 允许区间 |
| --- | --- |
| 创建事件（提交承诺） | 服务端当前时间 **严格早于** `commit_deadline` |
| 提交揭示 | `commit_deadline` ≤ 当前时间 ≤ `reveal_deadline`（两端均含） |

- 创建时若已到/过 `commit_deadline`：返回 `409 commit_deadline_passed`，**不创建事件**。
- 揭示期开始前揭示：`409 reveal_before_commit_deadline`。
- 超过 `reveal_deadline` 揭示：`409 reveal_after_reveal_deadline`，事件保持封存。
- 重复揭示：`409 already_revealed`（无论第二次内容是否匹配）。
- 哈希不匹配：`400 commitment_mismatch`，**不写入原文/盐，不改变任何状态**。
- 必须满足 `reveal_deadline > commit_deadline`，否则请求校验失败 `422`。
- 时间字段接受 ISO 8601；带时区的会换算为 UTC，**不带时区的按 UTC 解释**。

并发揭示通过 `SELECT ... FOR UPDATE` 行锁串行化，保证只有首个匹配揭示生效。

## API

### 1. 创建事件（封存）

`POST /events`

```bash
curl -s -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{
    "commitment_sha256": "<64位十六进制SHA-256>",
    "commit_deadline": "2026-09-22T10:00:00Z",
    "reveal_deadline": "2026-09-22T12:00:00Z"
  }'
```

`201` 响应（封存中，`payload`/`salt` 为 `null`）：

```json
{
  "id": "e1a2…",
  "status": "sealed",
  "commitment_sha256": "…",
  "commitment_algorithm": "SHA-256",
  "commitment_scheme": "sha256(uint64be(utf8_byte_length(payload)) || utf8(payload) || utf8(salt))",
  "created_at": "2026-09-22T09:00:00.123456+00:00",
  "commit_deadline": "2026-09-22T10:00:00+00:00",
  "reveal_deadline": "2026-09-22T12:00:00+00:00",
  "revealed_at": null,
  "payload": null,
  "salt": null,
  "recomputed_commitment_sha256": null,
  "commitment_matches": null,
  "server_time": "2026-09-22T09:00:00.123456+00:00",
  "timeline": [
    {"type": "event_created",        "at": "…", "scheduled": false},
    {"type": "commitment_recorded",  "at": "…", "scheduled": false},
    {"type": "commit_deadline",      "at": "…", "scheduled": true},
    {"type": "reveal_window_opens",  "at": "…", "scheduled": true},
    {"type": "reveal_deadline",      "at": "…", "scheduled": true}
  ]
}
```

### 2. 揭示

`POST /events/{event_id}/reveal`（仅在揭示期内可用）

```bash
curl -s -X POST http://localhost:8000/events/e1a2…/reveal \
  -H 'Content-Type: application/json' \
  -d '{"payload": "BTC 在 2026-10-01 收盘价为 120000", "salt": "r4nd0m-salt-9f3a"}'
```

成功后返回完整事件，`status` 变为 `revealed`，时间线追加 `revealed` 事件，
并直接给出可供第三方独立核验的字段：

- `payload`、`salt`：揭示的原文与盐；
- `recomputed_commitment_sha256`：服务端按方案重算的摘要；
- `commitment_matches`：是否与封存时的承诺一致（成功揭示必为 `true`）。

### 3. 查询事件与时间线

`GET /events/{event_id}`

- 提交截止前 / 揭示期内且尚未揭示：`status = "sealed"`；
- 揭示成功后：`status = "revealed"`；
- 过了 `reveal_deadline` 仍未成功揭示：`status = "expired"`（永久封存）。

时间线 `timeline` 中：

- `scheduled: true` 表示预定时刻（各截止点），`false` 表示实际发生的事实；
- 任何人拿到事件响应，都可用公开的 `payload`/`salt` 自行重算哈希，
  与 `commitment_sha256` 比对，验证公开内容与封存承诺一致。

### 4. 健康检查

`GET /health` → `{"status": "ok"}`（数据库不可用时返回 `503`）。

## 错误格式

所有业务错误统一返回：

```json
{"error": {"code": "commitment_mismatch", "message": "…"}}
```

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `commitment_mismatch` | 重算哈希与承诺不符，状态不变 |
| 404 | `event_not_found` | 事件不存在 |
| 409 | `commit_deadline_passed` | 迟到的提交（创建被拒绝） |
| 409 | `reveal_before_commit_deadline` | 提前揭示 |
| 409 | `reveal_after_reveal_deadline` | 逾期揭示 |
| 409 | `already_revealed` | 重复揭示 |
| 422 | FastAPI 请求校验错误 | 字段格式/截止时间顺序不合法等 |
| 503 | `database_unavailable` | 健康检查时数据库不可用 |

## 本地无容器开发（可选）

```bash
pip install -r requirements.txt
DATABASE_URL=postgresql://seal:seal@localhost:5432/sealed \
  uvicorn app.main:app --reload
```
