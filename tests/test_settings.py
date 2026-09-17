import unittest
from datetime import datetime

from ex_persona import persona_settings
from ex_persona.scheduler import ProactiveScheduler


class SettingsTest(unittest.TestCase):
    def test_defaults_are_complete(self):
        settings = persona_settings.load(None)
        for key in ("channels", "proactive", "advanced", "voice", "model", "device"):
            self.assertIn(key, settings)

    def test_default_reply_delay_is_bounded(self):
        safety = persona_settings.load(None)["safety"]
        self.assertTrue(safety["reply_delay"])
        self.assertLessEqual(safety["delay_max"], 5.0)
        self.assertLessEqual(safety["delay_min"], safety["delay_max"])

    def test_patch_merges_deeply(self):
        merged = persona_settings.merge_patch(
            None, {"proactive": {"enabled": True}, "advanced": {"allow_silence": True}}
        )
        self.assertTrue(merged["proactive"]["enabled"])
        self.assertEqual(merged["proactive"]["interval_hours"], 3)
        self.assertTrue(merged["advanced"]["allow_silence"])
        self.assertEqual(merged["advanced"]["merge_seconds"], 4)

    def test_validate_clamps_values(self):
        merged = persona_settings.merge_patch(
            None,
            {
                "proactive": {"interval_hours": 999, "window_start": "99:99"},
                "model": {"temperature": 5, "provider": "unknown", "memory_mode": "weird"},
                "advanced": {"merge_seconds": -3},
            },
        )
        validated = persona_settings.validate(merged)
        self.assertEqual(validated["proactive"]["interval_hours"], 168)
        self.assertEqual(validated["proactive"]["window_start"], "09:00")
        self.assertEqual(validated["model"]["temperature"], 1.3)
        self.assertEqual(validated["model"]["provider"], "system")
        self.assertEqual(validated["model"]["memory_mode"], "standard")
        self.assertEqual(validated["advanced"]["merge_seconds"], 0)

    def test_long_term_memory_defaults_and_validate(self):
        settings = persona_settings.load(None)
        self.assertTrue(settings["model"]["long_term_memory"])
        self.assertEqual(settings["model"]["memory_extract_every"], 6)
        merged = persona_settings.validate(
            persona_settings.merge_patch(None, {"model": {"memory_extract_every": 999}})
        )
        self.assertEqual(merged["model"]["memory_extract_every"], 50)
        merged = persona_settings.validate(
            persona_settings.merge_patch(None, {"model": {"memory_extract_every": 1}})
        )
        self.assertEqual(merged["model"]["memory_extract_every"], 2)
        merged = persona_settings.validate(
            persona_settings.merge_patch(None, {"model": {"long_term_memory": False}})
        )
        self.assertFalse(merged["model"]["long_term_memory"])

    def test_in_window(self):
        settings = persona_settings.load(None)
        self.assertTrue(persona_settings.in_window(settings, datetime(2026, 1, 1, 12, 0)))
        self.assertFalse(persona_settings.in_window(settings, datetime(2026, 1, 1, 23, 0)))

    def test_memory_limit(self):
        settings = persona_settings.load(None)
        settings["model"]["memory_mode"] = "saver"
        self.assertEqual(persona_settings.memory_limit(settings), 6)
        settings["model"]["memory_mode"] = "deep"
        self.assertEqual(persona_settings.memory_limit(settings), 40)
        settings["model"]["memory_mode"] = "none"
        self.assertEqual(persona_settings.memory_limit(settings), 0)

    def test_legacy_memory_mode_migrates(self):
        merged = persona_settings.validate(persona_settings.load('{"model": {"memory_mode": "recent"}}'))
        self.assertEqual(merged["model"]["memory_mode"], "saver")
        merged = persona_settings.validate(persona_settings.load('{"model": {"memory_mode": "full"}}'))
        self.assertEqual(merged["model"]["memory_mode"], "standard")

    def test_provider_model_presets(self):
        base, model = persona_settings.provider_model(
            "kimi", "https://api.deepseek.com/v1", "deepseek-chat"
        )
        self.assertEqual(base, "https://api.moonshot.cn/v1")
        self.assertEqual(model, "moonshot-v1-8k")
        base, model = persona_settings.provider_model(
            "minimax", "https://api.deepseek.com/v1", "deepseek-chat"
        )
        self.assertEqual(base, "https://api.minimax.cn/v1")
        self.assertEqual(model, "MiniMax-M3")
        base, model = persona_settings.provider_model(
            "system", "https://my.host/v1", "my-model"
        )
        self.assertEqual(base, "https://my.host/v1")
        self.assertEqual(model, "my-model")


    def test_validate_proactive_fields(self):
        merged = persona_settings.merge_patch(
            None,
            {"proactive": {"mode": "weird", "idle_hours": 999, "min_gap_minutes": 1, "max_per_day": 0}},
        )
        validated = persona_settings.validate(merged)
        self.assertEqual(validated["proactive"]["mode"], "interval")
        self.assertEqual(validated["proactive"]["idle_hours"], 72)
        self.assertEqual(validated["proactive"]["min_gap_minutes"], 5)
        self.assertEqual(validated["proactive"]["max_per_day"], 1)
        merged = persona_settings.validate(
            persona_settings.merge_patch(None, {"proactive": {"mode": "smart"}})
        )
        self.assertEqual(merged["proactive"]["mode"], "smart")


