# Requirements Document

## Introduction

当前用户可以在「编辑资料」中上传自定义头像，但分身（persona）头像统一使用「渐变色 + 名字首字」，用户无法为分身设置真实照片。本功能让用户在「手动编辑人设」弹层中为本人的分身上传自定义头像，并在所有出现分身头像的位置统一使用该头像。

## Glossary

- **分身（Persona）**：用户创建并拥有的 AI 人格对象，数据存于 `personas` 表。
- **分身头像（Persona Avatar）**：绑定到某个分身的图片数据，以 `data:image/...;base64,...` 形式存储在 `personas.avatar` 列。
- **渐变首字头像（Fallback Avatar）**：分身没有头像数据时展示的默认样式，由 `gradientFor(persona.id)` 渐进色与名字首字组成。
- **用户资料头像（User Avatar）**：绑定到登录账号的图片，与分身头像相互独立。
- **头像位（Avatar Slot）**：页面上展示某个分身头像的圆形或圆角区域，例如工作台顶部、分身列表卡片、朋友圈动态、回忆时间线。
- **头像数据上限（Avatar Limit）**：单张头像 base64 字符串的最大长度，取 400,000 字符，与用户资料头像一致。

## Requirements

### Requirement 1

**User Story:** AS 分身拥有者, I want 在编辑人设时上传自定义头像, so that 分身看起来更像真实的那个人。

#### Acceptance Criteria

1. WHEN 用户打开某个分身的「手动编辑人设」弹层, the 系统 SHALL 在弹层顶部展示该分身当前头像与「点击更换头像」提示。
2. WHEN 用户点击弹层顶部头像并选择一张本地图片, the 系统 SHALL 在浏览器本地把图片居中裁剪并压缩为 256×256 的 JPEG 数据。
3. WHEN 本地压缩完成, the 系统 SHALL 在弹层顶部即时预览新头像。
4. WHEN 用户点击保存且已选择新头像, the 系统 SHALL 通过 `PATCH /api/personas/{persona_id}` 提交头像数据，并在服务端持久化到该分身。
5. IF 图片读取或压缩失败, the 系统 SHALL 提示「图片读取失败，请换一张」并保持该分身原有头像不变。

### Requirement 2

**User Story:** AS 平台运营者, I want 服务端校验分身头像数据, so that 非法或超大的头像不会写入数据库。

#### Acceptance Criteria

1. WHEN `PATCH /api/personas/{persona_id}` 请求携带非空 `avatar` 字段, the 系统 SHALL 校验该字段以 `data:image/` 开头。
2. IF `avatar` 字段非空且不以 `data:image/` 开头, the 系统 SHALL 返回 HTTP 400 并说明头像格式不受支持。
3. IF `avatar` 字段长度超过 400,000 字符, the 系统 SHALL 返回 HTTP 400 并说明头像过大。
4. WHEN `avatar` 字段为空字符串, the 系统 SHALL 清除该分身头像并使其回退为渐变首字头像。

### Requirement 3

**User Story:** AS 分身拥有者, I want 上传的头像在所有位置生效, so that 我在任何页面看到的分身形象保持一致。

#### Acceptance Criteria

1. WHILE 分身存在头像数据, the 系统 SHALL 在头像位展示该图片并按容器形状裁剪（圆形或圆角）。
2. WHILE 分身没有头像数据, the 系统 SHALL 在头像位展示渐变首字头像。
3. WHILE 分身存在头像数据, the 系统 SHALL 在工作台顶部、分身列表、朋友圈动态与回忆时间线四个位置的每一个展示同一张头像。
4. WHEN 用户保存新头像后返回工作台, the 系统 SHALL 展示更新后的头像，无需用户手动刷新页面。

### Requirement 4

**User Story:** AS 分身拥有者, I want 头像操作仅作用于我自己的分身, so that 其他用户的分身头像不被改动。

#### Acceptance Criteria

1. WHEN 用户对不属于自己的分身提交头像更新, the 系统 SHALL 返回 HTTP 404。
2. WHEN 用户未登录提交头像更新, the 系统 SHALL 返回 HTTP 401。
3. WHEN 用户更新本人分身头像, the 系统 SHALL 仅修改该分身记录，并保持其他分身与用户资料头像不变。
