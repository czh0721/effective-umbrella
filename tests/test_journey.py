import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from ex_persona import humanize, persona_settings, store  # noqa: E402
from ex_persona.scheduler import ProactiveScheduler  # noqa: E402


class HumanizeTest(unittest.TestCase):
    def test_empty_and_short_replies_stay_single(self):
        self.assertEqual(humanize.split_reply("", 3), [])
        self.assertEqual(humanize.split_reply("你好呀", 2), ["你好呀"])
        self.assertEqual(humanize.split_reply("你好呀", 1), ["你好呀"])

    def test_long_reply_splits_into_segments(self):
        text = "。".join(["今天下午我去你说的那家咖啡店坐了坐"] * 20) + "。"
        segments = humanize.split_reply(text, 3)
        self.assertGreaterEqual(len(segments), 2)
        self.assertLessEqual(len(segments), 3)
        self.assertEqual("".join(segments), text)

    def test_blank_lines_split_before_punctuation(self):
        text = "第一段。" + "填充" * 60 + "\n\n第二段。" + "填充" * 60
        segments = humanize.split_reply(text, 2)
        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0].startswith("第一段"))
        self.assertTrue(segments[1].startswith("第二段"))

    def test_newline_reply_splits_even_when_short(self):
        segments = humanize.split_reply("在干嘛\n想你了\n快点回我", 3)
        self.assertEqual(segments, ["在干嘛", "想你了", "快点回我"])

    def test_single_short_line_stays_one_message(self):
        self.assertEqual(humanize.split_reply("嗯嗯，我也想你", 3), ["嗯嗯，我也想你"])

    def test_segment_delay_is_bounded(self):
        self.assertAlmostEqual(humanize.segment_delay(""), 0.8)
        self.assertLessEqual(humanize.segment_delay("x" * 10000), 6.0)
        self.assertGreater(humanize.segment_delay("x" * 200), humanize.segment_delay("x"))


class TuningTest(unittest.TestCase):
    def test_default_tuning_produces_no_hint(self):
        settings = persona_settings.load(None)
        self.assertEqual(persona_settings.tuning_hint(settings), "")

    def test_extremes_produce_guidance(self):
        settings = persona_settings.load(None)
        settings["tuning"] = {"verbosity": 90, "warmth": 90, "humor": 90}
        hint = persona_settings.tuning_hint(settings)
        self.assertIn("人格调校", hint)
        self.assertIn("多说几句", hint)
        self.assertIn("亲密", hint)
        settings["tuning"] = {"verbosity": 10, "warmth": 10, "humor": 5}
        hint = persona_settings.tuning_hint(settings)
        self.assertIn("短", hint)
        self.assertIn("克制", hint)

    def test_tuning_values_clamped(self):
        merged = persona_settings.validate(
            persona_settings.merge_patch(None, {"tuning": {"verbosity": 999, "warmth": -5, "humor": "x"}})
        )
        self.assertEqual(merged["tuning"]["verbosity"], 100)
        self.assertEqual(merged["tuning"]["warmth"], 0)
        self.assertEqual(merged["tuning"]["humor"], 30)

    def test_enabled_nodes_filters_disabled(self):
        settings = persona_settings.load(None)
        self.assertEqual(persona_settings.enabled_nodes(settings), [])
        raw = copy.deepcopy(settings)
        raw["proactive"]["nodes"][0]["enabled"] = True
        self.assertEqual([n["kind"] for n in persona_settings.enabled_nodes(raw)], ["morning"])

    def test_node_validation_coerces_fields(self):
        merged = persona_settings.validate(
            persona_settings.merge_patch(
                None,
                {
                    "proactive": {
                        "nodes": [
                            {"kind": "morning", "enabled": 1, "time": "99:99"},
                            {"kind": "anniversary", "date": "13-40"},
                            {"kind": "care", "idle_hours": 1},
                            {"kind": "bogus", "enabled": True},
                        ]
                    }
                },
            )
        )
        nodes = {node["kind"]: node for node in merged["proactive"]["nodes"]}
        self.assertEqual(len(nodes), 4)
        self.assertTrue(nodes["morning"]["enabled"])
        self.assertEqual(nodes["morning"]["time"], "08:30")
        self.assertEqual(nodes["anniversary"]["date"], "01-01")
        self.assertEqual(nodes["care"]["idle_hours"], 6)
        self.assertNotIn("bogus", nodes)


class _RecordingAgent:
    def __init__(self, text="在的"):
        self.text = text
        self.calls = []

    def reply(self, message, history=None, temperature=None):
        self.calls.append(message)
        return self.text


