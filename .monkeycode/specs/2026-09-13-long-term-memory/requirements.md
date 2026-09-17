# 长期记忆（Long-Term Memory）需求文档

- Feature: `long-term-memory`
- 日期: 2026-09-13
- 状态: 已评审（用户确认三项关键决策）

## 背景

当前「念念 Nian」的对话上下文来自 `chat_turns` 表，只取最近 N 条原文轮次作为历史
（N 由 `memory_mode` 档位决定），并且按 `persona_id` 存储、不区分聊天对象。当
对话轮次超过上限后，早期信息会永久丢失，人格无法"记得"很久以前聊过的事。

用户明确要求引入长期记忆：自动从对话中抽取关键信息并持久保存，回复时按当前话题
召回相关记忆注入上下文。

## 关键决策（用户确认）

1. **隔离维度**：按聊天对象隔离。同一个微信联系人一套记忆，A 说的话不会出现在
   B 的对话里。
2. **记忆内容**：自动抽取关键信息（事实/偏好/事件/关系），同时保留原始对话轮次。
3. **召回方式**：关键词检索 BM25，复用现有 `retrieval.BM25`，不依赖额外 embedding
   Key。

## 术语

- **联系人（contact）**：一个微信发送方，用其 `from_user_id` 作为 `contact_key`。
- **长期记忆（memory）**：从对话中抽取的一条结构化信息，含 `kind` 与 `content`。
- **抽取（extract）**：调用用户配置的大模型，把若干轮对话归纳为若干条记忆。
- **召回（recall）**：用当前用户消息对记忆集合做 BM25 检索，取最相关的若干条。

## 需求（EARS）

### 记忆隔离

- R1: When 平台收到一条来自某联系人的微信消息, the system shall 以该联系人的
  `from_user_id` 作为 `contact_key` 归属会话与记忆。
- R2: While 存在 `contact_key`, the system shall 仅使用该联系人名下的历史轮次与
  长期记忆来构造上下文。
- R3: If 请求未携带联系人标识, the system shall 回退到按人格维度处理，保持与旧
  行为兼容。

### 记忆抽取

- R4: When 一轮对话完成后新增的未抽取轮次达到设定阈值, the system shall 在后台
  异步调用大模型抽取记忆。
- R5: The system shall 抽取的事实满足：不含系统提示词原文、不含礼貌寒暄等无信息
  内容、单条内容不超过 200 字。
- R6: If 抽取结果与已有记忆重复, the system shall 跳过重复项。
- R7: If 用户未配置大模型 Key 或抽取失败, the system shall 静默跳过且不影响正常
  回复。

### 记忆召回

- R8: When 准备回复, the system shall 用当前用户消息对当前联系人+人格的记忆集合
  做 BM25 检索。
- R9: The system shall 只注入得分大于 0 的前 K 条记忆（K 默认 6）。
- R10: The system shall 将召回的长期记忆以独立区块注入系统提示词，并明确标注为
  背景记忆。

### 记忆管理

- R11: The system shall 提供按人格+联系人查看长期记忆的接口。
- R12: When 用户删除某条记忆, the system shall 永久移除该条记忆且不影响其他记忆。
- R13: The system shall 提供清空某联系人全部记忆的能力。

### 开关

- R14: While `long_term_memory` 关闭, the system shall 不抽取也不注入长期记忆，仅
  保留最近轮次上下文。
- R15: When 用户修改记忆开关或抽取间隔, the system shall 持久化到人格设置。

### 兼容

- R16: The system shall 在数据库升级时自动补齐 `chat_turns.contact` 列，历史数据
  `contact` 默认为空字符串。

## 非目标

- 不做向量语义检索。
- 不做跨人格共享记忆。
- 不做记忆的人工编辑（仅查看与删除）。
- 不引入新的外部存储服务。

## 验收标准

- 同一人格下，联系人 A 的记忆不会出现在联系人 B 的上下文中。
- 关闭开关后，历史仍按人格维度取最近轮次。
- 抽取失败不阻断回复。
- 删除单条记忆后该内容不再被召回。
- 数据库可向后兼容升级。
