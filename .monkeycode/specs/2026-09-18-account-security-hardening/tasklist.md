# 账号安全强化 — 实施任务清单

Feature Name: account-security-hardening
Updated: 2026-09-18

## 任务列表

### 密码策略

- [x] `accounts.py` 常量：`PASSWORD_MIN_LENGTH=10`、`PASSWORD_MAX_LENGTH=128`、`WEAK_PASSWORDS`
- [x] `_password_problems` / `validate_password_strength` / `check_password_strength`
- [x] `validate_credentials`、`change_password`、`set_username_password`、`create_admin`、`set_admin_password` 统一接入策略
- [x] 改密成功后写入 `users.last_password_change_at`

### 登录失败锁定

- [x] `users` / `admins` 补列 `failed_attempts` / `failed_first_at` / `locked_until`
- [x] `store.record_login_failure` / `clear_login_failures` / `get_lock_state` / `list_locked_accounts` / `unlock_account`
- [x] `accounts.authenticate` / `authenticate_admin` 先查锁定、失败计数、成功清零
- [x] 锁定响应 429 + `Retry-After`（`webapp._account_error`）

### 强制双因素（仅管理员）

- [x] `FEATURE_FLAG_DEFAULTS` 增加 `admin_force_totp: 0`
- [x] `current_admin` 守卫：待验证 403、未绑定且强开 403
- [x] `require_pending_admin` 依赖
- [x] `_admin_login_response` 支持 `totp_setup_required`
- [x] `admin_login` 强制绑定分支
- [x] `POST /api/admin/account/2fa/setup` / `enable`

### 解锁与审计

- [x] `GET /api/admin/security/locked`
- [x] `POST /api/admin/security/unlock`（写 `security.unlock` 审计）
- [x] 前端后台安全分区：强制开关状态、锁定列表与解锁按钮

### 用户端

- [x] `/api/account/security` 增加 `password_policy` / `last_password_change_at` / `lock`
- [x] `POST /api/account/password/strength`
- [x] 改密后其他会话失效、当前会话换发（沿用 `delete_user_sessions` + `_login_response`）
- [x] `login.html` 注册密码四类提示与客户端校验
- [x] `admin_login.html` 双因素绑定界面
- [x] `settings.html` 修改密码实时强度提示与最近改密时间

### 迁移与测试

- [x] 接入 `init_db`，幂等键 `account_security_migrated`
- [x] 全量测试密码替换为合规强密码（`Password123!`）
- [x] `tests/test_account_security.py` 新增密码策略 / 锁定 / 强制双因素 / 会话失效测试
- [x] `scripts/e2e/run_chain.py` 新增弱密码拒绝、锁定与解锁、管理员强制双因素（109/109）
- [x] 后台 Playwright 渲染探针通过（安全分区）
