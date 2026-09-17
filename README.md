# 念念 Nian（ex-persona）

把一个人的微信聊天记录蒸馏成「数字人格」，让 AI 用 ta 的方式跟你聊天，并通过微信 ClawBot 自动收发消息。内置多用户平台：一个服务可为多个人各建一个独立账号与人格，每人绑定自己的微信。

> 仅供学习与个人纪念用途。聊天记录涉及他人隐私，请仅在自己有权处理的范围内使用，不要公开传播。

## 原理

```
多平台素材(txt/csv/json/iMessage/短信/PDF/照片/社交导出)
   │  ingest   解析 + 归一化 + 识别目标对象
   ▼
messages.jsonl
   │  distill  风格统计 + 大模型人格卡片 + 真实对话检索对
   ▼
profile/ (SKILL.md · persona_card.json · style.json · pairs.jsonl)
   │  agent    人格 System Prompt + BM25 检索历史对话作参考 + 大模型生成
   ▼
OpenAI 兼容 HTTP 服务  ◀──  weclaw  ──▶  微信 ClawBot
```

对话时，系统会根据你当前说的话，从历史聊天里检索出「你曾说过类似的话、ta 当时怎么回」的真实片段，注入提示词，让回复更像本人。

## 安装

```bash
pip install --break-system-packages -r requirements.txt
cp .env.example .env
```

在 `.env` 里填入你自己的模型配置（本项目不读取、不使用任何平台内置密钥）：

```env
USER_LLM_API_KEY=your-api-key-here
USER_LLM_BASE_URL=https://api.deepseek.com/v1
USER_LLM_MODEL=deepseek-chat
```

`USER_LLM_BASE_URL` 可换成任意 OpenAI 兼容端点。不填 Key 也能跑统计部分，只是没有人格卡片。

平台模式还需要一个主密钥，用于加密各用户填写的模型 Key（不填会在 `data/.secret_key` 自动生成）：

```env
PERSONA_SECRET_KEY=change-me-to-a-long-random-string
```

## 多用户平台（推荐）

一条命令启动即得到「官网落地页 + 注册登录 + 工作台」，每个用户的数据与微信运行环境互相隔离：

```bash
python3 -m ex_persona.cli web --host 0.0.0.0 --port 8000
```

打开 `http://127.0.0.1:8000`：

1. **落地页 / 登录页**：用户名密码注册登录（若配置了微信开放平台凭据，会多出扫码登录入口）
2. **我的 Agent `/app`**：登录后看到所有 Agent 卡片（名字、标签、微信/QQ 绑定状态、创建时间、状态），点右上角「+」创建；底部是更新日志与 slogan
3. **创建方式**：
   - **AI 蒸馏**：填写 名字 / 性别 / 性格特点 / 口头禅 / 说话风格描述，并上传聊天记录、照片等素材，提交后后台异步蒸馏，界面显示进度条
   - **自己编写**：填写基础信息后直接创建，进入详情页继续补充
   - **市场**：选一个预置角色开箱即用
4. **训练人格 `/app/train`**：也可先建空人格，再拖入聊天记录/照片并「开始蒸馏」
5. **Agent 详情 `/app/agent/{id}`**：
   - 左侧「人设」四个 Tab：身份（姓名/性别/生日/年龄/籍贯/学历）、用户（关系/认识时间）、灵魂（说话风格/口头禅）、记忆（共同回忆）
   - 底部按钮：「AI 修改人设」重新分析素材并更新身份/灵魂/记忆（带进度条）、「手动编辑」逐字段修改、「新会话」清空上下文但保留人设
   - 右侧三张卡：消息渠道（微信绑定状态、**内置 weclaw 回调地址 + 桥接 Token**、聊天对象）、图片与表情包、主动消息
   - 底部功能：高级设定（消息合并、输入中提示、图片/表情包/语音回复、**允许沉默**、**独立会话**）、语音、大模型（模型 / 温度 / 记忆档位 **省积分·标准·深度**、**长期记忆开关与抽取间隔**）、智能设备、问题反馈
   - 最底部「删除该 Agent」
