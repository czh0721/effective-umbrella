# 蒸馏改用积分支付（Distill Credit Payment）技术设计

- Feature: `distill-credit-payment`
- 日期: 2026-09-19
- 关联需求: `requirements.md`

## 1. 现状

蒸馏券是一条与积分平行的独立货币链：

- 配置：`config.PlatformConfig.distill_ticket_price` / `distill_ticket_gift`，落
  `platform_config` 表同名列。
- 存储：`users.distill_tickets` 余额；`distill_ticket_ledger` 流水；`orders.bonus_tickets`；
  `credit_packages.bonus_tickets`。
- 逻辑：`store.reserve_distill_ticket` / `refund_distill_ticket` / `purchase_distill_ticket` /
  `distill_tickets_summary` / `reconcile_distill_tickets`。
- 接口：`webapp._reserve_distill_ticket` / `_refund_distill_ticket`，注册赠券，
  `/api/credits` 返回券信息，`POST /api/distill-tickets/purchase`。

积分侧已具备完整的预扣/退还能力：

- `store.grant_credits(user_id, delta, reason, actor, ref, idem, expires_days, source, package_id)`：
  `delta < 0` 按最早到期优先消耗批次，`idem` 幂等。
- `store.deduct_credits(user_id, amount, reason, ref)` = `grant_credits(-abs(amount), ...)`。
- `store.InsufficientCredits(balance)`。
- `store._claim_idempotency(conn, user_id, scope, key)` 与 `store._outstanding_reservation`。

## 2. 设计原则

1. **单向迁移**：券 → 积分一次性折算，之后代码只走积分路径。
2. **复用积分原语**：预扣/退还直接复用 `deduct_credits` / `grant_credits` 与幂等表，
   不新增账本表。
3. **旧数据不删**：券表、券列、历史流水保留，仅归零与停用。
4. **迁移幂等**：用 `schema_meta: distill_credits_v1` 标记。

## 3. 配置层

### `ex_persona/config.py`

```python
@dataclass
class PlatformConfig:
    ...
    distill_credit_cost: int = 100   # 新增
    distill_ticket_price: int = 60   # 保留（历史）
    distill_ticket_gift: int = 0     # 保留，语义改为「赠送张数→等值积分」
```

- `load_platform_config()` 读取 `distill_credit_cost`，缺失时回退 `100`。

### `platform_config` 表

```sql
distill_credit_cost INTEGER NOT NULL DEFAULT 100
```

放入 `_MIGRATIONS`，旧库 `ALTER TABLE ... ADD COLUMN` 幂等补列。
`store.set_platform_config(..., distill_credit_cost=None)` 接受并规范化（`max(int(x), 0)`）。
新增读函数：

```python
def distill_credit_cost() -> int:
    row = get_platform_config_row() or {}
    value = row.get("distill_credit_cost")
    return max(int(value), 0) if value not in (None, "") else DISTILL_CREDIT_COST_DEFAULT
```

`DISTILL_CREDIT_COST_DEFAULT = 100` 常量。

## 4. 存储层

### 4.1 预扣与退还（新增）

```python
def reserve_distill_credits(user_id: int, task_id: str) -> dict:
    cost = distill_credit_cost()
    if cost <= 0:
        return {"cost": 0, "reserved": False}
    ref = f"task:{task_id}"
    try:
        result = grant_credits(
            user_id, -cost, reason=DISTILL_RESERVE_REASON, actor="system",
            ref=ref, idem=f"distill.reserve:{task_id}",
        )
    except InsufficientCredits:
        raise
    if result.get("duplicate"):
        return {"cost": cost, "reserved": False, "duplicate": True}
    return {"cost": cost, "reserved": True}

def refund_distill_credits(user_id: int, task_id: str,
                           reason: str = DISTILL_REFUND_REASON) -> dict:
    if not _distill_credit_outstanding(user_id, task_id):
        return {"refunded": False, "duplicate": True}
    if not _claim_idempotency_id(...):  # 见下
        return {"refunded": False, "duplicate": True}
    cost = _reserved_distill_cost(conn, user_id, task_id)
    grant_credits(... +cost ..., idem=f"distill.refund:{task_id}")
    return {"refunded": True, "cost": cost}
```

