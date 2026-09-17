# Requirements Document

## Introduction

本需求将当前「单实例、单人、单页控制台」的前任人格蒸馏项目，升级为可承载多个独立用户的公开平台，产品形态对标 wxecho.cn（Echo 回声）：对外提供官网落地页，用户登录后进入自己的工作台，上传自己的聊天记录与照片、创建属于自己的人格，并将人格接入自己的微信进行对话。用户之间的数据、人格、模型配置与微信桥接完全隔离。

本轮以用户名与密码承载账号，并预留微信扫码登录（微信开放平台网页登录）接口：当平台配置了开放平台凭据时启用扫码登录，未配置时仅开放用户名密码登录。QQ 渠道列为后续扩展，本轮不实现。

## Glossary

- **平台（Platform）**: 本系统部署的单个服务实例，承载全部注册用户。
- **用户（User）**: 在平台注册并使用平台的独立个体。
- **账号（Account）**: 以用户名与密码标识的一份用户记录；账号在后续可绑定微信身份。
- **会话（Session）**: 用户登录后获得的、在有限时间内有效的访问凭据。
- **工作区（Workspace）**: 归属于单个用户的文件与数据集合，包含素材、人格档案与微信桥接状态。
- **人格（Persona）**: 由一个用户从自己的素材蒸馏得到的、可对话的角色档案。
- **素材（Material）**: 用户上传或粘贴的、用于蒸馏人格的原始内容，包含聊天记录、社交导出、PDF 与照片等。
- **模型配置（Model_Config）**: 某个用户为调用大模型而提供的 API Key、Base URL 与模型名。
- **微信桥接（WeChat_Bridge)**: 平台为单个用户拉起并管理的 weclaw 进程及其微信登录状态。
- **桥接令牌（Bridge_Token）**: 平台为单个人格生成的、用于把微信消息路由到正确用户的随机凭据。
- **管理员（Administrator）**: 拥有跨用户管理权限的特殊用户。

## Requirements

### Requirement 1: 官网落地页

**User Story:** AS 访问者, I want 在官网了解产品并进入平台, so that 我能快速理解这是什么并开始创建自己的人格。

#### Acceptance Criteria

1. WHEN 访问者打开站点根路径, THE Platform SHALL 展示包含产品名、一句话定位与进入平台入口的官网页面。
2. WHEN 访问者在官网点击进入平台, THE Platform SHALL 进入登录页或已登录时的工作台。
3. THE Platform SHALL 在官网展示与「用 AI 还原记忆中的那个人」一致的文案与视觉风格。

### Requirement 2: 账号注册与登录

**User Story:** AS 访问者, I want 使用用户名和密码注册并登录, so that 我可以拥有并访问属于自己的人的空间。

#### Acceptance Criteria

1. WHEN 访问者提交一个未被占用的用户名与符合密码策略的密码, THE Platform SHALL 创建账号并为该账号初始化空工作区。
2. WHEN 访问者提交的用户名已被占用, THE Platform SHALL 拒绝注册并返回明确的冲突提示。
3. IF 访问者提交的密码长度小于 8 个字符, THEN THE Platform SHALL 拒绝注册并说明密码策略。
4. WHEN 用户提交正确的用户名与密码, THE Platform SHALL 签发会话并将用户跳转到工作台。
5. WHEN 用户提交错误的用户名或密码, THE Platform SHALL 拒绝登录并返回不区分具体原因的统一错误提示。
6. THE Platform SHALL 以加盐的单向哈希保存密码，并确保明文密码不写入任何持久化介质。
7. WHEN 访问者提交的用户名长度小于 3 个字符或包含空白字符, THE Platform SHALL 拒绝注册并说明用户名规则。

### Requirement 3: 会话与登出

**User Story:** AS 登录用户, I want 登录状态可控且会过期, so that 我的账号在公用设备上也相对安全。

#### Acceptance Criteria

1. WHEN 用户完成登录, THE Platform SHALL 签发一个有效期为 7 天的会话并向客户端下发会话 Cookie。
2. WHEN 用户点击登出, THE Platform SHALL 立即失效当前会话并使会话 Cookie 过期。
3. WHILE 会话未过期, THE Platform SHALL 允许携带该会话的请求访问该用户的工作区。
4. IF 携带的会话已过期或被登出, THEN THE Platform SHALL 返回未认证状态并清除会话 Cookie。
5. IF 同一来源连续 5 次登录失败, THEN THE Platform SHALL 在 10 分钟内拒绝该来源的后续登录尝试。

