# User Instruction Memory

This file records user instructions, preferences, and teachings for reference in future interactions.

## Format

### User Instruction Entry
User instruction entries should follow this format:

[User Instruction Summary]
- Date: [YYYY-MM-DD]
- Context: [Mentioned scenario or time]
- Instructions:
  - [Content of user teaching or instruction, described line by line]

### Project Knowledge Entry
Entries discovered by the Agent during task execution should follow this format:

[Project Knowledge Summary]
- Date: [YYYY-MM-DD]
- Context: Discovered by Agent while performing [specific task description]
- Category: [Operations & Deployment|Build Methods|Testing Methods|Troubleshooting & Debugging|Workflow & Collaboration|Environment Configuration]
- Instructions:
  - [Specific knowledge points, described line by line]

## Deduplication Strategy
- Before adding a new entry, check for similar or identical instructions.
- If a duplicate is found, skip the new entry or merge it with the existing one.
- When merging, update the context or date information.
- This helps avoid redundant entries and keeps the memory file tidy.

## Entries

[Project Knowledge Summary]
- Date: 2026-09-13
- Context: Discovered by Agent while running and testing the multi-user platform
- Category: Environment Configuration
- Instructions:
  - weclaw 二进制在本环境的路径为 `/tmp/opencode/weclaw/weclaw`；启动平台时用 `WECLAW_BIN=/tmp/opencode/weclaw/weclaw python3 -m ex_persona.cli web --host 0.0.0.0 --port 8000`。内置 weclaw 会重复下发同一条微信消息，平台侧已用 4 秒去重缓存（`DEDUP_SECONDS`）处理。
  - **时间感知统一走 `ex_persona/clock.py`**：服务器时区是 UTC，所有「现在几点/今天几号/时段/时间窗/当天零点」都必须用 `clock.now_local()`（默认 `Asia/Shanghai`，可用环境变量 `PERSONA_TIMEZONE` 覆盖）而不是 `datetime.now()`，否则会把 UTC 时间说给东八区用户听；聊天系统提示注入 `clock.block()`，距上次对话超 1 小时再注入 `clock.gap_hint()`。

[Project Knowledge Summary]
- Date: 2026-09-13
- Context: Discovered by Agent while adding reliability hardening
- Category: Testing Methods
- Instructions:
  - 运行全部测试：`python3 -m unittest discover -s tests`；涉及平台的测试在 `tests/test_platform.py`，通过临时 `PERSONA_DATA_DIR` 与 FastAPI `TestClient` 隔离。
  - 可靠性相关测试在 `tests/test_reliability.py`（出站队列、媒体指令解析、任务落库、图片占位短路）。
  - 代码规范用 ruff：`ruff check ex_persona tests`；配置在根目录 `pyproject.toml`（line-length 120，忽略 E501/B008），CI 在 `.github/workflows/ci.yml`。ruff 未预装，需 `pip install --break-system-packages ruff`。

[Project Knowledge Summary]
- Date: 2026-09-15
- Context: Discovered by Agent while adding headless front-end render checks
- Category: Testing Methods
- Instructions:
  - 前端页面用 jsdom 做无头渲染冒烟测试：`require("/usr/local/lib/node_modules/jsdom")`（node v22 全局安装）。
  - 真机像素级验证可用 Python Playwright（2026-09-15 已装）：`pip install --break-system-packages playwright && python3 -m playwright install --with-deps chromium`，浏览器落在 `/root/.cache/ms-playwright/chromium-1234`；脚本示例 `/tmp/opencode/shot_settings.py`（`page.route` 打桩 `/api/**`，用 `locator("#creditCard").screenshot()` 截局部，并 `page.evaluate` 量取 `getBoundingClientRect` 校验右对齐列）。本模型无法直接看图，可用 `image_analysis_create_task` 传公网 URL 获取文字化的视觉评审；把截图目录用 `python3 -m http.server` 起服务再 `request_preview` 得到可访问 URL。
  - 纯静态预览：`python3 -m http.server 8199 --directory /workspace/web`，页面走 `/settings.html` 等相对入口，接口由 Playwright 路由打桩。
  - jsdom 脚本模式：读 `web/*.html`，用正则取出末尾内联 `<script>`，`new JSDOM(html,{runScripts:"outside-only"})` 后 `window.eval(appJs + inline)`，再 stub `window.fetch` 返回各接口假数据，`await` 150ms 后断言 DOM 并打印 `RENDER_OK`/`RENDER_FAIL`。
  - 渲染脚本放在 `/tmp/opencode/render_*.js`（settings/app/admin/memory/agent/timeline）；canvas 的 `getContext()` 在 jsdom 报未实现属预期噪音，可忽略。

