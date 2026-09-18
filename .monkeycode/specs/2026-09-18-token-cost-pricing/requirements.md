# Token 计量与套餐定价（Token Cost & Package Pricing）需求文档

- Feature: `token-cost-pricing`
- 日期: 2026-09-18
- 状态: 已实现（真实成本核对待生产流量）
- 关联: 扩展 `2026-09-18-package-tiers-bonus`

## 背景

用户在确认积分套餐时提出两条商业约束：1 元 = 10 念念币；套餐定价与每轮扣费必须保证平台
盈利。此前系统只按固定积分扣费，无法知道一次对话真实消耗多少上游 token，也无法反推每轮
成本与毛利。因此先补齐 token 计量与成本换算，再用真实成本反推套餐档位，避免拍脑袋定价。

## 关键决策（用户确认）

1. **货币与扣费口径**：1 元 = 10 念念币；1 念念币 = 100 积分；每轮扣 20 积分，即每轮
   售价 ¥0.02。
2. **毛利目标**：毛利率 60%，按上游模型成本 ¥0.008/轮反推。
3. **成本口径**：按平台模型（DeepSeek 官方直连）估算；核算覆盖全部 token 消耗，包含
   聊天、记忆抽取、朋友圈、蒸馏与主动消息。
4. **先计量后定价**：先做成本优化与 token 计量，再定套餐，用真实数据验证毛利。
5. **档位结构**：30/90/365 天各轻享/标准/尊享三档，另保留入门「体验包」。

## 术语

- **Token 用量（Token Usage）**：一次模型调用返回的 prompt / completion / cached 与合计
  token 数。
- **用途（Purpose）**：产生 token 消耗的业务场景，取值 `chat` / `memory` / `moment` /
  `distill` / `unknown`。
- **估算成本（Estimated Cost）**：按定价常量把 token 数换算成人民币的近似值。
- **毛利（Gross Margin）**：`1 - 单轮成本 / 单轮售价`。

## 需求（EARS）

### Token 计量

- R1: WHEN 一次模型调用成功返回, the system shall 记录该次调用的 prompt、completion、
  cached 与总 token 数。
- R2: The system shall 为每次记录标注用户、人格、聊天对象、模型、是否平台模型与用途。
- R3: IF 模型响应缺少 usage 字段, then the system shall 跳过该次记录且不阻断业务流程。
- R4: IF token 计量写入失败, then the system shall 记录告警并让主流程正常返回。
- R5: The system shall 在聊天、记忆抽取、朋友圈、蒸馏等调用点声明各自的用途。

### 成本换算

- R6: The system shall 按输入未命中、输入命中、输出三档单价把 token 数换算为人民币。
- R7: WHEN 管理员查询成本, the system shall 返回总量、按用途、按用户与平台模型口径的
  汇总与估算成本。

### 成本优化

- R8: The system shall 把系统提示中稳定不变的内容（人设、行为规则）放在最前，变动内容
  （时间、记忆、检索样本）放在其后，以命中模型侧前缀缓存。
- R9: The system shall 限制平台模型的单次输出长度，并降低检索对话与说话样本的注入条数。

### 套餐定价

- R10: The system shall 提供 30/90/365 天各三档的套餐板，每档积分 = 念念币价 × 100。
- R11: The system shall 让每档套餐在 ¥0.008/轮成本口径下毛利率约为 60%。
- R12: WHEN 系统首次升级到新套餐板, the system shall 按名称写入或更新档位，并下架被替换
  的旧跨时长套餐，且只执行一次。
- R13: IF 管理员在后台修改了套餐, then the system shall 在后续启动时保留该修改，不用默认
  值覆盖。
- R14: The system shall 尊享三档分别附赠 1 / 2 / 3 张蒸馏券。
- R15: The system shall 只提供 30/90/365 天各三档套餐，不提供入门体验包。

## 验收

- 单元测试覆盖计量归属、usage 缺失、成本换算、套餐档位数据与 60% 毛利、后台修改不被迁移
  覆盖。
- 全链路回归覆盖套餐板含 9 档、旧套餐已下架、毛利约 60%。
- 生产部署后核对套餐板数据与旧套餐下架状态。