class _FakeAgent:
    def __init__(self, text="在的，想你了"):
        self.text = text
        self.calls = []

    def reply(self, message, history=None, temperature=None):
        self.calls.append(message)
        return self.text


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        import ex_persona.scheduler as module

        self.module = module
        self.sent = []
        self.turns = []
        self.logs = []
        self.recent = []
        self.fixed = {"last_proactive": "", "count": 0, "last_turn": ""}
        self.agent = _FakeAgent()
        self.personas = [
            {"id": 1, "user_id": 1, "last_contact": "x@im.wechat", "settings": ""}
        ]
        self.settings = self._settings()
        self.originals = {
            "list": module.store.list_proactive_personas,
            "add_turn": module.store.add_turn,
            "add_proactive_log": module.store.add_proactive_log,
            "last_proactive_at": module.store.last_proactive_at,
            "count_proactive_since": module.store.count_proactive_since,
            "last_turn_at": module.store.last_turn_at,
            "list_turns": module.store.list_turns,
            "load": module.persona_settings.load,
        }
        module.store.list_proactive_personas = lambda: self.personas
        module.store.add_turn = lambda *args, **kwargs: self.turns.append((args, kwargs))
        module.store.add_proactive_log = lambda *args, **kwargs: self.logs.append((args, kwargs))
        module.store.last_proactive_at = lambda persona_id: self.fixed["last_proactive"]
        module.store.count_proactive_since = lambda persona_id, since: self.fixed["count"]
        module.store.last_turn_at = (
            lambda user_id, persona_id, contact="", role="user": self.fixed["last_turn"]
        )
        module.store.list_turns = (
            lambda user_id, persona_id, limit=8, contact=None: self.recent
        )
        module.persona_settings.load = lambda raw: self.settings

    def tearDown(self):
        module = self.module
        module.store.list_proactive_personas = self.originals["list"]
        module.store.add_turn = self.originals["add_turn"]
        module.store.add_proactive_log = self.originals["add_proactive_log"]
        module.store.last_proactive_at = self.originals["last_proactive_at"]
        module.store.count_proactive_since = self.originals["count_proactive_since"]
        module.store.last_turn_at = self.originals["last_turn_at"]
        module.store.list_turns = self.originals["list_turns"]
        module.persona_settings.load = self.originals["load"]

    def _settings(self, **proactive):
        import copy

        block = copy.deepcopy(self.module.persona_settings.DEFAULT_SETTINGS)
        block["proactive"].update({"enabled": True, **proactive})
        return block

    def _scheduler(self):
        return ProactiveScheduler(
            lambda uid, persona: self.agent,
            lambda uid, to, text: self.sent.append((to, text)) or True,
        )

    def _now(self):
        from datetime import timezone

        return datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def _idle_iso(self, hours):
        from datetime import timedelta

        return (self._now() - timedelta(hours=hours)).isoformat()

    def test_tick_sends_when_enabled(self):
        self._scheduler().tick(self._now())
        self.assertEqual(self.sent, [("x@im.wechat", "在的，想你了")])

    def test_smart_sends_when_idle(self):
        self.settings = self._settings(mode="smart", idle_hours=6)
        self.fixed["last_turn"] = self._idle_iso(10)
        self.recent = [{"role": "user", "content": "先忙啦"}, {"role": "assistant", "content": "好"}]
        self._scheduler().tick(self._now())
        self.assertEqual(self.sent, [("x@im.wechat", "在的，想你了")])
        self.assertIn("最近对话", self.agent.calls[-1])
        self.assertIn("对方：先忙啦", self.agent.calls[-1])

    def test_smart_skips_when_not_idle(self):
        self.settings = self._settings(mode="smart", idle_hours=6)
        self.fixed["last_turn"] = self._idle_iso(1)
        self._scheduler().tick(self._now())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.agent.calls, [])

    def test_smart_skips_on_model_marker(self):
        self.settings = self._settings(mode="smart", idle_hours=6)
        self.fixed["last_turn"] = self._idle_iso(10)
        self.agent = _FakeAgent("[[SKIP]]")
        self._scheduler().tick(self._now())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.turns, [])
        self.assertEqual(self.logs, [])

    def test_tick_respects_daily_cap(self):
        self.settings = self._settings(mode="smart", max_per_day=2)
        self.fixed["count"] = 2
        self._scheduler().tick(self._now())
        self.assertEqual(self.sent, [])

    def test_tick_respects_min_gap(self):
        from datetime import timedelta

        self.settings = self._settings(mode="smart", min_gap_minutes=60)
        self.fixed["last_proactive"] = (self._now() - timedelta(minutes=10)).isoformat()
        self._scheduler().tick(self._now())
        self.assertEqual(self.sent, [])


class MomentsSettingsTest(unittest.TestCase):
    def test_moments_defaults(self):
        moments = persona_settings.load(None)["moments"]
        self.assertFalse(moments["enabled"])
        self.assertEqual(moments["max_per_day"], 2)
        self.assertEqual(moments["min_gap_minutes"], 180)
        self.assertEqual(moments["prompt"], "")

    def test_moments_validate_clamps(self):
        merged = persona_settings.validate(
            persona_settings.merge_patch(
                None,
                {"moments": {"enabled": True, "max_per_day": 99, "min_gap_minutes": 1}},
            )
        )
        self.assertTrue(merged["moments"]["enabled"])
        self.assertEqual(merged["moments"]["max_per_day"], 10)
        self.assertEqual(merged["moments"]["min_gap_minutes"], 30)
        merged = persona_settings.validate(
            persona_settings.merge_patch(None, {"moments": {"max_per_day": 0, "prompt": "x" * 500}})
        )
        self.assertEqual(merged["moments"]["max_per_day"], 1)
        self.assertEqual(len(merged["moments"]["prompt"]), 200)


if __name__ == "__main__":
    unittest.main()
