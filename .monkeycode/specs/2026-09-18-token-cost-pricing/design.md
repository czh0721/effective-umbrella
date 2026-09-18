# Token 计量与套餐定价 技术设计

- Feature: `token-cost-pricing`
- 日期: 2026-09-18
- 状态: 已实现

## 架构总览

```
LLM 调用 (llm.chat_with_meta)
      │  成功返回 response.usage
      ▼
metering.record  ──►  store.token_usage 表
      ▲                     │
metering.bind(...)          ▼
业务调用点               store.token_usage_stats(since)
（chat/memory/            │
 moment/distill）         ▼
                    pricing.estimate_cost  ──►  后台概览 / GET /api/admin/model-cost
```

## 新增模块

### `ex_persona/pricing.py`

- 定价常量（元/百万 token）：`INPUT_MISS_PER_MILLION=2.0`、`INPUT_HIT_PER_MILLION=0.04`、
  `OUTPUT_PER_MILLION=8.0`。
- `estimate_cost(prompt_tokens, completion_tokens, cached_tokens)`：先按未命中价计算
  prompt，再把命中部分按命中价折算，最后加输出价，返回人民币。

### `ex_persona/metering.py`

- `bind(user_id, persona_id, purpose, contact)`：上下文管理器，用 `ContextVar` 保存当前
  归属；退出时重置。
- `record(config, usage, purpose=None)`：从 `usage` 读取 token 数并落库；`usage` 为空
  直接返回；懒加载 `store` 以避免循环依赖，异常只告警不抛出。
- `read_usage(usage)`：兼容对象与 dict 两种 usage 结构；同时支持
  `prompt_cache_hit_tokens` 与 `prompt_tokens_details.cached_tokens`。
- `current()`：返回当前绑定，供未显式传 purpose 的调用点使用。

## 数据模型

`store.token_usage`：

| 列 | 说明 |
| --- | --- |
| user_id / persona_id / contact | 归属 |
| purpose | `chat` / `memory` / `moment` / `distill` / `unknown` |
| model / platform | 模型名与是否平台模型 |
| prompt_tokens / completion_tokens / cached_tokens / total_tokens | 用量 |
| created_at | 记录时间 |

索引：`idx_token_usage_created`、`idx_token_usage_user`。

## 计量接入点

- `llm.chat_with_meta`：成功后统一 `metering.record(config, response.usage)`。
- `webapp`：
  - 聊天 → `bind(user_id, persona["id"], "chat", contact)`
  - 朋友圈生成 → `bind(..., "moment")`
  - `_extract_task` 记忆抽取 → `bind(..., "memory")`
  - `_run_distill_task` / `api_distill` → `bind(..., "distill")`

## 成本优化

- `build_config` 的平台模型返回 `max_tokens=512`。
- `PersonaAgent.top_k` 默认 4 → 2；说话样本 `retrieve_style(k=3)`。
- `build_system` 顺序：人设 `SKILL` → `CHAT_STYLE_RULES` → 当下时间 → 额外上下文 →
  检索对话 → 说话样本。稳定前缀前置以命中 DeepSeek 前缀缓存。

## 后台视图

- `GET /api/admin/summary` 增加 `token_usage` 字段。
- `GET /api/admin/model-cost?days=7`：管理员专用，返回时间窗内的汇总。
- `store.token_usage_stats(since_iso)` 返回总量、`cost_yuan`、`platform` 子口径、
  `by_purpose` 与 `by_user`。
- 后台概览「平台模型用量」卡片显示今日 token、估算成本与缓存命中。

## 套餐板与迁移

- `store.PACKAGE_TIERS`：9 个档位，元素为
  `(name, credits, coins, badge, sort, validity_days, bonus_credits, bonus_tickets)`；
  `DEFAULT_PACKAGES` 由 9 档派生，`price_cents = coins × 10`。
- 定价：`credits = coins × 100`，每轮 20 积分，成本 ¥0.008/轮，毛利 60%。
- `_migrate_package_tiers(conn)`：以 `schema_meta.package_tiers_v1` 为标记只执行一次；
  按名称 upsert 档位、下架 `LEGACY_PACKAGE_NAMES`（`标准包` / `尊享包`），保留数据与
  历史订单；此后后台改价不会被启动逻辑覆盖。
- `_migrate_retire_entry_package(conn)`：以 `schema_meta.packages_retired_v1` 为标记只
  执行一次，下架 `RETIRED_PACKAGE_NAMES`（`体验包`）。

## 测试

- `tests/test_token_usage.py`：pricing、`read_usage` 兼容、record 增量、bind 归属与重置、
  空 usage 忽略、`chat_with_meta` 落库、系统提示顺序与裁剪。
- `tests/test_package_tiers.py`：档位数据、60% 毛利、后台修改在重 init 后被保留。
- `scripts/e2e/run_chain.py`：套餐板含 9 档、旧套餐下架、毛利约 60%。

## 风险与限制

- 成本为估算：命中价与官方实际结算可能存在差异，需要以真实账单校准。
- 实测（见 `tasklist.md`）：稳定前缀缓存命中率约 98%，二次及以后每轮低于 ¥0.001，会话
  首轮冷启动 ¥0.005–0.014；平均成本低于 ¥0.008/轮，60% 毛利成立。
- `scripts/cost_probe.py` 可直接在生产复测：`cd /opt/nian && venv/bin/python scripts/cost_probe.py`。
