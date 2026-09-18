# 后台/运维增强 — 实施任务清单

Feature Name: admin-ops-enhancements
Updated: 2026-09-18

## 任务列表

### 用户会话管理

- [x] `sessions` 补列 `ip` / `user_agent`；`admin_alerts` 补列 `target`
- [x] `store.create_session` 支持 ip/user_agent；`accounts.start_session` 透传
- [x] `_login_response` 写入 `_client_ip` 与 User-Agent
- [x] `store.list_user_sessions`（只返回 token 前缀）/ `revoke_user_session`
- [x] `GET /api/admin/users/{id}/sessions`
- [x] `POST /api/admin/users/{id}/sessions/{prefix}/revoke`
- [x] `POST /api/admin/users/{id}/sessions/revoke-all`
- [x] `POST /api/admin/users/{id}/totp/reset`（写 `security.reset_totp`，会话下线写 `security.session_revoke`）

### 应用日志在线查看

- [x] `observability.setup_logging` 增加 `RotatingFileHandler`（`PERSONA_DATA_DIR/logs/nian.log`，5MB×3）
- [x] `log_file_path` / `read_log_tail`（最低级别 + 关键词过滤，缺失文件返回空）
- [x] `GET /api/admin/system/logs`（只读，limit 上限 1000）

### 告警中心增强

- [x] `store.add_admin_alert` 支持 `target`；`list_admin_alerts` 支持 `kind` / `status`
- [x] `store.mark_admin_alert_read`
- [x] `_notify_admins` 透传 target，调用点补 `user:{id}` / `report:{id}`
- [x] `GET /api/admin/alerts?kind=&status=` 与 `POST /api/admin/alerts/{id}/read`
- [x] 前端：类型/已读筛选、逐条已读、关联对象跳转

### 导出与批量操作

- [x] `store.export_redemption_codes` / `batch_void_redemption_codes`（跳过非 unused）
- [x] `GET /api/admin/redemption-codes/export`（CSV + UTF-8 BOM）
- [x] `POST /api/admin/redemption-codes/batch-void`（多选或按筛选全选，写 `redemption.batch_void`）
- [x] 前端：复选/全选、导出、批量作废二次确认

### 前端交互

- [x] 用户详情新增「登录会话」区块与全部下线/重置双因素按钮
- [x] 系统分区新增「应用日志」卡片（级别/关键词/刷新）
- [x] 兑换码表格复选、导出与批量作废按钮
- [x] 危险操作二次确认（`window.confirm` / 弹窗）

### 测试与验证

- [x] 新增 `tests/test_admin_ops_enhancements.py`（11 项）
- [x] `scripts/e2e/run_chain.py` 新增会话/日志/告警/兑换码链路（120/120）
- [x] ruff 全过、全量单测 398/398、后台渲染探针 `ADMIN_RENDER_OK`
- [x] 提交并部署，生产校验

## 部署记录

- 提交：待部署
- 部署时间：待部署
- DB 备份：待记录
