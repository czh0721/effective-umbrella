# 长期记忆 技术设计

- Feature: `long-term-memory`
- 关联需求: `requirements.md`
- 日期: 2026-09-13

## 1. 架构总览

```
微信消息
  └─ weclaw (patched)  ──POST /v1/chat/completions/{token} {user, messages}──▶ route_chat
                                                                                │
                          ┌─────────────────────────────────────────────────────┤
                          │ contact_key = request.user (from_user_id)           │
                          │ history    = chat_turns[persona, contact] 最近 N 条  │
                          │ memory     = memories[persona, contact] BM25 top-K   │
                          └─────────────────────────────────────────────────────┤
                                                                                │
              PersonaAgent.reply(message, history, extra_context=memory_block) │
                                                                                │
                         后台线程 extract_memories() ──▶ memories 表            │
```

## 2. 数据模型（SQLite）

新增两张表，`chat_turns` 增列：

```sql
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL,
    contact_key TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    last_turn_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(persona_id, contact_key)
);
CREATE INDEX IF NOT EXISTS idx_contacts_persona ON contacts(persona_id, contact_key);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL,
    contact_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'fact',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(persona_id, contact_key, id);

ALTER TABLE chat_turns ADD COLUMN contact TEXT NOT NULL DEFAULT '';
```

- `isolation key = (persona_id, contact_key)`；`user_id` 仅作冗余隔离与查询约束。
- `contacts.last_turn_id` 记录上次抽取到的最新 `chat_turns.id`，用于计算待抽取增量。

## 3. weclaw 补丁

weclaw v0.7.1 的 HTTP agent 只发送 `model` 与 `messages`，`conversationID`
（即 `from_user_id`）被丢弃。补丁在 `agent/http_agent.go` 的请求体中增加 `user` 字段：

```go
reqBody := map[string]interface{}{
    "model":    a.model,
    "messages": messages,
    "user":     conversationID,
}
```

平台端 `OAIRequest.user` 已存在，直接作为 `contact_key`。用 Go 1.25 从源码
`v0.7.1` 重新编译，替换 `/tmp/opencode/weclaw/weclaw`。

## 4. 记忆抽取（`ex_persona/memories.py`）

- 输入：persona 名称、待抽取轮次（user/assistant 原文）、已有记忆文本集合。
- 使用 `llm.chat(config, messages, json_mode=True)`，要求返回
  `{"memories":[{"kind":"fact|preference|event|relation","content":"..."}]}`。
- 解析容错：支持裸数组、对象包裹、代码块围栏；单条截断到 200 字；过滤空串与
  与已有记忆完全相同（归一化后）的项。
- 失败（无 Key、网络异常、JSON 解析失败）返回空列表，不抛出。

```python
def extract(config, persona_name, turns, existing) -> list[dict]
def parse(raw) -> list[dict]
def recall(memories, query, k=6) -> list[dict]
def format_block(items, persona_name) -> str
```

## 5. 对话路由改造（`webapp.route_chat`）

1. `contact_key = (request.user or "").strip()`。
2. 有 `contact_key` 时 `store.touch_contact(...)` 创建/更新联系人。
3. 历史：`store.list_turns(user_id, persona_id, limit=N, contact=contact_key or None)`。
4. 长期记忆：`long_term_memory` 开启且 `contact_key` 非空时：
   - `items = store.list_memories(user_id, persona_id, contact_key)`
   - `hits = memories.recall(items, message)`
   - `extra_context = memories.format_block(hits, persona.name)`
   - 传给 `agent.reply(..., extra_context=extra_context)`。
5. 持久化：`store.add_turn(..., contact=contact_key)`。
6. 回复成功后，若开启长期记忆，提交后台线程
   `_extract_task(user_id, persona, contact_key)`：读取 `last_turn_id` 之后的新轮次，
   达到 `memory_extract_every` 条才抽取；用按 `(persona, contact)` 的锁避免并发重复。

## 6. 记忆管理 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/personas/{id}/memories` | 返回联系人列表（含记忆数）与按联系人分组的记忆 |
| DELETE | `/api/personas/{id}/memories/{memory_id}` | 删除单条记忆 |
| POST | `/api/personas/{id}/memories/clear` | 清空全部或指定联系人的记忆 |
| PATCH | `/api/personas/{id}/contacts/{contact_key}` | 重命名联系人显示名 |

## 7. 设置项

`DEFAULT_SETTINGS["model"]` 增加：

```python
"long_term_memory": True,
"memory_extract_every": 6,   # 2..50
```

`validate()` 收敛取值范围。`memory_mode=none` 时即使开关为真也不注入最近轮次，
但长期记忆独立于此档位。

## 8. 前端

- 新页面 `/app/memory`（`web/memory.html`）：联系人切换 + 记忆卡片列表 + 删除。
- 底部导航新增「记忆」项，图标 `brain`。
- `agent.html` 模型面板的「上下文记忆」区增加长期记忆开关与抽取间隔。

## 9. 兼容与回滚

- `chat_turns.contact` 默认 `''`，历史数据自动归入空联系人维度。
- 关闭 `long_term_memory` 即回到旧行为，无需迁移回滚。
- 单元测试覆盖：表迁移、联系人隔离、抽取解析、召回排序、route_chat 注入与静默。

## 10. 风险

- 抽取会产生额外模型调用成本：通过阈值 + 后台异步 + 去重控制频率。
- 无 `from_user_id` 的调用（如平台内测试）走空联系人维度，与旧行为一致。
