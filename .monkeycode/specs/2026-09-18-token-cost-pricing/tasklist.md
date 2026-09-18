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
- [x] 新增 `scripts/cost_probe.py`，在生产用平台模型实测真实 token 与成本。

## 实测结果（2026-09-18，生产 `deepseek-chat`）

用 `cd /opt/nian && venv/bin/python scripts/cost_probe.py` 对同一段系统提示连续调用，
观察冷启动与缓存命中：

| 系统提示字符 | prompt tokens | 首次成本 | 后续成本 | 缓存命中 |
| --- | --- | --- | --- | --- |
| 4135 | 2690 | ¥0.0055 | ¥0.0005 | 2556/2690 |
| 12000 | 7175 | ¥0.0142 | ¥0.0006 | 7040/7175 |
| 20000 | 11937 | ¥0.0105 | ¥0.0009 | 11776/11937 |

结论：稳定前缀缓存命中率约 98%，二次及以后每轮成本低于 ¥0.001；只有会话首轮为冷启动，
成本随提示长度在 ¥0.005–0.014 之间。平均每轮远低于定价假设的 ¥0.008，¥0.02/轮售价下
60% 毛利成立并有较大余量。

## 待办

- [ ] 用真实平台账单核对 `pricing.py` 的缓存命中单价（现为 ¥0.04/百万）：第三方资料对该项
      报价分歧较大（¥0.04–¥0.7/百万），需要以官方后台数据为准；不影响 60% 毛利结论。
- [ ] 统计记忆抽取（purpose=`memory`）等非聊天调用占比，把每用户消息的综合成本纳入看板。
