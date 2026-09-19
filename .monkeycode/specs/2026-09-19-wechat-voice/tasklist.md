# 微信语音收发与音色克隆 实施任务清单

- Feature: `wechat-voice`
- 日期: 2026-09-19

## T1 数据层与迁移

- [ ] `ex_persona/store.py`：新增 `voice_samples` 表（`UNIQUE(persona_id, source_message_id)`）。
- [ ] `ex_persona/store.py`：新增 `voice_clones` 表（`UNIQUE(persona_id, contact)`）。
- [ ] `_MIGRATIONS["platform_config"]` 新增 `minimax_api_key_encrypted`、`voice_tts_model`、`voice_clone_cost`（500）、`voice_reply_cost`（20）、`voice_enabled`。
- [ ] 新增 store 函数：`add_voice_sample`、`list_voice_samples`、`prune_voice_samples`（保留最新 20）、`voice_sample_summary`、`get_voice_clone`、`upsert_voice_clone`、`set_voice_clone_status`。
- [ ] `ex_persona/config.py` `PlatformConfig` 补齐语音字段，`load_platform_config` 读取并与环境变量回退。
- [ ] `ex_persona/persona_settings.py` `voice` 默认值与 `validate()`：补 `preset`、`clone_voice_id`、`clone_status`、`clone_contact`。

## T2 语音客户端

- [ ] 新增 `ex_persona/voice.py`：`sniff_audio_ext(data)`、`PRESET_VOICES`（`female-1`→`female-shaonv` 等）。
- [ ] `minimax_tts(text, voice_id, *, api_key, base_url, model, timeout) -> bytes`：调用 `POST {base_url}/t2a_v2`，`audio_setting.format="mp3"`，十六进制解码。
- [ ] `minimax_clone(sample_paths, *, api_key, base_url, model, voice_id) -> dict`：上传样本 + 调用音色克隆接口。
- [ ] 定义 `VoiceError`（含 `auth`）与统一超时/错误映射。

## T3 入站语音

- [ ] `ex_persona/webapp.py`：新增 `POST /api/wechat/voice/{bridge_token}`，复用桥接令牌校验与限流。
- [ ] 音频校验（≤ 5,000,000 字节且识别为音频），落盘 `voices/{safe_contact}/msg-{message_id}.{ext}`。
- [ ] 按 `source_message_id` 幂等；写入 `voice_samples` 后调用 `prune_voice_samples`。
- [ ] `contact` 取 `X-WeChat-From`，`duration_ms` 取 `X-Voice-Duration-Ms`。

## T4 weclaw 补丁

- [ ] `scripts/weclaw/nian.patch` 追加：`Config.VoiceEndpoint` + `WECLAW_VOICE_ENDPOINT`。
- [ ] `cmd/start.go`：配置存在时 `SetVoiceEndpoint`。
- [ ] `messaging/handler.go`：`extractVoice`、`downloadVoice`、`forwardVoice`，并在 `HandleMessage` 中接入，失败不影响文字。
- [ ] `scripts/weclaw/zz_patch_test.go` 覆盖语音提取与转发构造；`build.sh` 编译通过。

## T5 平台语音配置（管理员）

- [ ] `webapp.py` 平台配置读写扩展语音字段，Key 仅存密文、不回显。
- [ ] `web/admin.html` 新增「语音设置」卡片：Key、模型名、克隆成本、回复成本、启用开关。
- [ ] 后台用量区展示音色克隆次数与语音回复条数。

## T6 音色接口

- [ ] `GET /api/personas/{id}/voice`：音色、克隆状态、样本总时长/条数、预设、平台是否就绪。
- [ ] `POST /api/personas/{id}/voice/clone`：校验 consent、样本 ≥ 10 秒、无进行中克隆；先扣费再调用，失败退款。
- [ ] `POST /api/personas/{id}/voice/preview`：合成固定文本，返回 `audio/mpeg`。
- [ ] 全部经 `_require_persona` 保证 401/404。

## T7 出站语音回复

- [ ] `route_chat` 拿到最终 `reply` 后，按 `advanced.reply_voice` + 平台密钥 + 非空文本触发 `_start_voice_reply` 后台线程。
- [ ] 合成成功 → 扣 `voice_reply_cost` → `_outbox.enqueue(media=path)`；失败不扣费且不阻断文字。
- [ ] 未配置密钥或余额不足时跳过语音并保留文字回复。

## T8 用户端语音设置

- [ ] `web/agent.html` `openTuning()` 语音区：回复开关、预设音色下拉、克隆状态、样本时长、克隆按钮、试听按钮、同意勾选。
- [ ] `web/static/app.js`：`playVoicePreview` 与音频播放辅助。
- [ ] 保存走 `PATCH /api/personas/{id}` 的 `advanced` 与 `voice` 字段。

## T9 测试

- [ ] `tests/test_voice_samples.py`：落盘、超限 400、幂等、20 条上限、权限。
- [ ] `tests/test_voice_clone.py`：样本不足 400、未同意 400、扣费与退款、重复发起、成功写 `voice_id`。
- [ ] `tests/test_voice_reply.py`：开启入队并扣费、失败不扣、无密钥跳过、空回复跳过。
- [ ] 契约测试：`admin.html` 语音设置、`agent.html` 克隆/试听/同意、`voice.py` 含 `t2a_v2`。
- [ ] `ruff check ex_persona tests`、`unittest discover`、`scripts/e2e/run_chain.py` 全绿。

## T10 交付

- [ ] 提交并 push `main`。
- [ ] 部署生产并核对（含 DB 备份、weclaw 重新编译同步）。

## 验证记录

- 待实施后补充。
