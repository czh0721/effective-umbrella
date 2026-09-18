import os
import tempfile
import types
import unittest

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("PERSONA_DATA_DIR", _TMP.name)
os.environ.setdefault("PERSONA_SECRET_KEY", "unit-test-secret")

from ex_persona import llm, metering, pricing, store  # noqa: E402
from ex_persona.agent import CHAT_STYLE_RULES, PersonaAgent  # noqa: E402
from ex_persona.config import LLMConfig  # noqa: E402


class _Usage:
    def __init__(self, prompt: int, completion: int, cached: int = 0) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion
        self.prompt_cache_hit_tokens = cached


def _config() -> LLMConfig:
    return LLMConfig(api_key="x", base_url=None, model="deepseek-chat", platform=True)


class PricingTest(unittest.TestCase):
    def test_estimate_cost(self):
        self.assertEqual(pricing.estimate_cost(), 0.0)
        self.assertAlmostEqual(pricing.estimate_cost(prompt_tokens=1_000_000), 2.0, places=6)
        self.assertAlmostEqual(
            pricing.estimate_cost(prompt_tokens=1_000_000, cached_tokens=1_000_000),
            0.04,
            places=6,
        )
        self.assertAlmostEqual(pricing.estimate_cost(completion_tokens=1_000_000), 8.0, places=6)


class ReadUsageTest(unittest.TestCase):
    def test_object_and_dict(self):
        data = metering.read_usage(_Usage(100, 20, 40))
        self.assertEqual(
            data,
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cached_tokens": 40,
                "total_tokens": 120,
            },
        )
        data = metering.read_usage(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 7},
            }
        )
        self.assertEqual(data["cached_tokens"], 7)
        self.assertEqual(data["total_tokens"], 15)
        self.assertEqual(metering.read_usage(None)["total_tokens"], 0)


class RecordTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def test_record_and_stats_delta(self):
        purpose = "test-meter-alpha"
        before = store.token_usage_stats()
        store.record_token_usage(
            user_id=777001,
            persona_id=1,
            contact="c",
            purpose=purpose,
            model="deepseek-chat",
            platform=1,
            prompt_tokens=1000,
            completion_tokens=200,
            cached_tokens=400,
            total_tokens=1200,
        )
        after = store.token_usage_stats()
        self.assertEqual(after["total_tokens"] - before["total_tokens"], 1200)
        self.assertEqual(after["cached_tokens"] - before["cached_tokens"], 400)
        self.assertEqual(
            after["platform"]["total_tokens"] - before["platform"]["total_tokens"], 1200
        )
        entry = next(p for p in after["by_purpose"] if p["purpose"] == purpose)
        self.assertEqual(entry["total_tokens"], 1200)
        self.assertGreaterEqual(entry["cost_yuan"], 0)
        user = next(u for u in after["by_user"] if u["user_id"] == 777001)
        self.assertEqual(user["total_tokens"], 1200)

    def test_bind_attribution_and_reset(self):
        purpose = "test-meter-beta"
        with metering.bind(777002, 5, purpose, "contact-x"):
            metering.record(_config(), _Usage(300, 100))
        self.assertEqual(metering.current(), {})
        stats = store.token_usage_stats()
        entry = next(p for p in stats["by_purpose"] if p["purpose"] == purpose)
        self.assertEqual(entry["total_tokens"], 400)
        user = next(u for u in stats["by_user"] if u["user_id"] == 777002)
        self.assertGreaterEqual(user["total_tokens"], 400)

    def test_empty_usage_ignored(self):
        before = store.token_usage_stats()["total_tokens"]
        metering.record(_config(), None)
        metering.record(_config(), _Usage(0, 0))
        self.assertEqual(store.token_usage_stats()["total_tokens"], before)


class LlmRecordingTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def test_chat_with_meta_records_usage(self):
        purpose = "test-meter-llm"
        response = types.SimpleNamespace(
            usage=_Usage(40, 5, 10),
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content="你好"),
                    finish_reason="stop",
                )
            ],
        )
        original = llm._request
        llm._request = lambda client, payload: response
        try:
            with metering.bind(888001, 9, purpose):
                text = llm.chat_with_meta(_config(), [{"role": "user", "content": "hi"}])[0]
        finally:
            llm._request = original
        self.assertEqual(text, "你好")
        stats = store.token_usage_stats()
        entry = next(p for p in stats["by_purpose"] if p["purpose"] == purpose)
        self.assertEqual(entry["total_tokens"], 45)
        self.assertEqual(entry["cached_tokens"], 10)


class PromptCostOptimizationTest(unittest.TestCase):
    def _agent(self) -> PersonaAgent:
        return PersonaAgent(
            "data/profile",
            config=LLMConfig(api_key="", base_url=None, model="m"),
        )

    def test_stable_prefix_and_rules_before_volatile(self):
        agent = self._agent()
        first = agent.build_system("你好", "记忆块")
        second = agent.build_system("完全不同的问题", "另一个记忆")
        stable_prefix = agent.persona.skill_text + "\n\n" + CHAT_STYLE_RULES
        self.assertTrue(first.startswith(stable_prefix))
        self.assertTrue(second.startswith(stable_prefix))
        self.assertLess(first.index(CHAT_STYLE_RULES), first.index("【当下时间】"))
        self.assertLess(first.index(CHAT_STYLE_RULES), first.index("记忆块"))

    def test_top_k_trimmed_and_style_samples_capped(self):
        agent = self._agent()
        self.assertEqual(agent.top_k, 2)
        text = agent.build_system("你好", "")
        if "【你以前的说话样本】" in text:
            section = text.split("【你以前的说话样本】\n", 1)[1]
            lines = [line for line in section.splitlines() if line.strip()]
            self.assertLessEqual(len(lines), 3)