### Requirement 4: 工作区隔离

**User Story:** AS 用户, I want 我的素材与人格只属于我, so that 其他用户无法查看或修改我的数据。

#### Acceptance Criteria

1. THE Platform SHALL 为每个用户在独立目录下保存该用户的素材、人格档案与微信桥接状态。
2. WHEN 任一用户发起的请求访问工作区资源, THE Platform SHALL 校验该资源归属于当前会话用户。
3. IF 用户请求的资源归属于其他用户, THEN THE Platform SHALL 返回未授权状态且不泄露资源是否存在。
4. WHEN 用户上传包含文件路径或文件名的素材, THE Platform SHALL 在写入前规范化路径，并确保写入位置位于该用户工作区目录内。
5. THE Platform SHALL 为每个用户存储相互独立的模型配置与桥接令牌。

### Requirement 5: 人格蒸馏与管理

**User Story:** AS 用户, I want 用自己的素材蒸馏人格, so that 我可以得到一个像 ta 的角色。

#### Acceptance Criteria

1. WHEN 用户上传一个或多个受支持的素材文件, THE Platform SHALL 将该批素材保存到该用户的工作区并返回逐文件的保存结果。
2. WHEN 用户提交一段粘贴文本, THE Platform SHALL 将粘贴文本作为素材保存到该用户的工作区。
3. WHEN 用户发起蒸馏且目标工作区存在素材, THE Platform SHALL 解析素材、识别目标对象、生成风格统计与人格档案，并写入该用户的人格目录。
4. WHEN 用户提供复刻对象的昵称, THE Platform SHALL 在蒸馏时按该昵称选择目标消息。
5. THE Platform SHALL 支持同一用户管理一个或多个人格，并将其中一个人格标记为当前激活人格。
6. IF 用户发起的蒸馏缺少可用素材, THEN THE Platform SHALL 返回明确错误提示且不修改该用户已有的人格档案。
7. WHEN 蒸馏完成, THE Platform SHALL 向用户返回素材消息总数与识别到的目标对象。
8. IF 用户未配置可用的模型配置, THEN THE Platform SHALL 仅生成风格统计画像，并提示用户配置模型后可重新蒸馏。

### Requirement 6: 模型配置

**User Story:** AS 用户, I want 配置自己的大模型 Key, so that 平台用我的额度为我的角色生成回复。

#### Acceptance Criteria

1. WHEN 用户提交 API Key、Base URL 与模型名, THE Platform SHALL 将该模型配置关联到该用户并持久化。
2. THE Platform SHALL 使用平台主密钥对 API Key 加密后存储。
3. WHEN 用户读取模型配置, THE Platform SHALL 返回脱敏后的 API Key，并保持其他字段可见。
4. THE Platform SHALL 在调用大模型时使用当前激活人格所属用户的模型配置。
5. IF 用户在未提供 API Key 的情况下发起对话, THEN THE Platform SHALL 返回明确的配置缺失提示。

### Requirement 7: 微信绑定

**User Story:** AS 用户, I want 绑定我自己的微信, so that 我的角色能通过我的微信与我的联系人对话。

#### Acceptance Criteria

1. WHEN 用户请求获取绑定二维码, THE Platform SHALL 以该用户独立的运行环境启动 weclaw 登录流程并返回二维码图像。
2. WHILE 用户的微信桥接处于运行状态, THE Platform SHALL 保持该用户独立且持续运行的 weclaw 进程。
3. WHEN 用户请求启动桥接, THE Platform SHALL 启动该用户的 weclaw 进程并把该用户的微信消息转发到该用户当前激活的人格。
4. WHEN 用户请求停止桥接, THE Platform SHALL 终止该用户的 weclaw 进程并保留其登录状态。
5. WHEN 用户查询桥接状态, THE Platform SHALL 返回该用户桥接的运行状态与登录阶段。
6. IF 其他用户未拥有该桥接，THEN THE Platform SHALL 拒绝对该桥接的启动、停止与查询操作。
7. THE Platform SHALL 为每个用户分配独立的 weclaw 配置目录与账号存储目录。
8. WHEN 微信消息到达某个用户的桥接, THE Platform SHALL 依据该用户的桥接令牌将消息路由到该用户当前激活的人格。

