# 蒸馏改用积分支付（Distill Credit Payment）需求文档

- Feature: `distill-credit-payment`
- 日期: 2026-09-19
- 状态: 已实现并部署（生产迁移已核对）
- 关联: 替换 `2026-09-17-credit-expiry-distill` 中的蒸馏券机制；扩展 `2026-09-18-package-tiers-bonus`

## 背景

当前「人格蒸馏」使用独立货币「蒸馏券」：管理员在 `platform_config` 配置蒸馏券单价
（`distill_ticket_price`，生产为 60 念念币）与注册赠送张数（`distill_ticket_gift`，生产为 1），
用户用念念币购买蒸馏券，发起蒸馏前预扣 1 张，失败退还。

用户决定**取消蒸馏券这一独立货币**，让蒸馏直接消耗积分。理由：钱包货币已统一为「念念币 →
积分」，再维护一层券会让价格体系、赠送、对账、前端展示重复且容易出错。改造后蒸馏按次扣积分，
单价用独立配置项，默认 100 积分。

## 关键决策（用户确认）

1. **计价方式**：蒸馏改为每次扣**积分**，单价为独立配置项 `distill_credit_cost`，默认
   **100 积分/次**（约 ¥0.10/次）。
2. **存量券处理**：生产存量蒸馏券**折算为等值积分**发放给用户（1 张 = 蒸馏单价积分）。
3. **赠送渠道**：套餐赠送的蒸馏券、新用户注册赠送的蒸馏券，**统一改为赠送等值积分**。
4. **保留历史**：`users.distill_tickets`、`distill_ticket_ledger` 表与历史流水**保留不删**，
   仅停止发放与消耗，并在对账时归零。
5. **前端**：用户端与管理端不再出现「蒸馏券」入口、购买按钮与单价配置，改为展示蒸馏所需积分。

## 术语

- **蒸馏积分单价（Distill Credit Cost）**：发起一次人格蒸馏预先扣除的积分数。
- **预扣（Reserve）**：发起蒸馏时先扣积分，蒸馏成功即消耗。
- **退还（Refund）**：蒸馏失败、取消或中断时把预扣积分退回用户。
- **折算（Conversion）**：把存量蒸馏券按单价换算为积分的一次性迁移动作。

## 需求（EARS）

### 配置

- R1: The system shall 在平台配置中提供「蒸馏积分单价」`distill_credit_cost`，默认 100。
- R2: WHEN 管理员通过后台保存平台配置, the system shall 接受并持久化 `distill_credit_cost`，
  且拒绝负值。
- R3: The system shall 保留 `distill_ticket_price` 与 `distill_ticket_gift` 列以兼容历史数据，
  但不再作为蒸馏计价依据。

### 扣费与退还

- R4: WHEN 用户发起人格蒸馏, the system shall 以用户预扣一次 `distill_credit_cost` 积分。
- R5: IF 用户积分不足以支付蒸馏单价, then the system shall 返回 402，并提示所需与当前积分，
  且不创建蒸馏任务。
- R6: WHEN 蒸馏失败、取消或中断, the system shall 幂等地把预扣积分退回用户，同一任务至多退一次。
- R7: WHEN 服务启动发现中断的蒸馏任务, the system shall 对尚未退还的预扣积分执行退还。

### 存量迁移

- R8: WHEN 系统首次升级到积分蒸馏, the system shall 把每个用户的 `distill_tickets` 余额按
  `distill_credit_cost` 折算为积分发放，并把券余额归零。
- R9: The system shall 为折算写入积分流水与蒸馏券流水，记录折算数量与原因，便于对账。
- R10: The system shall 把存在未退还预扣券的中断蒸馏任务同样折算为积分退还。
- R11: The system shall 把套餐中现有的 `bonus_tickets` 按 `distill_credit_cost` 折算进
  `bonus_credits` 并把 `bonus_tickets` 归零，只执行一次且不覆盖管理员后续自定义。
- R12: The system shall 用 `schema_meta` 标记保证折算迁移幂等，重启不重复发放。

### 赠送

- R13: WHEN 新用户注册且配置了蒸馏赠送张数, the system shall 改为发放
  `distill_ticket_gift × distill_credit_cost` 积分，不再发放蒸馏券。
- R14: WHEN 用户购买带 `bonus_tickets` 的套餐, the system shall 按等值积分发放，不再发放蒸馏券。

### 展示与接口

- R15: The system shall 在用户积分信息接口返回 `distill_credit_cost`，不再返回券余额。
- R16: The system shall 停止提供蒸馏券购买接口，对购买请求返回 410。
- R17: The system shall 在用户端「发起蒸馏」「设置」「人设浮层」展示蒸馏所需积分而非券。
- R18: The system shall 移除管理端蒸馏券单价、注册赠送券、套餐赠送券的输入项，改为蒸馏积分
  单价与等值积分赠送展示。

## 非功能需求

- N1: 折算迁移必须在单事务内完成，任一用户失败则整体回滚。
- N2: 折算前必须备份生产数据库。
- N3: 迁移与退还操作全部幂等，支持重复启动。
- N4: 不改动已完成的历史订单与历史积分批次。

## 验收标准

- A1: 蒸馏一次扣 100 积分（默认配置），积分不足返回 402 且不建任务。
- A2: 失败蒸馏的积分原路退还，重复退还只发生一次。
- A3: 生产存量 3 张券折算为 300 积分，券余额归零，流水完整。
- A4: 尊享套餐赠送由「1/2/3 张券」变为「100/200/300 积分」。
- A5: 重启服务不重复折算。
- A6: 全站搜索无「蒸馏券」用户可见文案与入口。
