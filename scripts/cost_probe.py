"""一次性成本探针（运维用）。

用平台内置模型跑一轮代表性对话，打印真实 token 用量与估算成本，用于校准
``ex_persona/pricing.py`` 的单价常量与验证每轮毛利。只读配置，不写业务表；
每次调用只打印用量，密钥不落任何输出。

用法（生产服务器）：

    cd /opt/nian && venv/bin/python scripts/cost_probe.py

可选环境变量：

- ``COST_PROBE_CHARS``：系统提示的目标字符数，默认 6000（约当一次带记忆与检索
  上下文的真实聊天）。
- ``COST_PROBE_CALLS``：连续调用次数，默认 2（第二次用于观察前缀缓存命中）。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ex_persona import llm, metering, pricing  # noqa: E402
from ex_persona.agent import CHAT_STYLE_RULES  # noqa: E402
from ex_persona.config import LLMConfig, load_platform_config  # noqa: E402


def build_system(chars: int) -> str:
    body = []
    while len("\n".join(body)) < chars:
        index = len(body)
        body.append(f"【关于 ta 的记忆】2026-08-{(index % 28) + 1:02d}：ta 提到最近在准备考试，压力有点大。")
    memory = "\n".join(body)
    dialogues = "\n".join(
        f"我：今天第 {i} 次想起你，不知道你在忙什么。\nta：还好，就是有点累。" for i in range(40)
    )
    style = "\n".join(
        f"样本 {i}：嗯，我在的。别太累了，早点休息。" for i in range(12)
    )
    parts = [
        "你正在扮演用户思念的一个人，用 ta 过去的语气陪伴用户。",
        CHAT_STYLE_RULES,
        "【当下时间】2026-09-18 21:00 周五",
        memory,
        "【你们过去的对话】\n" + dialogues,
        "【你以前的说话样本】\n" + style,
    ]
    return "\n\n".join(parts)[:chars]


def main() -> int:
    platform = load_platform_config()
    if not platform.ready:
        print(json.dumps({"ok": False, "reason": "platform model not ready"}, ensure_ascii=False))
        return 1
    config = LLMConfig(
        api_key=platform.api_key,
        base_url=platform.base_url,
        model=platform.model,
        max_tokens=512,
        platform=True,
    )
    chars = int(os.getenv("COST_PROBE_CHARS") or "6000")
    calls = max(int(os.getenv("COST_PROBE_CALLS") or "2"), 1)
    system = build_system(chars)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "在吗？今天有点累。"},
    ]
    client = llm.make_client(config)
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.9,
        "max_tokens": 512,
    }
    results = []
    for index in range(calls):
        started = time.time()
        response = llm._request(client, payload)
        usage = metering.read_usage(getattr(response, "usage", None))
        usage["latency_ms"] = int((time.time() - started) * 1000)
        usage["cost_yuan"] = round(
            pricing.estimate_cost(
                usage["prompt_tokens"], usage["completion_tokens"], usage["cached_tokens"]
            ),
            6,
        )
        results.append(usage)
    per_turn = round(sum(item["cost_yuan"] for item in results) / len(results), 6)
    print(
        json.dumps(
            {
                "ok": True,
                "model": config.model,
                "system_chars": len(system),
                "per_turn_cost_yuan": per_turn,
                "calls": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