[Project Knowledge Summary]
- Date: 2026-09-13
- Context: Discovered by Agent while implementing the outbox and observability layer
- Category: Operations & Deployment
- Instructions:
  - 所有主动发送（主动消息、表情/图片回复）统一经 `ex_persona/outbox.py` 的 `OutboxWorker` 入 `outbox` 表后由后台线程发送，带指数退避与每账号每分钟 20 条上限；失败超限标记 `failed`，可在 `store.outbox_stats()` 查看。
  - 蒸馏/重蒸馏任务落 `tasks` 表（`store.create_task/update_task/get_task`），带 `result` JSON 列，进程重启不丢。
  - 结构化 JSON 日志与进程内指标在 `ex_persona/observability.py`；`GET /api/metrics`（仅 admin 角色）返回计数与耗时，并附带 `outbox` 统计。

[Project Knowledge Summary]
- Date: 2026-09-13
- Context: Discovered by Agent while implementing per-contact long-term memory
- Category: Build Methods
- Instructions:
  - 平台依赖一个打过补丁的 weclaw：v0.7.1 原生 HTTP agent 不转发消息发送者，`agent/http_agent.go` 的请求体已加入 `"user": conversationID`。
  - 重新构建：源码在 `/tmp/opencode/weclaw-src`（`git clone --branch v0.7.1 https://github.com/fastclaw-ai/weclaw.git`），执行 `cd /tmp/opencode/weclaw-src && go build -o /tmp/opencode/weclaw/weclaw .`（Go 1.25），会覆盖 `/tmp/opencode/weclaw/weclaw`。
  - 补丁校验：`cd /tmp/opencode/weclaw-src && go test ./agent/ -run TestHTTPAgentSendsUser`，确认请求体带 `user`。
  - 同一份补丁新增：`config.Config.MediaEndpoint`（config key `media_endpoint`，env `WECLAW_MEDIA_ENDPOINT`）与 `messaging.Handler.SetMediaEndpoint`；收到图片/表情时由 `forwardImage` 以 multipart POST 到该端点（带 `X-WeChat-From` 头），平台 `POST /api/wechat/media/{bridge_token}` 保存为该人格的表情包。平台在 `wechat.py ensure_config` 自动写入该字段。
  - 追加补丁（2026-09-13）：`messaging/sender.go` 的 `SendTextReply` 在纯文本为空时直接返回，使平台只发表情不附文字时不产生空气泡；`messaging/handler.go` 图片分支转发成功后不再 return，而是以占位文本 `[图片]` 继续走 agent，由平台按人格设置 `advanced.reply_to_images` 决定是否回应（默认静默）。
  - 编译类命令必须走 background terminal，不要用前台 bash 直接构建。

[Project Knowledge Summary]
- Date: 2026-09-13
- Context: Discovered by Agent while designing long-term memory
- Category: Workflow & Collaboration
- Instructions:
  - 长期记忆以 `(persona_id, contact_key=微信 from_user_id)` 为隔离边界；`chat_turns.contact`、`contacts`、`memories` 三张/列支撑，`store.init_db` 自动迁移。
  - 记忆召回用 `ex_persona/memories.py` 的 BM25 + 汉字二元组分词（纯 jieba 会把「咖啡」并进「喝咖啡」导致漏召回）。
  - 设计文档在 `.monkeycode/specs/2026-09-13-long-term-memory/`。

