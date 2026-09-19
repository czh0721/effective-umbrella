# 分身自定义头像上传 实施任务清单

- Feature: `persona-avatar-upload`
- 日期: 2026-09-19

## T1 服务端头像校验

- [x] `ex_persona/webapp.py`：新增 `_validate_avatar(value)`，非空须以 `data:image/` 开头且长度 ≤ 400,000，否则 400。
- [x] `update_persona` 写入 `fields["avatar"]` 前调用 `_validate_avatar`，空串允许（清除）。
- [x] 复用 `_require_persona` 保证仅本人分身（404）与登录（401）。

## T2 前端共享助手

- [x] `web/static/app.js`：新增 `compressAvatarFile(file)`（256×256 JPEG, 0.86）。
- [x] `web/static/app.js`：新增 `avatarImgMarkup(avatar)`，非空返回转义后的 `<img>`。
- [x] `web/settings.html`：`readAvatar` 改调 `compressAvatarFile`，行为不变。

## T3 编辑人设弹层头像入口

- [x] `web/agent.html` `openEdit()`：顶部头像区（点击上传 + 预览 + 恢复默认）。
- [x] 保存时若选择新头像则随 PATCH 提交；「恢复默认」提交空串清除。

## T4 头像位渲染

- [x] `web/agent.html` 工作台头部：有头像渲染图片，无头像渐变首字。
- [x] `web/app.html` 分身卡片：同上。
- [x] `web/moments.html` 动态头部：同上，并新增 `.moments-avatar img` 样式。

## T5 测试

- [x] 新增 `tests/test_persona_avatar.py`：合法更新、非 image 前缀 400、超长 400、空串清除、非本人 404、未登录 401。
- [x] 前端契约测试：`web/agent.html` 含「点击更换头像」；三处头像位页面含 `avatarImgMarkup`。
- [x] `python3 -m ruff check ex_persona tests`、`python3 -m unittest discover -s tests`、`python3 scripts/e2e/run_chain.py` 全绿。

## T6 交付

- [x] 提交并 push `main`。
- [x] 部署生产并核对（编辑人设上传头像后三处展示、清除后回退）。
