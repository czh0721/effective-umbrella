# Requirements Document

## Introduction

念念 Nian 后台已具备看板、用户、订单、内容审核、运营投放、系统运维、管理员与审计能力。本轮补齐四类后台/运维增强，让管理员能处理会话与账号安全、在线排障、集中处理告警，并对导出与批量操作提效。功能仅对已登录的超级管理员开放，沿用独立管理员会话与审计体系。

## Confirmed Decisions

- 日志来源：服务新增滚动日志文件（`RotatingFileHandler`，写入 `PERSONA_DATA_DIR/logs`），后台读取尾部；不依赖 systemd。
- 会话管理范围：用户详情内展示会话列表；支持单条下线、全部下线、以及重置该用户双因素。
- 批量作废方式：列表多选 + 按当前筛选全选；已兑换的兑换码自动跳过并返回计数。

## Glossary

- **Admin**: 通过独立管理员账号登录后台的超级管理员。
- **Session**: 用户登录后签发的会话记录（`sessions` 表），用于访问用户端接口。
- **Application_Log**: 服务进程输出的单行 JSON 结构化日志。
- **Alert**: 系统写入 `admin_alerts` 表、面向管理员的提醒记录。
- **Redemption_Code**: 可兑换念念币的兑换码（`redemption_codes` 表）。
- **Audit**: 写入 `admin_audit` 表的管理员操作记录。
- **CSV_Export**: 带 UTF-8 BOM 的逗号分隔文件，浏览器直接下载。

## Requirements

### Requirement 1

**User Story:** AS Admin, I want 查看指定用户的登录会话并强制下线, so that 被盗号或异常登录可被及时阻断。

#### Acceptance Criteria

1. WHEN Admin 请求用户会话列表, the System SHALL 返回每条会话的会话标识前缀、客户端 IP、User-Agent、创建时间、到期时间与是否处于待验证状态。
2. WHEN Admin 对某条会话触发强制下线, the System SHALL 立即使该会话失效。
3. WHEN Admin 对某用户触发全部下线, the System SHALL 立即使用户的全部会话失效。
4. WHEN Admin 完成强制下线, the System SHALL 写入一条 action 为 `security.session_revoke` 的 Audit 记录。
5. IF 会话已过期, THEN the System SHALL 不在会话列表中返回该会话。

### Requirement 2

**User Story:** AS Admin, I want 重置用户双因素, so that 用户丢失验证器时可恢复登录。

#### Acceptance Criteria

1. WHEN Admin 对已开启双因素的用户触发重置双因素, the System SHALL 清空该用户的 TOTP 密钥与启用状态。
2. WHEN Admin 完成重置双因素, the System SHALL 写入一条 action 为 `security.reset_totp` 的 Audit 记录。
3. IF 目标用户未开启双因素, THEN the System SHALL 返回 400 与提示「该用户未开启双因素」。

### Requirement 3

**User Story:** AS Admin, I want 在线查看应用日志, so that 无需登录服务器即可定位问题。

#### Acceptance Criteria

1. WHEN 服务进程启动, the System SHALL 将结构化日志同时写入本地日志文件并保留滚动后的历史文件。
2. WHEN Admin 打开日志视图, the System SHALL 返回最近若干行日志并按时间倒序排列。
3. WHEN Admin 指定最低日志级别, the System SHALL 仅返回该级别及更严重级别的日志。
4. WHEN Admin 输入关键词, the System SHALL 仅返回消息或结构化字段包含该关键词的日志。
5. IF 日志文件不存在或为空, THEN the System SHALL 返回空列表。
6. WHILE Admin 查看日志, the System SHALL 仅提供只读访问，不提供修改、删除或清空操作。

### Requirement 4

**User Story:** AS Admin, I want 集中查看并处理告警, so that 异常可被逐条跟进而不是只看未读。

#### Acceptance Criteria

1. WHEN Admin 打开告警中心, the System SHALL 返回告警列表并支持按类型与已读状态筛选。
2. WHEN Admin 将单条告警标记为已读, the System SHALL 仅更新该条告警的已读时间。
3. WHEN Admin 触发全部已读, the System SHALL 更新当前 Admin 的全部未读告警。
4. WHEN 告警关联到用户、任务或绑定, the System SHALL 在列表中提供对应对象的标识。
5. IF 当前无符合筛选条件的告警, THEN the System SHALL 展示空状态。

### Requirement 5

**User Story:** AS Admin, I want 导出当前筛选下的完整数据, so that 运营对账不遗漏分页之外的数据。

#### Acceptance Criteria

1. WHEN Admin 导出兑换码, the System SHALL 按当前状态筛选输出 CSV_Export，字段包含兑换码、念念币面额、状态、批次、备注、兑换用户与时间。
2. WHEN Admin 导出订单, the System SHALL 导出全部匹配记录，不受列表分页上限限制。
3. WHEN Admin 导出审计, the System SHALL 按当前操作类型与关键词筛选导出 CSV_Export。
4. WHEN Admin 导出用户, the System SHALL 按当前关键词导出 CSV_Export。
5. IF 筛选结果为空, THEN the System SHALL 仍返回仅含表头的 CSV_Export 文件。

### Requirement 6

**User Story:** AS Admin, I want 批量作废兑换码, so that 错误发放可被快速回收。

#### Acceptance Criteria

1. WHEN Admin 提交批量作废请求, the System SHALL 将被选中的未使用兑换码状态置为 `void`。
2. WHEN Admin 触发按当前筛选全选并作废, the System SHALL 对当前筛选结果中的未使用兑换码执行作废。
2. WHEN 批量作废包含已兑换兑换码, the System SHALL 跳过该兑换码并继续处理其余兑换码。
3. WHEN 批量作废完成, the System SHALL 返回成功数量与跳过数量。
4. WHEN Admin 完成批量作废, the System SHALL 写入一条 action 为 `redemption.batch_void` 的 Audit 记录，detail 包含成功与跳过数量。
5. IF 批量作废未选中任何兑换码, THEN the System SHALL 返回 400 与提示「请至少选择一个兑换码」。

### Requirement 7

**User Story:** AS Admin, I want 危险操作有二次确认, so that 误操作不会影响用户。

#### Acceptance Criteria

1. WHEN Admin 触发强制下线、重置双因素或批量作废, the System SHALL 先展示包含对象数量或名称的确认对话框。
2. IF Admin 取消确认对话框, THEN the System SHALL 不发送任何写请求。

### Requirement 8

**User Story:** AS Admin, I want 四类增强沿用既有权限与审计, so that 权限边界与可追溯性保持一致。

#### Acceptance Criteria

1. WHEN 未登录或会话失效时访问任一新增后台接口, the System SHALL 返回 401。
2. WHILE `admin_force_totp` 开关开启且 Admin 未完成双因素, the System SHALL 对新增后台接口返回 403。
3. WHEN Admin 通过新增接口执行写操作, the System SHALL 写入对应的 Audit 记录。
