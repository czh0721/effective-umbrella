# Requirements Document

## Introduction

念念 Nian 后台「运营投放」当前仅支持发布公告（标题、正文、起止时间，受众固定为全体）与发送站内通知（正文加手填用户 ID 或全体），缺少受众定向、内容组织能力、模板复用与触达反馈。本轮将运营投放升级为可定向、可复用、可度量的投放工具：管理员可按活跃、付费、新用户、标签或指定账号圈定受众并在发送前看到命中人数；公告支持草稿、时间窗、置顶、类型、优先级与链接并带实时预览；通知支持模板与按收件人渲染的变量；公告与通知均提供送达与已读统计。功能仅对已登录的超级管理员开放，沿用独立管理员会话与审计体系。

## Confirmed Decisions

- 活跃口径：最近 30 天内存在登录记录的账号；新用户口径：注册时间在最近 7 天内的账号。
- 定时发布：公告保存开始与结束时间，用户端按当前时间自动过滤，不引入常驻调度任务。
- 触达统计：包含已读上报，新增公告已读表，用户端拉取生效公告时写入已读记录；通知已读复用 `alerts` 的已读字段。

## Glossary

- **Admin**: 通过独立管理员账号登录后台的超级管理员。
- **Audience_Segment**: 一次投放面向的账号集合，取值包括全体、活跃用户、付费用户、新用户、标签与指定用户。
- **Announcement**: 面向用户端展示的站内公告（`announcements` 表）。
- **Notice**: 写入用户站内提醒列表的一对一消息（`alerts` 表，`kind` 为 `notice`）。
- **Notice_Template**: 可复用的通知正文模板（`notice_templates` 表）。
- **Variable**: 通知正文中的占位符，按收件人账号数据渲染。
- **Active_User**: 最近 30 天内存在登录记录的账号。
- **New_User**: 注册时间在最近 7 天内的账号。
- **Paid_User**: 存在至少一笔订单的账号。
- **Tag**: 管理员为用户设置的标签文本（`users.tags`）。
- **Delivery_Stat**: 一次通知或公告的送达与已读统计。
- **Read_Receipt**: 用户已读某条公告的记录（`announcement_reads` 表）。
- **Audit**: 写入 `admin_audit` 表的管理员操作记录。

## Requirements

### Requirement 1

**User Story:** AS Admin, I want 按细分受众投放并看到命中人数, so that 消息能触达目标人群且避免误发空发。

#### Acceptance Criteria

1. WHEN 用户成功登录, the System SHALL 记录该账号的最近登录时间。
2. WHEN Admin 选择 Audience_Segment, the System SHALL 返回该 Audience_Segment 当前命中的账号数量。
3. WHEN Admin 选择活跃用户, the System SHALL 以最近 30 天内存在登录记录的账号为口径。
4. WHEN Admin 选择新用户, the System SHALL 以注册时间在最近 7 天内的账号为口径。
5. WHEN Admin 选择付费用户, the System SHALL 以存在至少一笔订单的账号为口径。
6. WHEN Admin 选择标签, the System SHALL 以标签文本包含所选标签的账号为口径。
7. WHEN Admin 选择指定用户, the System SHALL 以管理员输入的账号标识集合为口径。
8. WHEN Admin 提交投放, the System SHALL 展示命中账号数量并要求二次确认。
9. IF 命中账号数量为 0, THEN the System SHALL 返回 400 与提示「目标受众为空」。

### Requirement 2

**User Story:** AS Admin, I want 结构化的公告编辑体验, so that 公告可读、可排期、可复用且发布前可核对。

#### Acceptance Criteria

1. WHEN Admin 编辑公告, the System SHALL 提供标题、正文、摘要、类型、优先级、置顶、跳转链接、跳转文案、开始时间、结束时间与发布状态字段。
2. WHEN Admin 编辑开始时间或结束时间, the System SHALL 使用本地日期时间选择控件。
3. WHEN Admin 输入标题或正文, the System SHALL 显示当前字符数与上限。
4. WHEN Admin 填写公告内容, the System SHALL 展示与用户端一致的预览。
5. WHEN Admin 保存公告为草稿, the System SHALL 保存内容并对用户端隐藏。
6. WHEN Admin 编辑已存在的公告, the System SHALL 更新该公告内容并保留创建时间。
7. WHEN Admin 提交公告, the System SHALL 校验标题非空且结束时间不早于开始时间。
8. IF 校验未通过, THEN the System SHALL 返回 400 与对应字段的提示。

### Requirement 3

**User Story:** AS Admin, I want 公告按时间窗自动生效, so that 排期投放无需人工值守。

#### Acceptance Criteria

