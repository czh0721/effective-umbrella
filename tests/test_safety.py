import os
import random
import tempfile
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path

if "PERSONA_DATA_DIR" not in os.environ:
    _TMP = tempfile.TemporaryDirectory()
    os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ.setdefault("PERSONA_SECRET_KEY", "unit-test-secret")
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import persona_settings, safety, store, workspace  # noqa: E402
from ex_persona.webapp import app  # noqa: E402


def _unique_user_id() -> int:
    return uuid.uuid4().int % 1_000_000_000 + 1_000_000_000


class SensitiveWordTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "words.txt"
        self.path.write_text("# 注释行\n博彩\n办证刻章\n", encoding="utf-8")
        os.environ["PERSONA_SENSITIVE_WORDS"] = str(self.path)

    def tearDown(self):
        os.environ.pop("PERSONA_SENSITIVE_WORDS", None)

    def test_drop_returns_empty(self):
        text, hit = safety.scrub("一起去博彩", action="drop")
        self.assertEqual(text, "")
        self.assertEqual(hit, "博彩")

    def test_replace_masks_hit(self):
        text, hit = safety.scrub("顺便办证刻章", action="replace")
        self.assertNotIn("办证刻章", text)
        self.assertIn("***", text)
        self.assertEqual(hit, "办证刻章")

    def test_clean_text_untouched(self):
        text, hit = safety.scrub("今天天气不错")
        self.assertEqual(text, "今天天气不错")
        self.assertEqual(hit, "")

    def test_wordlist_hot_reload(self):
        self.assertIn("博彩", safety.load_words())
        self.path.write_text("新词", encoding="utf-8")
        bumped = time.time() + 5
        os.utime(self.path, (bumped, bumped))
        words = safety.load_words()
        self.assertIn("新词", words)
        self.assertNotIn("博彩", words)


class ReplyDelayTest(unittest.TestCase):
    def test_within_bounds_and_longer_for_long_text(self):
        rng = random.Random(7)
        short = safety.reply_delay("好", minimum=3, maximum=15, rng=rng)
        long = safety.reply_delay("字" * 400, minimum=3, maximum=15, rng=rng)
        self.assertGreaterEqual(short, 3)
        self.assertLessEqual(short, 15)
        self.assertGreater(long, short)

    def test_zero_range(self):
        self.assertEqual(safety.reply_delay("hi", minimum=0, maximum=0), 0.0)


class CrisisTest(unittest.TestCase):
    def test_detects_self_harm_intent(self):
        self.assertTrue(safety.detect_crisis("我真的不想活了"))
        self.assertTrue(safety.detect_crisis("很多次想过自杀"))
        self.assertTrue(safety.detect_crisis("活着没意思"))

    def test_ignores_affectionate_hyperbole(self):
        self.assertEqual(safety.detect_crisis("想死你了"), "")
        self.assertEqual(safety.detect_crisis("今天天气不错"), "")

    def test_crisis_reply_lists_resources(self):
        text = safety.crisis_reply("小念")
        self.assertIn("小念", text)
        self.assertIn("12356", text)
        self.assertIn("110", text)


class QuietHoursTest(unittest.TestCase):
    def test_default_window(self):
        settings = {"safety": {"quiet_start": 0, "quiet_end": 7}}
        self.assertTrue(safety.in_quiet_hours(settings, datetime(2026, 1, 1, 3)))
        self.assertFalse(safety.in_quiet_hours(settings, datetime(2026, 1, 1, 12)))

    def test_wraparound_window(self):
        settings = {"safety": {"quiet_start": 22, "quiet_end": 6}}
        self.assertTrue(safety.in_quiet_hours(settings, datetime(2026, 1, 1, 23)))
        self.assertTrue(safety.in_quiet_hours(settings, datetime(2026, 1, 1, 2)))
        self.assertFalse(safety.in_quiet_hours(settings, datetime(2026, 1, 1, 12)))

    def test_equal_bounds_means_disabled(self):
        settings = {"safety": {"quiet_start": 5, "quiet_end": 5}}
        self.assertFalse(safety.in_quiet_hours(settings, datetime(2026, 1, 1, 5)))


class SafetySettingsTest(unittest.TestCase):
    def test_defaults_exist(self):
        settings = persona_settings.load(None)
        self.assertTrue(settings["safety"]["enabled"])
        self.assertEqual(settings["safety"]["sensitive_action"], "replace")
        self.assertTrue(settings["safety"]["offline_alert"])

    def test_validate_clamps_and_normalizes(self):
        merged = persona_settings.merge_patch(None, {
            "safety": {
                "daily_limit": -5,
                "per_minute": 9999,
                "delay_min": 40,
                "delay_max": 5,
                "quiet_start": 99,
                "sensitive_action": "bogus",
            }
        })
        rules = persona_settings.validate(merged)["safety"]
        self.assertEqual(rules["daily_limit"], 0)
        self.assertEqual(rules["per_minute"], 600)
        self.assertEqual(rules["quiet_start"], 23)
        self.assertGreaterEqual(rules["delay_max"], rules["delay_min"])
        self.assertEqual(rules["sensitive_action"], "replace")


