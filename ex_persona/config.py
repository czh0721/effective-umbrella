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
    # 语音能力：由管理员在后台选择提供商并配置密钥，供 TTS 与音色克隆统一使用。
    # voice_provider 取 "minimax" 或 "doubao"。
    voice_provider: str = "minimax"
    minimax_api_key: str = ""
    voice_tts_model: str = "speech-02-turbo"
    # 豆包（火山引擎）：新版控制台 API Key 或旧版 AppID + Access Token 二选一。
    doubao_api_key: str = ""
    doubao_app_id: str = ""
    doubao_access_token: str = ""
    # 豆包资源 ID 覆盖；留空时按音色自动推断（复刻 seed-icl-2.0 / 2.0 seed-tts-2.0）。
    doubao_resource_id: str = ""
    # 音色克隆一次扣减的积分；0 表示免费。
    voice_clone_cost: int = 500
    # 每条语音回复在基础每轮扣费之外额外扣减的积分；0 表示免费。
    voice_reply_cost: int = 20
    # 是否启用平台的语音合成与克隆能力。
    voice_enabled: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.api_key) and self.enabled

    @property
    def voice_credentials_ready(self) -> bool:
        """当前提供商是否至少配置了一组可用凭据。"""
        if self.voice_provider == "doubao":
            return bool(self.doubao_api_key) or bool(
                self.doubao_app_id and self.doubao_access_token
            )
        return bool(self.minimax_api_key)

    @property
    def voice_ready(self) -> bool:
        return self.voice_credentials_ready and self.voice_enabled


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
    env_voice_key = (os.getenv("PERSONA_MINIMAX_API_KEY") or "").strip()
    env_doubao_key = (os.getenv("PERSONA_DOUBAO_API_KEY") or "").strip()
    env_doubao_appid = (os.getenv("PERSONA_DOUBAO_APP_ID") or "").strip()
    env_doubao_token = (os.getenv("PERSONA_DOUBAO_ACCESS_TOKEN") or "").strip()
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
        minimax_api_key=env_voice_key,
        doubao_api_key=env_doubao_key,
        doubao_app_id=env_doubao_appid,
        doubao_access_token=env_doubao_token,
        voice_enabled=bool(env_voice_key or env_doubao_key or (env_doubao_appid and env_doubao_token)),
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
        voice_encrypted = row.get("minimax_api_key_encrypted") or ""
        if voice_encrypted:
            config.minimax_api_key = crypto.decrypt(voice_encrypted)
        config.voice_tts_model = row.get("voice_tts_model") or config.voice_tts_model
        clone_cost = row.get("voice_clone_cost")
        config.voice_clone_cost = int(clone_cost) if clone_cost not in (None, "") else 500
        reply_cost = row.get("voice_reply_cost")
        config.voice_reply_cost = int(reply_cost) if reply_cost not in (None, "") else 20
        config.voice_enabled = bool(row.get("voice_enabled"))
        provider = (row.get("voice_provider") or "").strip().lower()
        if provider:
            config.voice_provider = provider
        doubao_encrypted = row.get("doubao_api_key_encrypted") or ""
        if doubao_encrypted:
            config.doubao_api_key = crypto.decrypt(doubao_encrypted)
        config.doubao_app_id = row.get("doubao_app_id") or config.doubao_app_id
        doubao_token_encrypted = row.get("doubao_access_token_encrypted") or ""
        if doubao_token_encrypted:
            config.doubao_access_token = crypto.decrypt(doubao_token_encrypted)
        config.doubao_resource_id = row.get("doubao_resource_id") or config.doubao_resource_id
    if not config.minimax_api_key and env_voice_key:
        config.minimax_api_key = env_voice_key
    if not config.doubao_api_key and env_doubao_key:
        config.doubao_api_key = env_doubao_key
    if not config.doubao_app_id and env_doubao_appid:
        config.doubao_app_id = env_doubao_appid
    if not config.doubao_access_token and env_doubao_token:
        config.doubao_access_token = env_doubao_token
    if config.voice_provider not in ("minimax", "doubao"):
        config.voice_provider = "minimax"
    if config.voice_credentials_ready and row is None:
        config.voice_enabled = True
    return config
