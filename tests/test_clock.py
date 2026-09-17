import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""

from ex_persona import clock  # noqa: E402


class ClockTest(unittest.TestCase):
    def test_now_local_is_timezone_aware(self):
        now = clock.now_local()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), timedelta(hours=8))

    def test_timezone_override_via_env(self):
        prev = os.environ.get("PERSONA_TIMEZONE")
        os.environ["PERSONA_TIMEZONE"] = "UTC"
        try:
            self.assertEqual(clock.now_local().utcoffset(), timedelta(0))
        finally:
            if prev is None:
                os.environ.pop("PERSONA_TIMEZONE", None)
            else:
                os.environ["PERSONA_TIMEZONE"] = prev

    def test_invalid_timezone_falls_back_to_utc8(self):
        prev = os.environ.get("PERSONA_TIMEZONE")
        os.environ["PERSONA_TIMEZONE"] = "Not/AZone"
        try:
            self.assertEqual(clock.now_local().utcoffset(), timedelta(hours=8))
        finally:
            if prev is None:
                os.environ.pop("PERSONA_TIMEZONE", None)
            else:
                os.environ["PERSONA_TIMEZONE"] = prev

    def test_period_boundaries(self):
        cases = {0: "深夜", 5: "深夜", 6: "早上", 10: "早上", 11: "中午",
                 13: "中午", 14: "下午", 17: "下午", 18: "晚上", 22: "晚上",
                 23: "深夜"}
        for hour, expected in cases.items():
            self.assertEqual(clock.period(hour), expected, hour)

    def test_describe_reports_local_wall_clock(self):
        moment = datetime(2026, 9, 17, 19, 50, tzinfo=clock.local_tz())
        self.assertEqual(clock.describe(moment), "2026年9月17日 星期四 晚上 19:50")

    def test_block_mentions_time_and_hides_instruction(self):
        moment = datetime(2026, 9, 17, 8, 5, tzinfo=clock.local_tz())
        text = clock.block(moment)
        self.assertIn("【当下时间】", text)
        self.assertIn("2026年9月17日", text)
        self.assertIn("早上", text)

    def test_gap_hint_ignores_short_gaps(self):
        now = datetime(2026, 9, 17, 12, 0, tzinfo=clock.local_tz())
        recent = (now - timedelta(minutes=30)).astimezone(timezone.utc).isoformat()
        self.assertEqual(clock.gap_hint(recent, now=now), "")

    def test_gap_hint_describes_hours_days_and_missing(self):
        now = datetime(2026, 9, 17, 12, 0, tzinfo=clock.local_tz())
        hour_ago = (now - timedelta(hours=3)).astimezone(timezone.utc).isoformat()
        self.assertIn("小时前", clock.gap_hint(hour_ago, now=now))

        days_ago = (now - timedelta(days=4)).astimezone(timezone.utc).isoformat()
        hint = clock.gap_hint(days_ago, now=now)
        self.assertIn("天前", hint)

        self.assertEqual(clock.gap_hint(None, now=now), "")
        self.assertEqual(clock.gap_hint("not-a-date", now=now), "")

    def test_parse_iso_treats_naive_as_utc(self):
        parsed = clock.parse_iso("2026-09-17T00:00:00")
        self.assertEqual(parsed.tzinfo, timezone.utc)


class SystemPromptTimeTest(unittest.TestCase):
    def test_agent_system_prompt_carries_current_time(self):
        from pathlib import Path

        from ex_persona import agent as agent_module
        from ex_persona.config import LLMConfig

        root = Path(_TMP.name) / f"clock-persona-{os.getpid()}"
        root.mkdir(parents=True, exist_ok=True)
        (root / "profile.json").write_text('{"name": "小鹿"}', encoding="utf-8")
        (root / "persona_card.json").write_text("{}", encoding="utf-8")
        (root / "style.json").write_text("{}", encoding="utf-8")
        (root / "SKILL.md").write_text("你是小鹿", encoding="utf-8")
        instance = agent_module.PersonaAgent(
            root, config=LLMConfig(api_key="k", base_url="x", model="m")
        )

        original = clock.now_local
        try:
            clock.now_local = lambda: datetime(2026, 9, 17, 19, 50, tzinfo=clock.local_tz())
            system = instance.build_system("在吗")
        finally:
            clock.now_local = original
        self.assertIn("【当下时间】", system)
        self.assertIn("2026年9月17日", system)
        self.assertIn("晚上", system)


class MomentsTimeTest(unittest.TestCase):
    def test_build_prompt_uses_local_period(self):
        from ex_persona import moments

        now = datetime(2026, 9, 17, 19, 50, tzinfo=clock.local_tz())
        prompt = moments.build_prompt({}, [], now=now)
        self.assertIn("晚上", prompt)
        self.assertIn("19:50", prompt)

    def test_day_start_uses_local_midnight_in_utc(self):
        from ex_persona import moments

        now = datetime(2026, 9, 17, 12, 0, tzinfo=clock.local_tz())
        self.assertEqual(
            moments._day_start_utc(now), "2026-09-16T16:00:00+00:00"
        )

    def test_day_start_treats_naive_as_local(self):
        from ex_persona import moments

        naive = datetime(2026, 9, 17, 12, 0)
        self.assertEqual(
            moments._day_start_utc(naive), "2026-09-16T16:00:00+00:00"
        )


if __name__ == "__main__":
    unittest.main()
