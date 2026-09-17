# 实施任务清单：积分套餐有效期与蒸馏单独购买

- Feature: `credit-expiry-distill`
- 关联: `requirements.md`、`design.md`
- 分支建议: `260917-feat-credit-expiry-distill`

## 阶段 A：存储层（`ex_persona/store.py`）

- [x] A1 `credit_batches` 表 + 两个索引加入 `SCHEMA`。
- [x] A2 `distill_ticket_ledger` 表 + 索引加入 `SCHEMA`。
- [x] A3 `schema_meta` 表加入 `SCHEMA`；实现 `_meta_get` / `_meta_set`。
- [x] A4 `_MIGRATIONS` 扩展：`users.distill_tickets`、`credit_packages.validity_days`、
  `platform_config.default_credit_days/distill_ticket_price/distill_ticket_gift`。
- [x] A5 `_PACKAGE_COLUMNS`（若有）与 `list_credit_packages` / `upsert_credit_package` /
  `get_credit_package` 支持 `validity_days`（含 30/90/365 校验）。
- [x] A6 `platform_config` 读写函数扩展三个新字段（`get/set_platform_config`、
  `load_platform_config`、`PlatformConfig` 数据类）。
- [x] A7 `init_db()` 一次性历史余额清零 + `schema_meta` 标记；存量套餐有效期补齐。
- [x] A8 `grant_credits` 支持 `expires_days/source/package_id`；正数建批次，负数走
  `_consume_batches`。
- [x] A9 实现 `_consume_batches(conn, user_id, amount)`（最早到期优先）。
- [x] A10 实现 `expire_credit_batches(now)` / `remind_expiring_batches(now, window_hours)`。
- [x] A11 `credits_summary` 增加 `next_expiry` / `expiring_soon` / `expired`。
- [x] A12 `purchase_package` 改为按 `validity_days` 建批次并保留幂等。
- [x] A13 蒸馏券：`grant_distill_tickets` / `distill_tickets_summary` /
  `purchase_distill_ticket` / `reserve_distill_ticket` / `refund_distill_ticket` /
  `reconcile_distill_tickets`。
- [x] A14 新增异常 `InsufficientDistillTickets`。
- [x] A15 `list_credit_ledger` 保持兼容；新增 `list_distill_ticket_ledger`。

## 阶段 B：后台巡检（`ex_persona/`）

- [x] B1 新增 `CreditExpiryWorker`（`interval=600s`、可注入 `now`、`start/stop`）。
- [x] B2 `webapp` 启动处启动 worker，关闭处停止；测试不自动启动。

## 阶段 C：接口层（`ex_persona/webapp.py`）

- [x] C1 `_run_distill_task` 成功保留预扣、异常分支幂等退款。
- [x] C2 `create_and_distill` / `redistill` / 重试分支接入 `reserve_distill_ticket`，
  无券 402。
- [x] C3 `POST /api/distill-tickets/purchase`（幂等 `client_id`）。
- [x] C4 `/api/credits` 返回 `batches`、`packages[].validity_days`、`distill_tickets`。
- [x] C5 `PlatformConfigRequest` 三个新字段与校验；`/api/admin/platform` 写入。
- [x] C6 `CreditGrantRequest.credit_days`；`/api/admin/users/{id}/credits` 传递有效期。
- [x] C7 `PackageRequest.validity_days` 校验；`/api/admin/packages` 写入。
- [x] C8 `/api/admin/credits` 增加到期清零汇总。
- [x] C9 新用户注册赠送按 `default_credit_days` 建批次；按 `distill_ticket_gift` 发放券。

## 阶段 D：前端

- [x] D1 `web/settings.html` 钱包页：积分到期行、套餐有效期、蒸馏券区块与购买。
- [x] D2 `web/app.html` 首页积分卡到期提示行。
- [x] D3 创建 / 重蒸馏流程显示券数量与不足引导。
- [x] D4 `web/admin.html`：套餐有效期、平台新字段、发放有效期、到期看板。
- [x] D5 `web/static/app.js` 复用组件与格式化，无 emoji。

## 阶段 E：测试与验证

- [x] E1 `tests/test_credit_expiry.py`：发放/扣减/清零/提醒/迁移/不变量。
- [x] E2 `tests/test_distill_tickets.py`：购买/预扣/退还/幂等/中断对账。
- [x] E3 适配 `tests/test_credits.py`、`tests/test_platform.py` 等既有用例。
- [x] E4 e2e 新增链路并全量通过（`run_chain.py`，63/63）。
- [x] E5 jsdom 探针：钱包页、首页积分卡、创建页券门槛、后台。
- [x] E6 `ruff check ex_persona tests scripts` + 全量单测（342 通过）。
- [ ] E7 部署前备份生产 DB，部署后逐项线上验证（买券扣币、无券 402、到期巡检日志）。
