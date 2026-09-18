# 管理后台扩展（P0）实施计划

- [ ] 1. 数据层迁移与字段扩展
  - [ ] 1.1 在 `ex_persona/store.py` 新增 `orders`、`announcements`、`feature_flags` 三张表与索引（设计 Data Models）
    - `orders` 含 `idx_orders_user`、`idx_orders_created`
    - `announcements`、`feature_flags` 按设计字段定义
  - [ ] 1.2 扩展 `users` 的 `note`、`tags`、`status_reason`、`disabled_at` 与 `moments` 的 `hidden`
  - [ ] 1.3 把迁移接入 `init_db`/`_startup`，使用 `schema_meta` 幂等键 `admin_console_v1`
  - [ ]* 1.4 为迁移编写单元测试：重复初始化不报错、目标列与表存在

- [ ] 2. 数据看板聚合与接口
  - [ ] 2.1 实现 `store.dashboard_kpis()`、`store.dashboard_trend(days)`、`store.dashboard_funnel()`（需求 R1–R4）
    - 指标含总用户、今日活跃、总人格、今日对话轮次、今日消耗积分、今日新增、待处理举报
    - 趋势按自然日聚合新增用户、活跃用户、对话轮次、营收，缺失日期补 0，日界按 `Asia/Shanghai`
  - [ ] 2.2 为看板聚合增加不高于 60 秒的 TTL 缓存（需求 R5）
  - [ ] 2.3 新增 `GET /api/admin/dashboard?days=7|30|90`，使用 `Depends(current_admin)`（需求 R1–R4）
  - [ ]* 2.4 单元测试：趋势补零、日期轴连续、转化率、缓存命中

- [ ] 3. 用户管理增强
  - [ ] 3.1 实现 `store.search_users(...)`：用户名搜索、状态/注册时间/积分区间筛选、20 条每页分页并返回总数（需求 R6、R7）
  - [ ] 3.2 实现 `store.user_overview(user_id)` 与 `store.list_user_ledger(user_id, kind, limit)`（需求 R8）
  - [ ] 3.3 实现 `store.set_user_note(user_id, note, tags)`（需求 R9）
  - [ ] 3.4 实现 `store.set_user_status(user_id, status, reason, actor)`：写入理由与停用时间并失效该用户全部会话（需求 R10、R11，性质 I4）
  - [ ] 3.5 接口：`GET /api/admin/users` 支持分页筛选、`GET /api/admin/users/{id}`、`POST /api/admin/users/{id}/note`、`POST /api/admin/users/{id}/status`（需求 R6–R12）
    - 写操作调用 `_audit(admin, ...)`
  - [ ]* 3.6 单元测试：筛选分页、用户详情字段、停用使会话失效、备注写入

- [ ] 4. 订单落库与计费接口
  - [ ] 4.1 实现 `store.record_order(...)`，并在 `purchase_package` 同一事务内调用，沿用幂等键 `package.purchase:{package_id}:{idem}`（需求 R22，性质 I1、I2）
  - [ ] 4.2 实现 `store.list_orders(...)` 与 `store.orders_revenue(days)`（需求 R23、R25）
  - [ ] 4.3 接口：`GET /api/admin/orders`、`GET /api/admin/orders/revenue?days=`（需求 R23、R25）
  - [ ]* 4.4 单元测试：购买写订单、订单积分与批次一致、幂等重试不重复写单、营收聚合

- [ ] 5. 检查点
  - 确保所有测试通过,如有疑问请询问用户

- [ ] 6. 后台布局与玻璃风重构
  - [ ] 6.1 重写 `web/admin.html` 骨架为侧栏 + 分区面板，保留举报/积分配置/审计/系统现有功能，新增概览（看板）、用户、订单分区（需求 R54、R55、R56）
  - [ ] 6.2 重写 `web/static/admin.css` 布局层，复用 `app.css` 令牌（`--glass`、`--v`、`--r-lg`、`--shadow`、`--hairline`），移除深色主题（需求 R55）
  - [ ] 6.3 概览分区渲染 KPI 卡与内联 SVG 趋势图、转化漏斗，数值缺失补 0（需求 R1–R4）
  - [ ] 6.4 用户分区渲染筛选栏、分页列表、用户详情，支持备注与停用/启用操作（需求 R6–R12）
  - [ ] 6.5 订单分区渲染订单列表与营收汇总（需求 R23、R25）
  - [ ] 6.6 实现窄视口抽屉导航，复用 `navToggle`/`navScrim`/`sideClose`（需求 R54）
  - [ ]* 6.7 Playwright 探针：各分区渲染、图表 SVG 存在、抽屉开合、无 JS 报错

- [ ] 7. 检查点
  - 确保所有测试通过,如有疑问请询问用户

---

## P1 实施记录（已完成）

- [x] 内容审核
  - [x] `store.search_personas` / `persona_overview` / `set_persona_status`（软处置，停用同时摘除激活标记，可恢复）
  - [x] `store.admin_list_moments` / `set_moment_hidden`（软删除，可恢复）
  - [x] 用户端 `store.list_moments` 过滤 `hidden = 0`
  - [x] 接口：`GET /api/admin/content/personas`、`GET/POST /api/admin/content/personas/{id}[/status]`、`GET /api/admin/content/moments`、`POST /api/admin/content/moments/{id}/hidden`
  - [x] 被停用的人格在桥接回复入口静默不回复，且用户无法自行重新激活
- [x] 运营投放
  - [x] `store.create_announcement` / `list_announcements` / `active_announcements` / `set_announcement_active` / `broadcast_notice`
  - [x] 接口：`GET/POST /api/admin/announcements`、`POST /api/admin/announcements/{id}/active`、`POST /api/admin/notices`
  - [x] 用户端 `GET /api/announcements`，`app.html` / `settings.html` 顶部展示公告
- [x] 系统运维
  - [x] `store.admin_list_bindings` / `admin_list_tasks` / `retry_task` / `list_backups`（只读）/ `get_feature_flags` / `set_feature_flag` / `feature_flag_enabled`
  - [x] 接口：`/api/admin/system/{health,bindings,tasks,flags,backups}` 与绑定强制下线、任务重试
  - [x] 三个功能开关缺省开启并即时生效：`moments_auto` 停自动发布、`wechat_login` 停扫码登录、`platform_model` 停平台模型回退
- [x] 管理员与审计
  - [x] `store.reset_admin_totp`；审计 `list_audit` 支持操作人/动作/目标/时间筛选
  - [x] 接口：`GET/POST /api/admin/admins`、`POST /api/admin/admins/{id}/{status,password,totp}`、`GET /api/admin/audit?...`
  - [x] 管理员列表与创建响应不下发密码/双因素密钥
- [x] 测试与验证
  - [x] `tests/test_admin_console.py` 扩至 16 项；全量 375 单测通过；`ruff` 通过；e2e 94/94
  - [x] Playwright 后台渲染探针覆盖内容/运营/系统/管理员分区，无 JS 报错
