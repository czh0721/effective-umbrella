# 微信语音收发与音色克隆 实施任务清单

- Feature: `wechat-voice`
- 日期: 2026-09-19

## T1 数据层与迁移

- [x] `ex_persona/store.py`：新增 `voice_samples` 表（`UNIQUE(persona_id, source_message_id)`）。
- [x] `ex_persona/store.py`：新增 `voice_clones` 表（`UNIQUE(persona_id, contact)`）。
- [x] `_MIGRATIONS["platform_config"]` 新增 `minimax_api_key_encrypted`、`voice_tts_model`、`voice_clone_cost`（500）、`voice_reply_cost`（20）、`voice_enabled`。
- [x] 新增 store 函数：`add_voice_sample`、`list_voice_samples`、`prune_voice_samples`（保留最新 20）、`voice_sample_summary`、`get_voice_clone`、`upsert_voice_clone`、`set_voice_clone_status`。
- [x] `ex_persona/config.py` `PlatformConfig` 补齐语音字段，`load_platform_config` 读取并与环境变量回退。
- [x] `ex_persona/persona_settings.py` `voice` 默认值与 `validate()`：补 `preset`、`clone_voice_id`、`clone_status`、`clone_contact`。

## T2 语音客户端

- [x] 新增 `ex_persona/voice.py`：`sniff_audio_ext(data)`、`PRESET_VOICES`（`female-1`→`female-shaonv` 等）。
- [x] `minimax_tts(text, voice_id, *, api_key, base_url, model, timeout) -> bytes`：调用 `POST {base_url}/t2a_v2`，`audio_setting.format="mp3"`，十六进制解码。
- [x] `minimax_clone(sample_paths, *, api_key, base_url, model, voice_id) -> dict`：上传样本 + 调用音色克隆接口。
- [x] 定义 `VoiceError`（含 `auth`）与统一超时/错误映射。

## T3 入站语音

- [x] `ex_persona/webapp.py`：新增 `POST /api/wechat/voice/{bridge_token}`，复用桥接令牌校验与限流。
- [x] 音频校验（≤ 5,000,000 字节且识别为音频），落盘 `voices/{safe_contact}/msg-{message_id}.{ext}`。
- [x] 按 `source_message_id` 幂等；写入 `voice_samples` 后调用 `prune_voice_samples`。
- [x] `contact` 取 `X-WeChat-From`，`duration_ms` 取 `X-Voice-Duration-Ms`。

## T4 weclaw 补丁

- [x] `scripts/weclaw/nian.patch` 追加：`Config.VoiceEndpoint` + `WECLAW_VOICE_ENDPOINT`。
- [x] `cmd/start.go`：配置存在时 `SetVoiceEndpoint`。
- [x] `messaging/handler.go`：`extractVoice`、`downloadVoice`、`forwardVoice`，并在 `HandleMessage` 中接入，失败不影响文字。
- [x] `scripts/weclaw/zz_patch_test.go` 覆盖语音提取与转发构造；`build.sh` 编译通过。

## T5 平台语音配置（管理员）

- [x] `webapp.py` 平台配置读写扩展语音字段，Key 仅存密文、不回显。
- [x] `web/admin.html` 新增「语音设置」卡片：Key、模型名、克隆成本、回复成本、启用开关。
- [x] 后台用量区展示音色克隆次数与语音回复条数。

## T6 音色接口

- [x] `GET /api/personas/{id}/voice`：音色、克隆状态、样本总时长/条数、预设、平台是否就绪。
- [x] `POST /api/personas/{id}/voice/clone`：校验 consent、样本 ≥ 10 秒、无进行中克隆；先扣费再调用，失败退款。
- [x] `POST /api/personas/{id}/voice/preview`：合成固定文本，返回 `audio/mpeg`。
- [x] 全部经 `_require_persona` 保证 401/404。

## T7 出站语音回复

- [x] `route_chat` 拿到最终 `reply` 后，按 `advanced.reply_voice` + 平台密钥 + 非空文本触发 `_start_voice_reply` 后台线程。
- [x] 合成成功 → 扣 `voice_reply_cost` → `_outbox.enqueue(media=path)`；失败不扣费且不阻断文字。
- [x] 未配置密钥或余额不足时跳过语音并保留文字回复。

## T8 用户端语音设置

- [x] `web/agent.html` `openTuning()` 语音区：回复开关、预设音色下拉、克隆状态、样本时长、克隆按钮、试听按钮、同意勾选。
- [x] `web/static/app.js`：`playVoicePreview` 与音频播放辅助。
- [x] 保存走 `PATCH /api/personas/{id}` 的 `advanced` 与 `voice` 字段。
- [x] 排版重做：样本统计卡（条数/时长）+「回复设置」「克隆音色」分组卡片，状态用徽标，按钮归并为上传/克隆与保存两行。