6. **长期记忆 `/app/memory`**：按聊天对象查看、重命名与删除自动抽取的长期记忆
7. **模型设置 `/app/settings`**：填自己的 Base URL / 模型 / API Key（加密存储），改密码、导出档案、注销账号
8. **消息渠道 `/app/wechat`**：扫码登录并启动转发，消息由当前激活人格回复

记忆档位：省积分只保留最近几条上下文，标准为常用档，深度保留更多轮次；`route_chat` 按档位裁剪历史。人设设置存 `personas.settings`，会话上下文存 `chat_turns`，表情包存 `stickers`；主动消息由后台调度器按时间窗与频率调用 `weclaw send`。

长期记忆：以 `(persona_id, contact_key)` 为边界隔离，每个微信联系人各自一份。对话积累到设定轮数后，后台异步调用用户的大模型抽取事实/偏好/事件/关系，写入 `memories`；回复时用 BM25（中文分词 + 汉字二元组）按当前消息召回最相关的若干条注入系统提示词。原始对话仍保留在 `chat_turns`，可按联系人回看。相关代码在 `ex_persona/memories.py`。

多租户隔离方式：

- 每个账号的数据在 `data/users/{user_id}/` 下，分为 `raw/`（素材）、`profile/{persona_id}/`（人格档案）、`home/`（独立 HOME，运行各自的 weclaw）
- 每个用户的 weclaw 指向带令牌的地址 `/v1/chat/completions/{bridge_token}`，平台按令牌解析出用户与其人格
- 用户填的 API Key 用 Fernet 加密后入库，响应与日志中只出现脱敏值

运维可用 `create-user` 直接开一个账号：

```bash
python3 -m ex_persona.cli create-user --username alice --password 'your-password'
```

## 三步生成人格（命令行）

```bash
# 1. 解析聊天记录，data/raw 放你的导出文件
python3 -m ex_persona.cli ingest --input data/raw --out data/profile --target 小鹿

# 2. 蒸馏人格（会调用一次大模型生成人格卡片）
python3 -m ex_persona.cli distill --profile-dir data/profile

# 3. 本地命令行试聊
python3 -m ex_persona.cli chat --profile-dir data/profile
```

`--target` 不填会自动推断出现最多的非本人昵称。支持的素材格式：

- **txt / log / md**：`2023-05-01 21:03:00 昵称` 换行接内容，或 `时间 昵称: 内容`；若整段都不像聊天，会当作一段独白
- **csv**：自动识别 `时间/内容/昵称`、`StrTime/StrContent/talker`、`IsSender` 等列名
- **json / jsonl**：消息数组，或含 `messages/data/list/records` 字段的对象；无发送者字段的会按社交动态处理
- **微博 / 豆瓣 / 小红书 / Instagram 导出 json**：递归提取 `text/caption/desc/content` 等字段，自动去重、去 HTML 标签，作为「本人动态」
- **iMessage**：macOS `chat.db`（自动识别 SQLite，兼容 Apple 时间戳）
- **短信**：SMS Backup & Restore 的 `.xml`（`type` 区分收发）
- **pdf**：整本抽文本，像聊天就按聊天解析，否则作为独白
- **照片**：`jpg/png/webp/heic` 等，读取 EXIF（时间/相机/GPS）与同名 `.txt` 配文，作为带时间线的「照片记忆」，不计入语言风格统计

也可以直接把文字粘贴进来（网页「加入粘贴内容」或 CLI 的 `--text`），写你对 ta 的主观描述同样有效。

先用仓库自带的样例验证管线：

```bash
python3 -m ex_persona.cli ingest --input examples/sample_chat.txt --out /tmp/persona-demo --target 小鹿
python3 -m ex_persona.cli distill --profile-dir /tmp/persona-demo --no-llm
python3 -m ex_persona.cli chat --profile-dir /tmp/persona-demo
```

