# Requirements Document

## Introduction

「念念 Nian」通过内置 weclaw 桥接连通微信。当前微信语音只会被微信自带的语音转文字（STT）处理为文本后交给平台，原始音频不会下发；平台也无法发出语音，只能回复文字。本功能打通两个方向：把收到的微信语音原始音频采集为「语音样本」，用于 MiniMax 音色克隆；在开启语音回复后，用选中音色合成音频并经现有出站队列发送到微信。

## Glossary

- **语音样本（Voice Sample）**：微信桥接转发到平台的原始语音音频文件，保存在该分身目录下，用于音色克隆。
- **联系人（Contact）**：与某个分身对话的微信用户，由 `contact_key` 标识。
- **音色（Voice）**：用于语音合成的发声特征，对应 MiniMax 的 `voice_id`，可为系统预设音色或复刻音色。
- **复刻音色（Cloned Voice）**：调用 MiniMax 音色克隆接口，用语音样本生成并绑定到分身的 `voice_id`。
- **语音回复（Voice Reply）**：把分身的文字回复经 TTS 合成为音频文件，再经出站队列发送。
- **平台密钥（Platform Voice Key）**：管理员在后台配置的 MiniMax API Key，供全部用户的语音合成与克隆使用。
- **语音积分成本（Voice Cost）**：语音回复与音色克隆各自消耗的服务端积分额度。

## Requirements

### Requirement 1

**User Story:** AS 分身拥有者, I want 收到的微信语音被保存为语音样本, so that 我可以基于对方真实声音克隆音色。

#### Acceptance Criteria

1. WHEN 微信桥接收到达标语音消息, the 桥接 SHALL 把原始音频上传到 `POST /api/wechat/voice/{bridge_token}`，并将该消息的微信转写文字作为对话内容。
2. WHEN 平台收到语音上传请求且音频为合法音频文件, the 平台 SHALL 把音频保存到该分身目录下的 `voices/{contact}/` 中，并记录样本时长、字节数与来源消息 ID。
3. IF 音频字节数超过 5,000,000 字节或时长超过 120 秒, the 平台 SHALL 拒绝保存该样本并返回 HTTP 400。
4. WHEN 同一来源消息 ID 重复上传, the 平台 SHALL 复用已保存样本并返回成功，不产生重复文件。
5. WHILE 某联系人的语音样本数量达到 20 条, the 平台 SHALL 保留最新 20 条并停止新增。

### Requirement 2

**User Story:** AS 分身拥有者, I want 用语音样本克隆音色, so that 分身能用对方的声音说话。

#### Acceptance Criteria

1. WHILE 分身未绑定复刻音色, the 系统 SHALL 使用平台预设音色 `female-1` 对应的 MiniMax 系统音色进行语音合成。
2. WHEN 用户在语音设置中点击「克隆音色」且该联系人语音样本总时长不少于 10 秒, the 系统 SHALL 调用 MiniMax 音色克隆接口创建复刻音色。
3. WHEN MiniMax 音色克隆成功, the 系统 SHALL 把返回的 `voice_id` 与该联系人绑定，并将克隆状态置为 `ready`。
4. IF 语音样本总时长不足 10 秒, the 系统 SHALL 返回 HTTP 400 并提示「语音样本不足，至少需要 10 秒」。
5. IF MiniMax 音色克隆失败, the 系统 SHALL 将克隆状态置为 `failed` 并保存错误原因，用户可重新发起克隆。
6. WHILE 克隆状态为 `pending`, the 系统 SHALL 拒绝针对同一联系人的重复克隆请求。

### Requirement 3

**User Story:** AS 分身拥有者, I want 分身用语音回复, so that 对话更接近和真人聊天。

#### Acceptance Criteria

1. WHILE 分身的 `advanced.reply_voice` 为开启, the 系统 SHALL 在生成文字回复后调用 MiniMax TTS 合成音频。
2. WHEN TTS 合成成功, the 系统 SHALL 把音频作为媒体任务加入出站队列，并发送到当前联系人。
3. WHEN `advanced.reply_voice` 为关闭, the 系统 SHALL 仅发送文字回复，不调用 TTS。
4. IF TTS 合成失败或超时, the 系统 SHALL 仍发送文字回复，并记录一次语音合成失败。
5. WHILE 平台未配置平台密钥, the 系统 SHALL 保持文字回复可用，并跳过语音合成。
6. WHEN 文字回复为空, the 系统 SHALL 跳过 TTS 合成，不发送空语音。

### Requirement 4

**User Story:** AS 分身拥有者, I want 在设置页管理语音, so that 我能控制开关、音色与克隆。

#### Acceptance Criteria

1. WHEN 用户在语音设置页查看音色, the 系统 SHALL 展示当前生效音色（预设或复刻）、克隆状态与可用样本总时长。
2. WHEN 用户切换语音回复开关, the 系统 SHALL 通过 `PATCH /api/personas/{persona_id}` 持久化 `advanced.reply_voice`。
3. WHEN 用户点击「试听」, the 系统 SHALL 用当前音色合成一段固定示例文本并通过页面播放。
4. IF 用户的 MiniMax 音色克隆仍在进行, the 系统 SHALL 在设置页展示进行中状态并禁用「克隆音色」按钮。
5. WHEN 用户选择预设音色, the 系统 SHALL 把该预设音色写入分身设置并在后续合成中生效。
6. WHEN 用户未登录或操作他人分身, the 系统 SHALL 返回 HTTP 401 或 404。

### Requirement 5

**User Story:** AS 平台运营者, I want 语音能力按积分计费, so that 平台不因语音成本亏损。

#### Acceptance Criteria

1. WHEN 管理员配置平台密钥, the 系统 SHALL 在后台「语音设置」中保存 MiniMax API Key 与 TTS 模型名。
2. WHEN 用户发起音色克隆且余额不少于语音克隆成本, the 系统 SHALL 先扣除「语音克隆成本」积分再调用 MiniMax。
3. IF 用户余额少于所需积分, the 系统 SHALL 返回 HTTP 402 并提示积分不足，且不调用 MiniMax。
4. WHEN 音色克隆失败, the 系统 SHALL 退回本次扣除的语音克隆成本。
5. WHEN 一次语音回复合成成功, the 系统 SHALL 在基础每轮扣费之外扣除「语音回复成本」积分。
6. IF 语音回复合成失败, the 系统 SHALL 不扣除本次语音回复成本。

### Requirement 6

**User Story:** AS 平台运营者, I want 语音克隆具备合规提示, so that 平台降低滥用与法律风险。

#### Acceptance Criteria

1. WHEN 用户首次发起音色克隆, the 系统 SHALL 要求用户勾选并确认「已获得声音权利人同意」的声明。
2. WHILE 用户未确认声音权利人同意声明, the 系统 SHALL 拒绝发起音色克隆。
3. WHEN 用户在后台查看语音用量, the 系统 SHALL 展示音色克隆次数与语音回复条数。
