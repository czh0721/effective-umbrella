import time

from .config import LLMConfig

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

_clients: dict[tuple[str, str | None], object] = {}

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.8

_AUTH_ERRORS = ("AuthenticationError", "PermissionDeniedError")
_RETRYABLE_ERRORS = (
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
)


class LLMError(RuntimeError):
    """模型调用失败。``auth`` 为真表示 Key 无效或没有权限。"""

    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


def _classify(error: Exception) -> LLMError:
    name = type(error).__name__
    if name in _AUTH_ERRORS:
        return LLMError("模型鉴权失败，请检查 API Key 是否有效", auth=True)
    if name == "RateLimitError" or "RateLimit" in name:
        return LLMError("模型请求过于频繁（已限流），请稍后再试")
    if name in _RETRYABLE_ERRORS or "Timeout" in name:
        return LLMError("模型暂时不可用，请稍后再试")
    return LLMError(f"模型调用失败：{error}")


def _retryable(error: Exception) -> bool:
    name = type(error).__name__
    return name in _RETRYABLE_ERRORS or "Timeout" in name


def make_client(config: LLMConfig):
    if OpenAI is None:
        raise RuntimeError("未安装 openai，请先执行 pip install -r requirements.txt")
    if not config.ready:
        raise RuntimeError(
            "缺少 USER_LLM_API_KEY，请在 .env 中填入你自己的模型 Key"
        )
    key = (config.api_key, config.base_url)
    client = _clients.get(key)
    if client is None:
        kwargs = {"api_key": config.api_key, "timeout": 30.0, "max_retries": 0}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        client = OpenAI(**kwargs)
        _clients[key] = client
    return client


def _request(client, payload: dict):
    """带指数退避的重试；鉴权错误立即失败，其余瞬时错误重试。"""
    last: LLMError | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return client.chat.completions.create(**payload)
        except Exception as error:  # noqa: BLE001 - 统一归类为 LLMError
            classified = _classify(error)
            last = classified
            if classified.auth or not _retryable(error) or attempt == RETRY_ATTEMPTS - 1:
                raise classified from error
            time.sleep(RETRY_BASE_DELAY * (2**attempt))
    raise last or LLMError("模型调用失败")


def chat_with_meta(
    config: LLMConfig,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> tuple[str, str | None]:
    """聊天补全，同时返回 finish_reason（"length" 表示被 max_tokens 截断）。"""
    client = make_client(config)
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature if temperature is None else temperature,
        "max_tokens": config.max_tokens if max_tokens is None else max_tokens,
    }
    if json_mode:
        try:
            response = _request(client, {**payload, "response_format": {"type": "json_object"}})
        except LLMError:
            response = _request(client, payload)
    else:
        response = _request(client, payload)
    choice = response.choices[0]
    return choice.message.content or "", getattr(choice, "finish_reason", None)


def chat(
    config: LLMConfig,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> str:
    return chat_with_meta(
        config, messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
    )[0]