class SchedulerNodeTest(unittest.TestCase):
    def setUp(self):
        import ex_persona.scheduler as module

        self.module = module
        self.sent = []
        self.turns = []
        self.logs = []
        self.counts = {}
        self.last_proactive = ""
        self.last_turn = ""
        self.agent = _RecordingAgent()
        self.persona = {"id": 7, "user_id": 3, "last_contact": "x@im.wechat", "settings": ""}
        self.originals = {
            "list": module.store.list_proactive_personas,
            "add_turn": module.store.add_turn,
            "add_proactive_log": module.store.add_proactive_log,
            "last_proactive_at": module.store.last_proactive_at,
            "count_proactive_since": module.store.count_proactive_since,
            "last_turn_at": module.store.last_turn_at,
            "load": module.persona_settings.load,
        }
        module.store.list_proactive_personas = lambda: [self.persona]
        module.store.add_turn = lambda *a, **k: self.turns.append((a, k))
        module.store.add_proactive_log = lambda *a, **k: self.logs.append((a, k))
        module.store.last_proactive_at = lambda *a, **k: self.last_proactive
        module.store.count_proactive_since = (
            lambda persona_id, since, kind=None: self.counts.get(kind or "any", 0)
        )
        module.store.last_turn_at = lambda *a, **k: self.last_turn
        module.persona_settings.load = lambda raw: self.settings

    def tearDown(self):
        for name, value in self.originals.items():
            if name == "list":
                self.module.store.list_proactive_personas = value
            elif name == "load":
                self.module.persona_settings.load = value
            else:
                setattr(self.module.store, name, value)

    def _settings(self, nodes, **proactive):
        block = copy.deepcopy(self.module.persona_settings.DEFAULT_SETTINGS)
        block["proactive"].update(
            {"enabled": True, "window_start": "00:00", "window_end": "23:59", **proactive}
        )
        block["proactive"]["nodes"] = [
            dict(node, enabled=True) for node in block["proactive"]["nodes"]
        ]
        for node in block["proactive"]["nodes"]:
            if node["kind"] in nodes:
                node.update(nodes[node["kind"]])
            else:
                node["enabled"] = False
        self.settings = block

    def _scheduler(self):
        return ProactiveScheduler(
            lambda uid, persona: self.agent,
            lambda uid, to, text: self.sent.append((to, text)) or True,
        )

    def test_morning_node_fires_with_kind(self):
        self._settings({"morning": {"time": "06:00"}})
        self._scheduler().tick(datetime(2026, 1, 1, 8, 0))
        self.assertEqual(self.sent, [("x@im.wechat", "在的")])
        self.assertEqual(self.logs[-1][1]["kind"], "morning")
        self.assertIn("早安", self.agent.calls[-1])

    def test_node_skipped_when_already_sent_today(self):
        self._settings({"morning": {"time": "06:00"}}, mode="interval", interval_hours=3)
        self.counts = {"morning": 1}
        self.last_proactive = datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc).isoformat()
        self._scheduler().tick(datetime(2026, 1, 1, 8, 0))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.agent.calls, [])

    def test_node_not_due_before_time(self):
        self._settings({"morning": {"time": "09:00"}}, mode="interval", interval_hours=3)
        self.last_proactive = datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc).isoformat()
        self._scheduler().tick(datetime(2026, 1, 1, 8, 0))
        self.assertEqual(self.sent, [])

    def test_care_node_uses_idle_hours(self):
        self._settings({"care": {"idle_hours": 48}})
        self.last_turn = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc).isoformat()
        self._scheduler().tick(datetime(2026, 1, 5, 8, 0))
        self.assertEqual(self.logs[-1][1]["kind"], "care")
        self.assertIn("没有聊天", self.agent.calls[-1])

    def test_anniversary_node_matches_month_day(self):
        self._settings({"anniversary": {"date": "01-01"}})
        self._scheduler().tick(datetime(2026, 1, 1, 12, 0))
        self.assertEqual(self.logs[-1][1]["kind"], "anniversary")
        self.assertIn("01-01", self.agent.calls[-1])

    def test_timed_node_fires_outside_window(self):
        # 默认时间窗 09:00-22:00，早安节点 08:30 落在窗外，仍应触发。
        self._settings({"morning": {"time": "08:30"}}, window_start="09:00", window_end="22:00")
        self._scheduler().tick(datetime(2026, 1, 1, 8, 30))
        self.assertEqual(self.logs[-1][1]["kind"], "morning")

    def test_timed_node_outside_window_skips_when_too_late(self):
        # 窗外迟到超过上限（120 分钟）时不再补发，避免半夜收到「早安」。
        self._settings({"morning": {"time": "08:00"}}, window_start="09:00", window_end="22:00")
        self._scheduler().tick(datetime(2026, 1, 1, 23, 0))
        self.assertEqual(self.sent, [])

    def test_interval_message_still_respects_window(self):
        # 非节点消息即使在时间窗外满足间隔条件，也不能发送。
        self._settings({}, mode="interval", interval_hours=1,
                       window_start="09:00", window_end="22:00")
        self._scheduler().tick(datetime(2026, 1, 1, 7, 0))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.agent.calls, [])

    def test_failed_attempt_cools_down_before_retry(self):
        # 模型返回 SKIP 后应进入冷却，避免每 60 秒重复调用一次大模型。
        self._settings({}, mode="interval", interval_hours=1)
        self.agent = _RecordingAgent("[[SKIP]]")
        scheduler = self._scheduler()
        scheduler.tick(datetime(2026, 1, 1, 12, 0))
        scheduler.tick(datetime(2026, 1, 1, 12, 1))
        self.assertEqual(len(self.agent.calls), 1)
        # 超过冷却时间后允许再次尝试。
        scheduler.tick(datetime(2026, 1, 1, 12, 11))
        self.assertEqual(len(self.agent.calls), 2)


class JourneyStoreTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _user(self, username):
        return store.create_user(username, "h", "s")

    def test_farewell_saved_and_overwritten(self):
        user = self._user(f"fw-{os.urandom(4).hex()}")
        persona = store.create_persona(user["id"], "小念", "/tmp/x")
        first = store.save_farewell(user["id"], persona["id"], "c1", "第一封信")
        self.assertEqual(first["content"], "第一封信")
        second = store.save_farewell(user["id"], persona["id"], "c2", "第二封信")
        self.assertEqual(second["content"], "第二封信")
        fetched = store.get_farewell(user["id"], persona["id"])
        self.assertEqual(fetched["content"], "第二封信")
        self.assertIsNone(store.get_farewell(user["id"], persona["id"] + 9999))

    def test_timeline_aggregates_turns_memories_and_proactive(self):
        user = self._user(f"tl-{os.urandom(4).hex()}")
        persona = store.create_persona(user["id"], "阿屿", "/tmp/y")
        store.add_turn(user["id"], persona["id"], "user", "在吗", contact="c1")
        store.add_turn(user["id"], persona["id"], "assistant", "在的", contact="c1")
        store.add_memory(user["id"], persona["id"], "c1", "fact", "喜欢咖啡")
        store.add_proactive_log(user["id"], persona["id"], "c1", "早安", kind="morning")
        data = store.timeline(user["id"], persona["id"], "c1")
        self.assertEqual(data["total"], 2)
        self.assertTrue(data["first_at"])
        roles = {row["role"]: row["n"] for row in data["days"]}
        self.assertEqual(roles.get("user"), 1)
        self.assertEqual(roles.get("assistant"), 1)
        self.assertEqual(len(data["memories"]), 1)
        self.assertEqual(data["proactive"][0]["kind"], "morning")

    def test_proactive_kind_filters(self):
        user = self._user(f"pk-{os.urandom(4).hex()}")
        persona = store.create_persona(user["id"], "小屿", "/tmp/z")
        store.add_proactive_log(user["id"], persona["id"], "c1", "早安", kind="morning")
        store.add_proactive_log(user["id"], persona["id"], "c1", "晚安", kind="goodnight")
        self.assertEqual(store.count_proactive_since(persona["id"], "1970-01-01"), 2)
        self.assertEqual(store.count_proactive_since(persona["id"], "1970-01-01", "morning"), 1)
        self.assertEqual(store.last_proactive_at(persona["id"], "goodnight"), store.last_proactive_at(persona["id"], "goodnight"))
        self.assertTrue(store.last_proactive_at(persona["id"], "goodnight"))
        self.assertEqual(store.last_proactive_at(persona["id"], "care"), "")


def _ready_agent(reply):
    class _Config:
        ready = True
        api_key = "k"
        base_url = "https://api.deepseek.com/v1"
        model = "deepseek-chat"
        platform = False

    class _Agent:
        config = _Config()
        last_error = None
        last_fallback = False

        def reply(self, *args, **kwargs):
            return reply

    return _Agent()