1. WHEN 用户端请求生效公告, the System SHALL 仅返回发布状态、启用状态且当前时间处于时间窗内的公告。
2. WHEN 用户端请求生效公告, the System SHALL 仅返回 Audience_Segment 包含当前账号的公告。
3. WHILE 公告处于草稿状态, the System SHALL 对用户端保持隐藏。
4. WHEN 公告开始时间晚于当前时间, the System SHALL 在后台将该公告标注为未开始。
5. WHEN 公告结束时间早于当前时间, the System SHALL 在后台将该公告标注为已结束。
6. WHEN 公告处于时间窗内且启用, the System SHALL 在后台将该公告标注为进行中。
7. WHILE 公告处于停发状态, the System SHALL 对用户端保持隐藏并在后台标注为已停发。
8. WHEN 用户端展示公告, the System SHALL 按置顶优先、发布时间倒序排列。

### Requirement 4

**User Story:** AS Admin, I want 通知模板与变量, so that 重复运营文案可复用且千人千面。

#### Acceptance Criteria

1. WHEN Admin 管理 Notice_Template, the System SHALL 支持创建、编辑与删除模板，并保存名称与正文。
2. WHEN Admin 选择某个 Notice_Template, the System SHALL 将该模板正文填充到通知内容。
3. WHEN 通知正文包含 Variable, the System SHALL 按收件人渲染用户名、昵称、念念币、积分与蒸馏券。
4. WHEN Admin 插入 Variable, the System SHALL 提供可用 Variable 列表。
5. WHEN Admin 预览通知, the System SHALL 展示按示例账号渲染后的正文。
6. IF 正文中的占位符无法识别, THEN the System SHALL 原样保留该占位符。

### Requirement 5

**User Story:** AS Admin, I want 查看通知发送历史与送达统计, so that 能评估投放效果并复盘文案。

#### Acceptance Criteria

1. WHEN Admin 发送通知, the System SHALL 记录一条发送历史，包含发送人、Audience_Segment、正文、命中人数与发送时间。
2. WHEN Admin 查看通知历史, the System SHALL 按发送时间倒序展示发送记录。
3. WHEN Admin 查看某条通知, the System SHALL 展示送达人数、已读人数与未读人数。
4. WHEN 用户读取站内通知, the System SHALL 将该用户的对应送达记录标记为已读。
5. WHEN Admin 重新查看通知历史, the System SHALL 返回最新的送达与已读计数。

### Requirement 6

**User Story:** AS Admin, I want 查看公告触达明细, so that 能确认公告是否真正被看到。

#### Acceptance Criteria

1. WHEN 用户端拉取生效公告, the System SHALL 为当前账号写入该公告的 Read_Receipt。
2. WHEN Admin 查看某条公告, the System SHALL 展示覆盖人数、已读人数与未读人数。
3. WHEN Admin 展开公告触达明细, the System SHALL 分页展示已读与未读账号名单。
4. IF 公告处于草稿状态, THEN the System SHALL 返回空的触达统计。
5. WHEN 同一账号重复拉取同一条公告, the System SHALL 保留首次已读时间且不产生重复记录。

### Requirement 7

**User Story:** AS Admin, I want 运营投放操作受权限与审计约束, so that 误操作可追溯且接口不可越权访问。

#### Acceptance Criteria

1. WHEN 非管理员访问运营投放接口, the System SHALL 返回 401。
2. WHEN Admin 创建公告, the System SHALL 写入 action 为 `announcement.create` 的 Audit 记录。
3. WHEN Admin 更新公告, the System SHALL 写入 action 为 `announcement.update` 的 Audit 记录。
4. WHEN Admin 发送通知, the System SHALL 写入 action 为 `notice.broadcast` 的 Audit 记录。
5. WHEN Admin 创建、更新或删除 Notice_Template, the System SHALL 写入 action 为 `notice_template.create`、`notice_template.update` 或 `notice_template.delete` 的 Audit 记录。
6. WHEN Admin 触发发送、批量操作或删除, the System SHALL 在界面要求二次确认。

### Requirement 8

**User Story:** AS Admin, I want 升级后旧数据与旧行为保持可用, so that 上线不影响既有用户与公告。

#### Acceptance Criteria

1. WHEN 升级完成, the System SHALL 保留既有公告的可读性与停发能力。
2. WHEN Admin 未选择 Audience_Segment 就发送通知, the System SHALL 以全体活跃账号为默认受众。
3. WHEN 通知渲染 Variable, the System SHALL 对每个收件人使用其自身账号数据。
4. WHEN 升级完成, the System SHALL 保持既有管理员会话、审计记录与用户接口返回结构兼容。