## T9 测试

- [x] `tests/test_voice_samples.py`：落盘、超限 400、幂等、20 条上限、权限。
- [x] `tests/test_voice_clone.py`：样本不足 400、未同意 400、扣费与退款、重复发起、成功写 `voice_id`。
- [x] `tests/test_voice_reply.py`：开启入队并扣费、失败不扣、无密钥跳过、空回复跳过。
- [x] 契约测试：`admin.html` 语音设置、`agent.html` 克隆/试听/同意、`voice.py` 含 `t2a_v2`。
- [x] `ruff check ex_persona tests`、`unittest discover`、`scripts/e2e/run_chain.py` 全绿。

## T10 交付

- [x] 提交并 push `main`。
- [x] 部署生产并核对（含 DB 备份、weclaw 重新编译同步）。

## T11 多提供商（MiniMax / 豆包）

- [x] `store.py`：`platform_config` 增 `voice_provider`/`doubao_*` 列；`upsert_voice_clone` 记录 `provider`；`set_platform_config` 支持豆包字段。
- [x] `config.py`：`PlatformConfig` 增豆包字段与 `voice_credentials_ready`；按提供商判定 `voice_ready`；支持 `PERSONA_DOUBAO_*` 环境变量。
- [x] `voice.py`：`VoiceCredentials` + `credentials()`；`synthesize()`/`clone()` 分派；豆包 `doubao_tts`（V3 分块 JSON）与 `doubao_clone`（V3 复刻）；按提供商解析预设音色与资源 ID。
- [x] `webapp.py`：克隆/合成/试听改走分派；后台配置读写与脱敏字段；`voice_provider` 写审计。
- [x] `admin.html`：新增「语音提供商」下拉与豆包 API Key / App ID / Access Token / 资源 ID 字段。
- [x] `tests/test_voice.py` 豆包用例；`tests/test_voice_clone.py` 提供商切换与就绪判定；`tests/test_contracts.py` 豆包端点与后台字段契约。

## T12 用户上传音频样本

- [x] `webapp.py`：`POST /api/personas/{persona_id}/voice/samples`，校验大小/格式/时长，临时目录探测后落盘，复用 `add_voice_sample` 与样本上限。
- [x] `web/agent.html` `openVoice()`：新增「上传本地音频」按钮与多选文件输入，逐条上传并刷新面板。
- [x] `tests/test_voice_samples.py` 上传用例；`tests/test_contracts.py` 前端标记与端点契约。

## T13 语音收集开关与进度

- [x] `persona_settings.py`：`voice.collect` 默认 `true` + `validate()` 归一。
- [x] `webapp.py`：入站语音在 `collect=false` 时跳过并返回 `skipped`；`_persona_voice_detail` 返回 `collect`。
- [x] `web/agent.html`：「语音收集」分组（开关 + 进度条 + 结果文案）；克隆 `pending` 轮询刷新。
- [x] `tests/test_voice_samples.py` 跳过收集与详情字段用例；`tests/test_contracts.py` 前端标记。

## 验证记录

- 真实样本：iLink 下发编码为 `#!SILK_V3`（两条 5.83s + 6.49s）；在服务器编译 `kn007/silk-v3-decoder` 解码器并装到 `/opt/nian/bin/silk_v3_decoder`，`SILK_DECODER_BIN` 写入 `/opt/nian/.env`。
- 转码链路：2 条 silk 经 `voice.build_clone_audio` 合并为 mp3，11.98s / 96KB / 24kHz 单声道，满足 MiniMax 克隆约束。
- 测试：`python3 -m ruff check ex_persona tests` 全过；`python3 -m unittest discover -s tests` 490 全过；`python3 scripts/e2e/run_chain.py` 129/129。
- 生产：新 weclaw 二进制装 `/opt/nian/bin/weclaw`（备份 `weclaw.bak.20260919_041433`），部署 `20260919_042641`（T1 数据层）、`20260919_044538`（语音全量）；DB 含 `voice_samples`/`voice_clones` 与语音配置列。
- 待管理员在后台「语音设置」填入 MiniMax Key 并开启后，出站语音与克隆才会生效。
- 多提供商增量：`ruff check ex_persona tests` 全过；`unittest discover` 504 全过；`scripts/e2e/run_chain.py` 129/129。豆包接口按官方 V3 文档实现，需管理员填入豆包 App ID + Access Token（或 API Key）后做一次真实联调验证。
- 用户上传音频增量：`ruff check ex_persona tests` 全过；`unittest discover` 511 全过；`scripts/e2e/run_chain.py` 129/129。
- 收集开关与进度增量：`ruff check ex_persona tests` 全过；`unittest discover` 513 全过；`scripts/e2e/run_chain.py` 129/129。