class ApiFlowTest(unittest.TestCase):
    """通过真实路由验证时间线与告别接口的接线。"""

    def setUp(self):
        from fastapi.testclient import TestClient

        from ex_persona import webapp as webapp_module

        store.init_db()
        self.webapp = webapp_module
        self.client = TestClient(webapp_module.app)
        self.originals = {"get_agent": webapp_module.get_agent}
        username = f"flow-{os.urandom(4).hex()}"
        self.user = self.client.post(
            "/api/auth/register", json={"username": username, "password": "password123"}
        ).json()["user"]
        self.persona = self.client.post(
            "/api/personas", json={"name": "小屿"}
        ).json()["persona"]

    def tearDown(self):
        self.webapp.get_agent = self.originals["get_agent"]
        self.client.close()

    def test_timeline_endpoint_empty_then_filled(self):
        pid = self.persona["id"]
        empty = self.client.get(f"/api/personas/{pid}/timeline").json()
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["contacts"], [])
        self.assertIsNone(empty["farewell"])
        store.touch_contact(self.user["id"], pid, "c9")
        store.add_turn(self.user["id"], pid, "user", "在吗", contact="c9")
        store.add_turn(self.user["id"], pid, "assistant", "在的", contact="c9")
        data = self.client.get(f"/api/personas/{pid}/timeline?contact=c9").json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["contacts"]), 1)
        self.assertEqual(data["persona"]["name"], "小屿")

    def test_farewell_endpoint_generates_and_retires(self):
        pid = self.persona["id"]
        self.webapp.get_agent = lambda user_id, persona: _ready_agent("谢谢你陪我走过这一段，照顾好自己。")
        response = self.client.post(f"/api/personas/{pid}/farewell", json={})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("照顾好自己", body["farewell"]["content"])
        self.assertFalse(body["delivered"])
        self.assertEqual(store.get_persona(self.user["id"], pid)["status"], "retired")
        read_back = self.client.get(f"/api/personas/{pid}/farewell").json()
        self.assertIn("照顾好自己", read_back["farewell"]["content"])

    def test_farewell_without_ready_agent_is_rejected(self):
        pid = self.persona["id"]
        response = self.client.post(f"/api/personas/{pid}/farewell", json={})
        self.assertEqual(response.status_code, 400)


class ReplySegmentOrderTest(unittest.TestCase):
    """分段回复的后续气泡必须排在首条之后，避免出站线程抢跑导致顺序错乱。"""

    def setUp(self):
        from fastapi.testclient import TestClient

        from ex_persona import webapp as webapp_module

        store.init_db()
        self.webapp = webapp_module
        self.client = TestClient(webapp_module.app)
        username = f"seg-{os.urandom(4).hex()}"
        self.user = self.client.post(
            "/api/auth/register", json={"username": username, "password": "password123"}
        ).json()["user"]
        self.persona = self.client.post(
            "/api/personas", json={"name": "小段"}
        ).json()["persona"]

    def tearDown(self):
        self.client.close()

    def test_segments_queue_after_head_delay(self):
        captured = []
        original = self.webapp._outbox.enqueue

        def fake_enqueue(uid, recipient, text="", media="", delay_seconds=0):
            captured.append((text, float(delay_seconds)))
            return 1

        self.webapp._outbox.enqueue = fake_enqueue
        try:
            queued = self.webapp._queue_text_segments(
                self.user["id"], self.persona, "c1", ["第二段", "第三段"], base_delay=4.0
            )
        finally:
            self.webapp._outbox.enqueue = original
        self.assertEqual(queued, 2)
        self.assertEqual([text for text, _ in captured], ["第二段", "第三段"])
        self.assertGreaterEqual(captured[0][1], 4.0)
        self.assertGreater(captured[1][1], captured[0][1])


class AdminAlertCooldownTest(unittest.TestCase):
    def setUp(self):
        from ex_persona import webapp as webapp_module

        self.webapp = webapp_module
        self.records = []
        self.originals = {
            "list_users": store.list_users,
            "add_alert": store.add_alert,
        }
        store.list_users = lambda: [{"id": 1, "role": "admin"}, {"id": 2, "role": "user"}]
        store.add_alert = lambda user_id, kind, message: self.records.append((user_id, kind, message))
        webapp_module._admin_alert_at.clear()

    def tearDown(self):
        store.list_users = self.originals["list_users"]
        store.add_alert = self.originals["add_alert"]
        self.webapp._admin_alert_at.clear()

    def test_same_kind_is_throttled(self):
        self.webapp._notify_admins("platform", "一号故障")
        self.webapp._notify_admins("platform", "二号故障")
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0], (1, "platform", "一号故障"))

    def test_distinct_kinds_both_recorded(self):
        self.webapp._notify_admins("platform", "平台故障")
        self.webapp._notify_admins("llm", "模型故障")
        self.assertEqual([item[1] for item in self.records], ["platform", "llm"])

    def test_zero_cooldown_always_records(self):
        self.webapp._notify_admins("report", "举报一", cooldown=0)
        self.webapp._notify_admins("report", "举报二", cooldown=0)
        self.assertEqual([item[2] for item in self.records], ["举报一", "举报二"])


if __name__ == "__main__":
    unittest.main()
