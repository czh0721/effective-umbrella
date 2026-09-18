# 运营投放增强 — 实施任务清单

Feature Name: ops-campaign
Updated: 2026-09-18

## 任务列表

### 受众定向

- [x] `SEGMENT_KINDS` / `SEGMENT_LABELS` / `ACTIVE_WINDOW_DAYS` / `NEW_WINDOW_DAYS` 常量
- [x] `users` 补列 `last_login_at`；`touch_user_login`，`_login_response` 与 2FA 完成时写入
- [x] `store.segment_user_ids`（all/active/paid/new/tag/manual，排除 disabled）
- [x] `store.count_segment` 与 `POST /api/admin/audience/count`
- [x] 发送前命中人数预览与二次确认；命中 0 返回 400「目标受众为空」

### 公告结构化编辑与时间窗

- [x] `announcements` 补列 `summary/kind/priority/pinned/link_url/link_text/status/audience_value/audience_count`
- [x] `create_announcement` 扩展字段与 `update_announcement`（PATCH，保留创建时间）
- [x] `admin_list_announcements` 派生状态与覆盖/已读计数；`announcement_state`
- [x] `GET/POST /api/admin/announcements` 与 `PATCH /api/admin/announcements/{id}`
- [x] 标题非空与结束时间校验（400）
- [x] `active_announcements` 支持 `status`、时间窗与受众过滤，置顶优先
- [x] 用户端 `GET /api/announcements` 按受众过滤并写已读回执

### 公告触达统计

- [x] 新表 `announcement_reads`（主键 announcement_id + user_id）
- [x] `record_announcement_reads`（INSERT OR IGNORE，保留首次已读时间）
- [x] `count_announcement_reads` / `reach_stats` / `reach_users`
- [x] `GET /api/admin/announcements/{id}/reach` 与 `/reach/users?state=`

### 通知模板与变量

- [x] 新表 `notice_templates`；`list/create/update/delete_notice_template`
- [x] `NOTICE_VARIABLES` 与 `render_notice_message`（按收件人渲染，未知占位符保留）
- [x] `GET/POST /api/admin/notice-templates`、`PATCH/DELETE /api/admin/notice-templates/{id}`
- [x] `broadcast_notice` 支持 `render` / `notice_id` / 受众分段
- [x] 前端模板选择、变量插入、按收件人渲染开关

### 通知历史与送达统计

- [x] 新表 `admin_notices`；`alerts` 补列 `notice_id`
- [x] `create_admin_notice` / `set_admin_notice_sent` / `notice_stats` / `list_admin_notices`
- [x] `GET /api/admin/notices` 与 `GET /api/admin/notices/{id}`
- [x] 用户读取站内通知后统计已读（复用 `alerts.read_at`）

### 前端交互

- [x] 公告编辑器：类型/优先级/置顶/摘要/链接/`datetime-local`/字符计数/实时预览/草稿
- [x] 公告列表：状态标签、覆盖与已读、触达明细弹窗、编辑与停发
- [x] 通知编辑器：受众下拉、受众参数、模板、变量、人数预览、发送确认
- [x] 模板管理：列表、新建、编辑、删除（二次确认）
- [x] 发送历史：受众、命中、送达、已读、未读、发送人与时间
- [x] 新增样式 `.preview-card` / `.var-btn` / `.feed-acts` 等，移动端无横向溢出

### 测试与验证

- [x] 新增 `tests/test_ops_campaign.py`（12 项）
- [x] `scripts/e2e/run_chain.py` 新增受众/模板/定向公告/触达/历史链路（127/127）
- [x] ruff 全过、全量单测 410/410
- [x] 后台渲染探针 `ADMIN_RENDER_OK`；1440/390 各分区横向溢出均为 0
- [x] 提交并部署，生产校验

## 部署记录

- 提交：`94595a4`（实现）、`3ecec2f`（spec）
- 部署时间：2026-09-18 15:31（TS `20260918_153139`）
- DB 备份：`platform_20260918_153139.db`
- 生产校验：服务 active；`/health` 200；`users.last_login_at`、`announcements` 扩展列、`alerts.notice_id` 迁移完成；`announcement_reads` / `notice_templates` / `admin_notices` 已建；`/api/admin/notice-templates`、`/api/admin/notices`、`/api/admin/announcements/{id}/reach` 未登录均 401；`/admin` 302；`/static/admin.css` 已含新增样式。