class StoreSafetyTest(unittest.TestCase):
    def test_send_events_counted(self):
        store.init_db()
        uid = _unique_user_id()
        store.add_send_event(uid)
        store.add_send_event(uid)
        self.assertEqual(store.count_send_events_since(uid, "2000-01-01T00:00:00+00:00"), 2)

    def test_alerts_lifecycle(self):
        store.init_db()
        uid = _unique_user_id()
        store.add_alert(uid, "offline", "微信掉线了")
        self.assertEqual(store.count_unread_alerts(uid), 1)
        items = store.list_alerts(uid, unread_only=True)
        self.assertEqual(items[0]["kind"], "offline")
        self.assertEqual(items[0]["message"], "微信掉线了")
        store.mark_alerts_read(uid)
        self.assertEqual(store.count_unread_alerts(uid), 0)


class RouteChatSafetyTest(unittest.TestCase):
    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "password123"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _setup_chat(self, client, token, name):
        user = self._register(client, f"{name}-{uuid.uuid4().hex[:8]}")
        persona = client.post("/api/personas", json={"name": name}).json()["persona"]
        store.upsert_wechat_binding(
            user["id"],
            bridge_token=token,
            home_dir=str(workspace.home_dir(user["id"])),
            persona_id=persona["id"],
        )
        return user, persona

    def _fake_agent(self, reply):
        class _Config:
            ready = True

        class _Agent:
            config = _Config()
            last_error = None
            last_fallback = False

            def reply(self, *args, **kwargs):
                return reply

        return _Agent()

    def test_reply_masks_sensitive_word(self):
        path = Path(tempfile.mkdtemp()) / "words.txt"
        path.write_text("博彩\n", encoding="utf-8")
        os.environ["PERSONA_SENSITIVE_WORDS"] = str(path)
        try:
            with TestClient(app) as client:
                _, persona = self._setup_chat(client, "token-safe-reply", "安全")
                from ex_persona import webapp as webapp_module

                original = webapp_module.get_agent
                webapp_module.get_agent = lambda uid, p: self._fake_agent("别碰博彩")
                try:
                    response = client.post(
                        "/v1/chat/completions/token-safe-reply",
                        json={"user": "c1", "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = original
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "别碰***")
        finally:
            os.environ.pop("PERSONA_SENSITIVE_WORDS", None)

    def test_crisis_message_short_circuits_without_llm(self):
        with TestClient(app) as client:
            _, persona = self._setup_chat(client, "token-safe-crisis", "安全")
            from ex_persona import webapp as webapp_module

            def _boom(*args, **kwargs):
                raise AssertionError("危机语境下不应调用大模型")

            original = webapp_module.get_agent
            webapp_module.get_agent = _boom
            try:
                response = client.post(
                    "/v1/chat/completions/token-safe-crisis",
                    json={"user": "c1", "messages": [{"role": "user", "content": "我不想活了"}]},
                )
            finally:
                webapp_module.get_agent = original
            self.assertEqual(response.status_code, 200, response.text)
            content = response.json()["choices"][0]["message"]["content"]
            self.assertIn("12356", content)

    def test_platform_unavailable_replies_instead_of_error(self):
        with TestClient(app) as client:
            user = self._register(client, f"no-platform-{uuid.uuid4().hex[:8]}")
            persona = client.post("/api/personas", json={"name": "未配置"}).json()["persona"]
            store.upsert_wechat_binding(
                user["id"],
                bridge_token="token-no-platform",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            response = client.post(
                "/v1/chat/completions/token-no-platform",
                json={"user": "c1", "messages": [{"role": "user", "content": "在吗"}]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["choices"][0]["message"]["content"])
            kinds = [item["kind"] for item in store.list_alerts(user["id"])]
            self.assertIn("platform", kinds)

    def test_daily_cap_suppresses_reply(self):
        with TestClient(app) as client:
            user, persona = self._setup_chat(client, "token-safe-cap", "限额")
            client.put(
                f"/api/personas/{persona['id']}/settings",
                json={"settings": {"safety": {"daily_limit": 1, "per_minute": 0}}},
            )
            from ex_persona import webapp as webapp_module

            original = webapp_module.get_agent
            webapp_module.get_agent = lambda uid, p: self._fake_agent("在呢")
            try:
                first = client.post(
                    "/v1/chat/completions/token-safe-cap",
                    json={"user": "c1", "messages": [{"role": "user", "content": "第一条"}]},
                )
                second = client.post(
                    "/v1/chat/completions/token-safe-cap",
                    json={"user": "c1", "messages": [{"role": "user", "content": "第二条"}]},
                )
            finally:
                webapp_module.get_agent = original
            self.assertEqual(first.json()["choices"][0]["message"]["content"], "在呢")
            self.assertEqual(second.json()["choices"][0]["message"]["content"], "")

    def test_notifications_endpoint(self):
        with TestClient(app) as client:
            user = self._register(client, f"notify-{uuid.uuid4().hex[:8]}")
            store.add_alert(user["id"], "offline", "微信掉线了")
            data = client.get("/api/notifications").json()
            self.assertGreaterEqual(data["unread"], 1)
            read = client.post("/api/notifications/read")
            self.assertEqual(read.json()["unread"], 0)
            self.assertEqual(store.count_unread_alerts(user["id"]), 0)


if __name__ == "__main__":
    unittest.main()