[Project Knowledge Summary]
- Date: 2026-09-14
- Context: Discovered by Agent while deploying the platform to the production server
- Category: Operations & Deployment
- Instructions:
  - 生产服务器：`191.40.37.63`（hostname `RainYun-WkTsqH7r`，Ubuntu 24.04.1 LTS, x86_64, 2 vCPU / 4GB / 40GB），SSH 用户 `root`（密码通过私密渠道提供，不记录于此）。原公网 IP `162.251.93.174` 已于 2026-09-14 弃用，该 IP 现返回 `Server: Caddy` 的 308，属服务商共享网关，不再指向本机。
  - 部署目录 `/opt/nian`（代码）、`/opt/nian/venv`（虚拟环境）、`/opt/nian/bin/weclaw`（Linux x86_64 二进制）、`/opt/nian/data`（运行数据，含 SQLite）、`/opt/nian/.env`（600 权限，含 `PERSONA_SECRET_KEY`/`WECLAW_BIN`/`PERSONA_DATA_DIR`/`PERSONA_PORT`）。
  - 进程由 systemd 管理：unit 为 `nian`（已 `enable` 开机自启），`ExecStart=/opt/nian/venv/bin/python -m ex_persona.cli web --host 127.0.0.1 --port 8000`，只监听回环；对外由 Nginx `sites-enabled/nian` 反代 80 到 `127.0.0.1:8000`（`client_max_body_size 64m`）。常用运维：`systemctl restart nian`、`systemctl status nian`、`nginx -t && systemctl reload nginx`。
   - 访问入口：`http://191.40.37.63/`（云安全组已放行 80；8000 不对外暴露）。域名需在 DNS 侧把 A 记录指向 `191.40.37.63`，否则 HTTPS 证书签发/访问会走旧 IP 失败。**主力域名 `nian.czihao.xyz`（2026-09-14 启用，与 `ai.czihao.xyz` 同证书同站点）**：用户端曾因本地/运营商解析器残留旧 IP `162.251.93.174` 出现间歇 `ERR_SSL_PROTOCOL_ERROR`（旧 IP 的 443 会回 `tlsv1 alert internal error`），换新子域即绕开缓存；`ai.czihao.xyz` 暂保留。
  - SSH 连接限制的真实原因：**本 Agent 沙箱的出口在连续多次连接后会临时封锁出站 22 端口**（表现为 `kex_exchange_identification: Connection closed` 或 `Permission denied`；同期连 GitHub:22 会被同样方式关闭，而 GitHub:443 正常），不是服务器侧问题。因此禁止短时间连发多个 ssh/scp；用低频（每次间隔 300-420 秒）、**单次** `ssh 'bash -s' < script` 完成整批操作。`ControlMaster` 多路复用在此不稳定（会 Broken pipe 后回退密码认证，反而触发封禁），不要依赖。
  - 该主机 `scp` 通道不稳定（常 `Connection closed`），优先用 `ssh 'cat > 目标路径' < 本地文件` 或把文件内容嵌入脚本单次执行。实测大文件（约 200KB+）用 `ssh 'cat > file' < file` 会在 kex 阶段被断开，改用 `scp` 上传再单次 `ssh` 启动更稳。
  - `/root/.ssh/config` 已配置别名 `nian` + 部署密钥 `/root/.ssh/id_ed25519`（已加入服务器 `authorized_keys`），密钥登录在允许窗口内可用。
  - 稳妥发布方式：把代码 tar 用 base64 内嵌进自包含 `bootstrap.sh`，单次连接执行 `ssh nian 'cat > /root/bootstrap.sh; chmod +x /root/bootstrap.sh; nohup setsid /root/bootstrap.sh >/dev/null 2>&1 &' < bootstrap.sh`；脚本自身把日志写 `/var/log/nian_bootstrap.log`，并在结尾复制到 `/opt/nian/web/static/_boot_status.txt` 供 HTTP 读取（SSH 被封时也能看结果）。
   - HTTPS 已配置：`certbot` + `python3-certbot-nginx` 已装，证书在 `/etc/letsencrypt/live/ai.czihao.xyz/`（SAN 含 `ai.czihao.xyz` 与 `nian.czihao.xyz`，自动续期，有效至 2026-12-13）；Nginx `server_name ai.czihao.xyz nian.czihao.xyz`，80 端口 301 跳 443，443 提供站点。新增域名时：把域名加入两个 `server_name`，再 `certbot --nginx -d ai.czihao.xyz -d <新域名> --expand --non-interactive --agree-tos --redirect`。本机**未安装 fail2ban**。
  - 站点曾**间歇性成簇失败**（坏窗口约占 30%-85% 请求），浏览器（iOS/Safari）表现为弹 `Load failed`。证据：坏窗口 `http://` 返回 `Server: Caddy` 的 308、`https://` 在 TLS 握手阶段收到 `tlsv1 alert internal error`；正常窗口 `http://` 为 `Server: nginx/1.24.0`、`https:///health` 200。旧机**无任何 Caddy 进程/二进制**，判定为**服务商侧共享 Caddy 网关间歇劫持/ IP 冲突**，非服务器问题；同一坏窗口 `ssh` 也会 `Permission denied (publickey)`。2026-09-14 更换公网 IP 为 `191.40.37.63` 后，`Server` 头稳定返回 nginx，疑已规避该问题，待持续观察。
  - 缓解措施（已上线）：前端 `/static/app.js` 的 `api()` 加入网络层自动重试——GET 最多 4 次、写操作（POST/PUT/PATCH/DELETE）最多 3 次，退避 `260ms*2^(n-1)` 加随机抖动，并对 502/503/504 重试；`navigator.onLine===false` 时直接提示"网络已断开"。可显著提升坏窗口下的最终成功率。根治需联系雨云排查该 IP 的代理/绑定/冲突。
  - 数据库每日备份已启用：`nian-backup.timer`（每天 04:30）调用 `/opt/nian/scripts/backup_db.sh`，输出到 `/opt/nian/backups/`（保留最近 15 份，SQLite 在线 backup + gzip）；`/var/log/journal` 已持久化。
  - 发布新版本：本地打包（排除 `data/.git/.monkeycode/__pycache__`）经单次 SSH 传到 `/opt/nian` 覆盖，再 `systemctl restart nian`；`data/` 目录保留不动。
  - 新机部署步骤：`apt-get update && apt-get install -y python3-venv python3-pip nginx`；`python3 -m venv /opt/nian/venv && /opt/nian/venv/bin/pip install -r /opt/nian/requirements.txt`。
  - 管理员开通：`/opt/nian/venv/bin/python -m ex_persona.cli set-role --username <名> --role admin`，或设环境变量 `PERSONA_ADMIN_USERS=名字1,名字2`（启动与注册时自动提升）。生产管理员账号为 `czh0721`（id=1）。重置密码：`/opt/nian/venv/bin/python -m ex_persona.cli set-password --username <名> --password <新密码>`（改密后注销其全部会话并写入审计）。
  - Nginx 站点配置在 `/etc/nginx/sites-available/nian`（`server_name ai.czihao.xyz nian.czihao.xyz`），已开 `listen 443 ssl http2` 与 gzip；`gzip_types` 必须含 `text/javascript`（静态 JS 响应是 `text/javascript` 而非 `application/javascript`，漏了就不压缩）。网页路由：登录页 `/login`、应用 `/app`、后台 `/admin`（独立登录页 `/admin/login`，旧 `/app/admin` 302 兼容）、设置 `/app/settings`（不是 `/admin.html`）。
  - 应用层已加 HSTS（仅 HTTPS 响应，`max-age=31536000; preload`）、全局 500 异常处理；`X-Content-Type-Options`/`Referrer-Policy` 由 Nginx 层下发。
  - `/health` 自愈巡检：`/usr/local/bin/nian-healthcheck.sh` + `nian-healthcheck.timer`（每分钟），连续 3 次失败即 `systemctl restart nian`；计数存 `/run/nian-health.fail`，日志 `journalctl -t nian-health`。
  - 管理员审计表 `admin_audit`（`store.add_audit/list_audit`），后台改角色/重置密码/CLI `set-role`/`set-password` 均写审计。
   - 发布仍用自包含 `bootstrap.sh`（head + base64 `code.tgz` + tail）；tail 会装 healthcheck、按 `/opt/nian/deploy/nginx-nian.conf` 覆盖 Nginx 站点（覆盖前备份 `nian.harden.bak.*`，`nginx -t` 失败自动回滚）并重启服务。
    - 日常快速发布脚本：`bash /tmp/opencode/deploy_credit.sh`（打包 `web ex_persona`+`scp` → 远程 `/tmp/opencode/deploy_credit_remote.sh $TS` 内先用 sqlite `backup()` 备份生产 DB 到 `/opt/nian/backups/platform_$TS.db`，再备份代码到 `/opt/nian/backups/{ex_persona,web}_$TS` → 解包 `/opt/nian` → `systemctl restart nian` → 校验 `is-active`）。**凡涉及启动迁移（如积分清零）的发布，务必保留 DB 备份**，因为迁移在 `init_db()` 启动时不可逆执行。脚本内置 20 次、每次间隔 30s 的重试以对抗沙箱出口对 22 端口的临时封锁；实测常在第 1-2 次 `Connection closed` 后于第 3 次成功，需耐心等待。**注意**：连续 ssh 会触发封禁，验证期不要短时间反复连。

