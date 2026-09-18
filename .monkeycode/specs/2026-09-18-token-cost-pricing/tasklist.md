# Token 计量与套餐定价 实施清单

- Feature: `token-cost-pricing`
- 日期: 2026-09-18
- 状态: 已实现并部署，真实成本核对待生产流量

## 已完成

- [x] 新增 `ex_persona/pricing.py`：定价常量与 `estimate_cost`。
- [x] 新增 `ex_persona/metering.py`：`bind` / `record` / `read_usage` / `current`。
- [x] `store.py`：新增 `token_usage` 表与索引、`record_token_usage`、`token_usage_stats`
      （含平台口径拆分）。
- [x] `llm.py`：`chat_with_meta` 采集 `response.usage` 并落库。
- [x] `webapp.py`：聊天 / 记忆抽取 / 朋友圈 / 蒸馏接入 `metering.bind`。
- [x] `webapp.py`：平台模型 `max_tokens=512`；`admin_summary` 暴露 `token_usage`；
      新增 `GET /api/admin/model-cost`。
- [x] `agent.py`：系统提示稳定前缀前置；`top_k` 4 → 2；说话样本 6 → 3。
- [x] `web/admin.html`：概览显示今日 token / 估算成本 / 缓存命中。
- [x] 新增 `tests/test_token_usage.py`（8 项）。
- [x] 套餐板切到 30/90/365 天各三档（`PACKAGE_TIERS`）与一次性迁移
      `_migrate_package_tiers`。
- [x] 新增 `tests/test_package_tiers.py`（档位、60% 毛利、后台修改保留）。
- [x] e2e 增加套餐板 9 档、旧套餐下架、毛利约 60% 断言。
- [x] 部署并核对生产套餐板：`体验包` 保留，旧 `标准包` / `尊享包` 下架，9 档 active。
- [x] 入门体验包对齐为 10 念念币 / 1000 积分 / 30 天（`_migrate_entry_package`）。

## 待办

- [ ] 收集真实平台模型流量后，用 `token_usage` 与 `/api/admin/model-cost` 校准
      `pricing.py` 的单价常量，确认每轮成本与 60% 毛利成立。