## 接入微信 ClawBot（weclaw）

[weclaw](https://github.com/fastclaw-ai/weclaw) 是微信与 AI 之间的桥接器，扫码登录后把微信消息转给本服务。本项目用的是它的 **HTTP 模式**，所以不用写任何适配代码。

### 平台内一键绑定（推荐）

登录平台后进入「微信绑定」，点「获取二维码」扫码即可。后端会自动：

1. 在该用户独立的 HOME（`data/users/{id}/home`）下写 weclaw 配置，指向带该用户令牌的 `/v1/chat/completions/{bridge_token}`
2. 拉起 `weclaw login` 生成二维码，登录成功后执行 `weclaw start`
3. 之后发给该微信账号的消息就会由该用户当前激活的人格回复

先安装 weclaw（只需一次）：

```bash
# 安装 weclaw 二进制
curl -sSL https://raw.githubusercontent.com/fastclaw-ai/weclaw/main/install.sh | sh
```

如果 weclaw 不在 PATH，用 `WECLAW_BIN=/path/to/weclaw` 启动平台：

```bash
# 指定 weclaw 路径并启动平台
WECLAW_BIN=/path/to/weclaw python3 -m ex_persona.cli web --host 0.0.0.0 --port 8000
```

### 命令行方式

```bash
# 1. 启动 OpenAI 兼容服务
python3 -m ex_persona.cli serve --profile-dir data/profile --host 127.0.0.1 --port 8000

# 2. 合并 config/weclaw.config.example.json 到 ~/.weclaw/config.json

# 3. 扫码登录并启动桥接
weclaw start
```

微信里发 `/new` 可清空当前会话。

> 注意：进行人格角色扮演时，建议用一个单独的微信号，避免主号被风控。若要 7x24 挂机，需要把本项目与 weclaw 一起跑在一台常开的机器上（沙箱会休眠）。

## 命令一览

| 命令 | 作用 |
|------|------|
| `ingest` | 解析多平台素材（txt/csv/json/iMessage/短信/PDF/照片/社交导出），识别目标对象 |
| `distill` | 生成风格统计、人格卡片、检索对和 `SKILL.md` |
| `chat` | 本地命令行与人格对话 |
| `serve` | 启动单实例 OpenAI 兼容服务，供 weclaw 调用 |
| `web` | 启动多用户平台（官网 / 登录 / 工作台） |
| `create-user` | 运维：直接创建一个用户名密码账号 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 目录结构

```
ex_persona/
  ingest.py     素材解析与归一化
  sources.py    多数据源解析器（iMessage/短信/社交 JSON/PDF/照片 EXIF）
  distill.py    风格统计、人格卡片、SKILL.md 渲染
  persona.py    人格画像加载
  retrieval.py  BM25 中文检索
  agent.py      对话核心（人格 prompt + RAG + LLM）
  server.py     单实例 OpenAI 兼容 HTTP 接口
  webapp.py     多用户平台后端 + 带令牌的消息路由
  store.py      SQLite 数据层（用户/会话/人格/模型配置/微信绑定/会话轮次/表情包）
  accounts.py   scrypt 密码哈希与会话
  crypto.py     模型 Key 的 Fernet 加解密
  workspace.py  按用户隔离的目录与路径安全
  persona_files.py 人格档案读写与标签页字段归一化
  persona_settings.py 人格级设置默认值与校验
  presets.py    市场预置角色
  scheduler.py  主动消息调度器
  wechat.py     按用户管理的 weclaw 扫码登录、桥接与发送
  wechat_login.py 微信开放平台扫码登录（预留）
  cli.py        命令行入口
web/            落地页与工作台页面（landing/login/app/agent/train/settings/wechat）
config/         weclaw 配置示例
examples/       样例聊天记录
tests/          单元测试
```