[Project Knowledge Summary]
- Date: 2026-09-16
- Context: Discovered by Agent while fixing a production crash loop after adding a migrated column
- Category: Troubleshooting & Debugging
- Instructions:
  - `store.py` 的 `SCHEMA` 会在 `_MIGRATIONS` 补列**之前**执行；任何引用迁移列的 `CREATE INDEX` 必须从 `SCHEMA` 移出、放在 `init_db` 的补列循环之后，否则老库启动时 `executescript(SCHEMA)` 报 `sqlite3.OperationalError: no such column`，服务 crash loop（systemd 表现为 `activating`、`NRestarts` 持续增长、日志 `Application startup failed. Exiting.`）。
  - 服务 crash loop 时 `systemctl is-active` 返回 `activating` 且非零退出码，会让部署脚本的 `set -e` 提前中断；先看 `journalctl -u nian -n 40` 定位 startup 异常。
  - 生产机**未安装 sqlite3 CLI**，查询/校验数据库请用 `/opt/nian/venv/bin/python -c "import sqlite3; ..."`。

[Project Knowledge Summary]
- Date: 2026-09-16
- Context: Discovered by Agent while fixing WeChat channel auto-on / re-login no-reply
- Category: Troubleshooting & Debugging
- Instructions:
  - weclaw 会话过期后守护进程仍存活并每 5s 刷 `[monitor] WARNING: WeChat session expired`，而 `weclaw status` 显示 `weclaw is not running`（pidfile 已失效）。不能用「进程存在」判定在线；`ex_persona/wechat.py` 现以日志中的 `session expired` / `re-authenticate` 作为失效信号：停止进程、落库 `phase=expired`、生成「微信登录已失效」站内提醒。
  - 转发开关的用户意图必须落库到 `wechat_bindings.phase`：只有 `running` / `failed` 才允许 watchdog 和打开渠道面板时自动拉起；`logged-in`（用户手动关闭）与 `expired` 都不自动拉起。进程退出清理（`WeChatManager.stop_all`）必须用 implicit 停止，不能改写 phase。
  - 重新扫码成功后必须 `start_bridge(force=True)`：否则旧 weclaw 进程句柄还在时 `start_bridge` 会直接返回「已在运行」，新凭据不被加载，表现为扫码成功却不回复。
  - `ensure_config` 会在 status 轮询时被高频调用，必须原子写入（临时文件 + `os.replace`）并串行化，避免并发写坏 JSON 导致 weclaw 读不到配置。
  - **反复扫码会导致 `~/.weclaw/accounts/` 累积多个账号文件**（按 bot id 命名；`ilink.LoadAllCredentials` 会为每个账号各起一个 monitor）。weclaw 启动日志出现 `Starting message bridge for N account(s)`（N>1）即为累积信号；历史失效账号持续刷 `session expired`，会让**新登录的有效账号被整体误判为过期并杀桥接**，表现为「扫码十几次一直收不到消息」。修复：`WeChatBridge.prune_stale_accounts()` 只保留最新账号（其余移入 `accounts/_stale/` 备份，不删除），`start_bridge` 前自动清理，`status()` 在账号数>1 时不判失效而是清理并 `start_bridge(force=True)` 自动恢复（无需用户重扫）；`_autostart_bridges` 对 `phase=expired` 且残留账号>1 的绑定走 `recover()`。
  - 排障入口（SSH 被沙箱封锁时）：用管理员 cookie 调 `/api/me` 看 `wechat.phase/running`、`/api/wechat/status?verbose=1` 看 `weclaw`/`log`/`bridge_log`（重点看 `Starting message bridge for N account(s)` 与是否含 `session expired`）、`/api/notifications` 看站内提醒；三者足以还原桥接现场。cookie 文件需去掉 `#HttpOnly_` 前缀（`sed 's/^#HttpOnly_//'`）才能被 `curl -b` 识别。
  - 人格「答非所问/不记得自己刚说过什么」时，先用 `POST /api/personas/{id}/debug`（带 `contact` 与 `message`，不写库）看返回的 `history_count`、`history` 与 `system_prompt`（可直接确认 `【当下时间】`/`【时间间隔】`/记忆块是否真的注入，且不消耗积分），再用 `/api/personas/{id}/timeline` 交叉验证。历史按 `contact` 过滤即已实现「每个聊天对象独立上下文」，`independent_session` 不能用「把 `memory_limit` 置 0」来实现隔离，否则连当前对话也一起失忆。

