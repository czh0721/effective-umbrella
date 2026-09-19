# 蒸馏改用积分支付 实施任务清单

- Feature: `distill-credit-payment`
- 日期: 2026-09-19

## T1 配置与表结构

- [x] `ex_persona/config.py`：`PlatformConfig` 新增 `distill_credit_cost: int = 100`，
      `load_platform_config` 读取该列（缺失回退 100）。
- [x] `ex_persona/store.py`：`platform_config` 建表加 `distill_credit_cost INTEGER NOT NULL
      DEFAULT 100`，`_MIGRATIONS` 加同名列补列。
- [x] `store.set_platform_config(..., distill_credit_cost=None)` 支持写入与规范化。
- [x] 新增 `DISTILL_CREDIT_COST_DEFAULT`、`DISTILL_RESERVE_REASON`、
      `DISTILL_REFUND_REASON`、`DISTILL_CONVERT_REASON`、`store.distill_credit_cost()`。

## T2 积分类预扣/退还

- [x] `store.reserve_distill_credits(user_id, task_id)`。
- [x] `store.refund_distill_credits(user_id, task_id, reason=...)` + `_distill_credit_outstanding`。
- [x] `store.reconcile_distill_credits()`（旧 `reconcile_distill_tickets` 保留薄封装）。

## T3 一次性折算迁移

- [x] `_migrate_distill_credits(conn)`：券余额→积分、中断预扣→积分、套餐 bonus 折算，
      写流水，归零券，`schema_meta: distill_credits_v1`。
- [x] 在 `_ensure()` 迁移序列中排在 `_migrate_credit_expiry` 之后、种子之后调用。

## T4 套餐档位

- [x] `PACKAGE_TIERS` 三档尊享 `bonus_tickets` 归零、`bonus_credits` +100/+200/+300。

## T5 接口层

- [x] `_reserve_distill_tickets`/`_refund_...` 改调积分函数，402 文案改积分。
- [x] 注册赠送改发 `gift × cost` 积分。
- [x] `/api/credits` 返回 `distill_credit_cost`，移除券余额。
- [x] `POST /api/distill-tickets/purchase` 返回 410。
- [x] 管理端平台配置读写支持 `distill_credit_cost`。
- [x] `reconcile_distill_tickets` 调用点更新。

## T6 前端

- [x] `web/create_distill.html`：改积分提示与 402 文案，移除购买入口。
- [x] `web/agent.html`：蒸馏 402 文案改积分。
- [x] `web/settings.html`：移除蒸馏券入口与购买。
- [x] `web/admin.html`：平台配置改蒸馏积分单价；套餐赠送改积分。

## T7 测试

- [x] 更新 `tests/test_distill_tickets.py` 为积分口径。
- [x] 更新 `tests/test_package_bonus.py` 期望值。
- [x] 更新 `tests/test_platform.py` 配置字段。
- [x] 新增折算迁移幂等测试与契约测试。
- [x] `ruff check`、`python3 -m unittest discover -s tests`、`scripts/e2e/run_chain.py`。

## T8 部署与验证

- [x] 备份生产 DB，部署代码。
- [x] 生产核对：3 张券折 300 积分、券归零、流水完整、重启不重复。

## 完成定义

- 全部测试通过；生产迁移正确；用户端与管理端无「蒸馏券」入口；发起蒸馏按积分扣费且失败退还。