### Requirement 8: 多页面控制台

**User Story:** AS 用户, I want 在相互独立的页面完成登录、训练、设置与绑定, so that 每一步都清晰而专注。

#### Acceptance Criteria

1. THE Platform SHALL 提供独立的登录/注册页面、工作台页面、人格训练页面、模型设置页面与微信绑定页面。
2. WHEN 未登录用户访问受保护页面, THE Platform SHALL 将其重定向到登录页面。
3. WHEN 用户登录成功, THE Platform SHALL 跳转到该用户的工作台页面。
4. THE Platform SHALL 在页面间共享同一套视觉规范并保持导航一致。
5. WHEN 用户在页面中执行操作, THE Platform SHALL 仅展示属于该用户的状态与数据。

### Requirement 9: 数据导出与注销

**User Story:** AS 用户, I want 导出或清除我的数据, so that 我能掌控自己的隐私。

#### Acceptance Criteria

1. WHEN 用户请求导出人格档案, THE Platform SHALL 打包该用户的人格档案并返回下载。
2. WHEN 用户确认注销账号, THE Platform SHALL 终止该用户的微信桥接、清除该用户的会话并移除该用户的账号凭据。
3. IF 用户注销账号, THEN THE Platform SHALL 在移除账号前再次校验当前密码。
4. WHILE 用户注销流程尚未确认, THE Platform SHALL 保留该用户的数据。

### Requirement 10: 安全与滥用防护

**User Story:** AS 平台运营者, I want 平台具备基础安全防护, so that 多人使用时的风险可控。

#### Acceptance Criteria

1. THE Platform SHALL 对所有写操作校验会话用户与该资源的归属关系。
2. THE Platform SHALL 对单位时间内来自单一来源的登录请求与素材上传请求实施速率限制。
3. THE Platform SHALL 在接受素材上传时限制单文件大小与单次上传文件数量。
4. THE Platform SHALL 在响应中避免输出密码哈希、API Key 明文与桥接令牌。
5. WHEN 平台通过 HTTP 对外提供服务, THE Platform SHALL 将会话 Cookie 标记为 HttpOnly 与 SameSite=Lax。
6. IF 平台通过 HTTPS 对外提供服务, THEN THE Platform SHALL 将会话 Cookie 标记为 Secure。

### Requirement 11: 运维与并发

**User Story:** AS 平台运营者, I want 平台稳定承载多个在线用户, so that 服务可持续运行。

#### Acceptance Criteria

1. THE Platform SHALL 支持至少 100 个注册用户与 20 个同时在线用户。
2. WHEN 一个用户的微信桥接异常退出, THE Platform SHALL 更新该用户的桥接状态并支持该用户重新启动。
3. WHILE 多个用户的桥接同时运行, THE Platform SHALL 使每个桥接仅写入该用户的工作区。
4. THE Platform SHALL 为每个用户的桥接进程设置运行资源上限。
5. WHEN 平台进程收到关闭信号, THE Platform SHALL 终止全部受管的微信桥接进程。

### Requirement 12: 微信扫码登录（预留）

**User Story:** AS 平台运营者, I want 平台预留微信扫码登录能力, so that 拿到微信开放平台凭据后无需改动架构即可启用。

#### Acceptance Criteria

1. WHILE 平台未配置微信开放平台凭据, THE Platform SHALL 在登录页隐藏扫码入口并保留用户名密码登录。
2. WHILE 平台已配置微信开放平台凭据, THE Platform SHALL 在登录页展示微信扫码登录入口。
3. WHEN 平台接收到有效的微信授权回调, THE Platform SHALL 依据微信身份创建或加载账号并签发会话。
4. IF 微信授权回调的 state 与发起登录时的 state 不一致, THEN THE Platform SHALL 拒绝该次登录。
5. WHEN 访问者使用用户名密码登录且账号尚未绑定微信身份, THE Platform SHALL 保留后续绑定微信身份的入口。

## 后续扩展

- QQ 渠道：在本轮微信渠道稳定后，以同样的桥接令牌与人格路由模型接入 QQ 桥接。
- 更多素材类型：在现有解析器基础上按需扩展。
