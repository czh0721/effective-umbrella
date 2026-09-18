"""模型调用计价：按 DeepSeek 官方价估算成本（元 / 百万 tokens）。

高峰时段、缓存未命中取较高价，用于成本上界估算；空闲时段价格减半。
价格随官方调整变化，这里用于后台成本看板的量级判断。
"""

# 元 / 百万 tokens（deepseek-flash 高峰价）。
INPUT_MISS_PER_MILLION = 2.0
INPUT_HIT_PER_MILLION = 0.04
OUTPUT_PER_MILLION = 8.0


def estimate_cost(prompt_tokens: int = 0, completion_tokens: int = 0, cached_tokens: int = 0) -> float:
    """按输入（区分缓存命中/未命中）与输出 token 估算成本（元）。"""
    prompt = max(int(prompt_tokens or 0), 0)
    completion = max(int(completion_tokens or 0), 0)
    cached = min(max(int(cached_tokens or 0), 0), prompt)
    miss = prompt - cached
    cost = (
        miss * INPUT_MISS_PER_MILLION
        + cached * INPUT_HIT_PER_MILLION
        + completion * OUTPUT_PER_MILLION
    ) / 1_000_000
    return round(cost, 6)