[Project Knowledge Summary]
- Date: 2026-09-16
- Context: Discovered by Agent while adding a full-chain regression script
- Category: Testing Methods
- Instructions:
  - 全链路回归脚本：`python3 scripts/e2e/run_chain.py`（自带假模型服务 `scripts/e2e/fake_llm.py`，无需真实 Key）。用 FastAPI `TestClient` 跑真实路由，覆盖 注册赠积分→创建人格→粘贴素材→解析→同步蒸馏→异步重蒸馏轮询→绑定桥接令牌→对话计费→长回复分段入队→空/乱码回复退款→长期记忆抽取→危机兜底→主动消息→时间线→告别/激活→兑换码/套餐/后台发放→通知→导出。
  - 该脚本刻意不放进 `tests/`（`unittest discover` 不加载），因为它会设置 `PERSONA_PLATFORM_*` 环境变量并启动本地 HTTP 服务，放进默认套件会污染其他平台测试；单独运行即可。
  - 假模型按提示词分流：系统提示含「人格蒸馏分析师」返回画像 JSON、含「对话记忆抽取器」返回记忆 JSON，其余按最后一条 user 消息触发词返回长回复 / 空 / 乱码（私用区字符触发 `looks_garbled`）。
  - 分段改写提示：`humanize.MAX_SEGMENT_CHARS=160`，假回复必须超过 160 字才会触发分段；调试分段时先确认长度。

