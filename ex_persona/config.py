import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


@dataclass
class LLMConfig:
    api_key: str
    base_url: str | None
    model: str
    temperature: float = 0.9
    max_tokens: int = 1024
    fallback_reply: str = ""
    platform: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.api_key)


@dataclass
class PlatformConfig:
    """平台内置模型：由管理员在后台配置，用户未配置 Key 时统一供模。"""

    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    # 每个用户每天可用平台模型的次数上限；0 表示不限制。
    daily_limit: int = 0
    # 是否对用户启用平台模型。
    enabled: bool = False
    # 每轮聊天回复扣减的积分数；0 表示免费。
    per_turn_cost: int = 1
    # 新用户注册赠送的积分数。
    new_user_gift: int = 100
    # 非套餐发放（注册赠送、管理员默认）的积分有效期天数。
    default_credit_days: int = 30
    # 发起一次人格蒸馏预扣的积分数；0 表示免费。
    distill_credit_cost: int = 100
    # 单张蒸馏券的念念币价格；0 表示免费（历史字段，已不再用于计价）。
    distill_ticket_price: int = 60
    # 历史字段（已退役）：原「注册赠送蒸馏次数」。
    # 蒸馏券取消后已并入 new_user_gift，保留列以兼容历史数据与迁移幂等。
    distill_ticket_gift: int = 0

    @property
    def ready(self) -> bool:
        return bool(self.api_key) and self.enabled


def load_llm_config() -> LLMConfig:
    if load_dotenv is not None:
        load_dotenv()

    base_url = (os.getenv("USER_LLM_BASE_URL") or "").strip() or "https://api.deepseek.com/v1"
    return LLMConfig(
        api_key=(os.getenv("USER_LLM_API_KEY") or "").strip(),
        base_url=base_url,
        model=(os.getenv("USER_LLM_MODEL") or "deepseek-chat").strip(),
        temperature=float(os.getenv("USER_LLM_TEMPERATURE") or "0.9"),
        max_tokens=int(os.getenv("USER_LLM_MAX_TOKENS") or "1024"),
        fallback_reply=(os.getenv("USER_LLM_FALLBACK_REPLY") or "").strip(),
    )


def load_platform_config() -> PlatformConfig:
    """读取平台模型配置：优先数据库单例，缺省时回退环境变量。

    未配置数据库记录且未设置 ``PERSONA_PLATFORM_API_KEY`` 时视为未启用。
    """
    if load_dotenv is not None:
        load_dotenv()
    env_key = (os.getenv("PERSONA_PLATFORM_API_KEY") or "").strip()
    try:
        daily_limit = int(os.getenv("PERSONA_PLATFORM_DAILY_LIMIT") or "0")
    except ValueError:
        daily_limit = 0
    config = PlatformConfig(
        api_key=env_key,
        base_url=(os.getenv("PERSONA_PLATFORM_BASE_URL") or "https://api.deepseek.com/v1").strip(),
        model=(os.getenv("PERSONA_PLATFORM_MODEL") or "deepseek-chat").strip(),
        daily_limit=max(daily_limit, 0),
        enabled=bool(env_key),
    )
    try:
        from . import crypto, store
        row = store.get_platform_config_row()
    except Exception:  # noqa: BLE001 - 数据库未就绪时退回环境变量
        row = None
    if row:
        encrypted = row.get("api_key_encrypted") or ""
        if encrypted:
            config.api_key = crypto.decrypt(encrypted)
            config.enabled = bool(row.get("enabled"))
        config.base_url = row.get("base_url") or config.base_url
        config.model = row.get("model") or config.model
        config.per_turn_cost = int(row.get("per_turn_cost") or 0)
        config.new_user_gift = int(row.get("new_user_gift") or 0)
        config.default_credit_days = int(row.get("default_credit_days") or 30)
        cost = row.get("distill_credit_cost")
        config.distill_credit_cost = int(cost) if cost not in (None, "") else 100
        config.distill_ticket_price = int(row.get("distill_ticket_price") or 0)
        config.distill_ticket_gift = int(row.get("distill_ticket_gift") or 0)
    return config