**约束**：`grant_credits` 内部自己加锁/开连接，因此 reserve/refund 不能在 `_lock` 内调用
`grant_credits`（会死锁，`_lock` 是 `threading.RLock` 时可重入，但 `grant_credits` 自己
`connect()` 会开新连接）。确认 `_lock` 为可重入锁后可在外层持锁调用；若为普通锁则改为
先检查再调用。实现时以现有 `InsufficientCoins`/`purchase_package` 的事务风格为准，
优先复用 `grant_credits` 的独立事务，避免嵌套事务。

**退还额度**：按实际预扣额退。预扣额记录在积分流水 `credit_ledger` 中
（`ref=f"task:{task_id}"` 且 `reason=DISTILL_RESERVE_REASON`）。用 `SUM` 查询获得
`reserved` 与 `refunded` 的差额，与券版本 `_outstanding_reservation` 同构：

```python
def _distill_credit_outstanding(conn, user_id, task_id) -> int:
    # 返回未退还的预扣积分数（0 表示无需退）
    ref = f"task:{task_id}"
    row = conn.execute(
        "SELECT"
        " COALESCE((SELECT SUM(-delta) FROM credit_ledger WHERE user_id=? AND ref=?"
        "   AND reason=? AND delta<0), 0) AS reserved,"
        " COALESCE((SELECT SUM(delta) FROM credit_ledger WHERE user_id=? AND ref=?"
        "   AND reason=? AND delta>0), 0) AS refunded",
        (...), (...)).fetchone()
    return max(int(row["reserved"]) - int(row["refunded"]), 0)
```

### 4.2 对账（改造）

`reconcile_distill_tickets()` 重命名为 `reconcile_distill_credits()`（保留旧名作为不删的
薄封装，防止外部引用断裂）。逻辑：遍历 `kind IN ('distill','redistill')` 且状态
`queued/pending/running` 的任务，若存在未退还预扣积分则退还，并把任务置为
`error/interrupted`。

### 4.3 折算迁移（新增）

```python
def _migrate_distill_credits(conn) -> None:
    if _meta_get(conn, "distill_credits_v1") == "1":
        return
    cost = 蒸馏单价（从 platform_config 读，回退 100）
    now = utcnow()
    # 1) 存量券余额折算
    rows = conn.execute("SELECT id, distill_tickets FROM users WHERE distill_tickets > 0")
    for row:
        tickets = int(row["distill_tickets"])
        credits = tickets * cost
        if credits > 0:
            写 credit_batches（source='migration', reason='蒸馏券折算',
                             expires_at=按 default_credit_days）
            写 credit_ledger（delta=+credits, reason=DISTILL_CONVERT_REASON）
            写 distill_ticket_ledger（delta=-tickets, reason='折算为积分')
        UPDATE users SET distill_tickets = 0
    # 2) 未退还预扣券的中断任务折算
    for task in 中断蒸馏任务:
        写 credit_ledger（delta=+cost, reason='蒸馏中断折算',
                         ref=f"task:{task_id}"）
        写 distill_ticket_ledger（delta=+1, reason=TICKET_REFUND_REASON,
                                 ref=f"task:{task_id}")  # 标记不再 outstanding
    # 3) 套餐赠送券折算进 bonus_credits
    UPDATE credit_packages
       SET bonus_credits = bonus_credits + bonus_tickets * cost,
           bonus_tickets = 0, updated_at = ?
     WHERE bonus_tickets > 0
    _meta_set(conn, "distill_credits_v1", "1")
```

注意：迁移在 `_ensure()` 的 `conn` 中执行，必须使用**同一连接**写批次与流水，不能再调用
`grant_credits`（其会开新连接造成死锁/不可见）。因此迁移内联批次写入逻辑，复用
`credit_batches` 表结构。

**积分到期**：折算积分沿用 `default_credit_days`（生产 30 天），与套餐到账口径一致。

**批次合并**：若用户已有批次，直接新增一个批次即可，无需合并。

### 4.4 套餐档位

`PACKAGE_TIERS` 把 `bonus_tickets` 归零并等值上调 `bonus_credits`：

| 套餐 | 原 bonus_tickets | 新 bonus_credits |
|------|------------------|------------------|
| 尊享月卡 | 1 | 100 |
| 尊享季卡 | 2 | 200 |
| 尊享年卡 | 3 | 300 |
| 其余 | 0 | 不变（0） |

`purchase_package` 的 `bonus_tickets` 分支保留（读列为 0 自然不触发），无需改动。
`gain = base_gain + bonus_credits` 已覆盖赠送积分，无需改。

## 5. 接口层 `ex_persona/webapp.py`

- `_reserve_distill_ticket` → `_reserve_distill_credits`：调用
  `store.reserve_distill_credits`，`except store.InsufficientCredits` → 402，
  文案「蒸馏需要 N 积分，当前 M 积分」。
- `_refund_distill_ticket` → `_refund_distill_credits`：调用
  `store.refund_distill_credits`，失败仅记日志。
- 注册赠券：`if platform.distill_ticket_gift > 0:` 改为
  `store.grant_credits(user["id"], platform.distill_ticket_gift * cost, reason="新用户注册赠送")`。
- `/api/credits`：移除 `distill_tickets` 券余额字段，改为
  `"distill_credit_cost": store.distill_credit_cost()`；保留
  `distill_ticket_ledger` 供历史查询（只读）。
- `POST /api/distill-tickets/purchase`：返回 `410 Gone`，detail「蒸馏券已取消，蒸馏将直接扣积分」。
- `POST /api/distill-tickets/purchase` 的 `DistillTicketPurchase` 模型保留（不删）。
- 管理端平台配置读写：新增 `distill_credit_cost`；`platform` 序列化新增该字段。
- `store.reconcile_distill_tickets()` 调用点改为 `store.reconcile_distill_credits()`。

## 6. 前端

- `web/create_distill.html`：读取 `credits.distill_credit_cost`，提示改为
  「蒸馏消耗 N 积分 · 失败自动退还」；402 文案改积分；移除购买入口。
- `web/agent.html`：402/「蒸馏券」判断改为「积分不足」，文案改积分。
- `web/settings.html`：移除「蒸馏券」入口与购买逻辑（保留其他设置项）。
- `web/admin.html`：平台配置把「蒸馏券单价/注册赠送蒸馏券」改为
  「蒸馏积分单价」；套餐编辑的「赠送蒸馏券」改为「赠送积分」（写 `bonus_credits`）。

后端仍返回 `bonus_tickets` 字段（恒为 0），管理端显示改用 `bonus_credits`。

## 7. 数据与安全

- 迁移前备份 `/opt/nian/data/platform.db` 到 `/opt/nian/backups/`。
- 迁移在 `init_db` 内、`_ensure()` 的单次连接事务中完成，失败回滚且不标记 meta。
- 折算不触碰 `orders`、历史 `credit_ledger`、历史 `distill_ticket_ledger` 行。

## 8. 测试计划

| 层 | 用例 |
|----|------|
| store | 预扣 100 积分、余额不足抛 `InsufficientCredits`、退还幂等、中断任务启动退还 |
| store | 折算迁移：3 张券→300 积分、券归零、重复 `_ensure` 不重复发放、套餐 bonus 转换 |
| webapp | 发起蒸馏扣积分、402 文案、credits 接口含 `distill_credit_cost`、购买接口 410 |
| webapp | 注册赠送 `gift × cost` 积分 |
| 契约 | 前端无「蒸馏券」可见文案 |
| e2e | 现有链不受影响 |

## 9. 风险

| 风险 | 缓解 |
|------|------|
| 生产迁移把券折算错误 | 迁移前备份、幂等标记、流水可对账；部署后查 `credit_ledger` 校验 |
| `_lock` 嵌套调用 `grant_credits` 死锁 | 迁移与退还复用现有积分事务风格；单元测试覆盖并发 |
| 前端遗漏「蒸馏券」文案 | 契约测试全站 grep 断言 |
| 老客户端调用购买接口 | 返回 410 + 明确文案 |
