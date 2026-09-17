import os
import tempfile
import unittest
import uuid

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from datetime import datetime
from types import SimpleNamespace

from ex_persona import moments, persona_settings, store  # noqa: E402


class MomentStoreTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _user(self, prefix="mom"):
        return store.create_user(f"{prefix}-{uuid.uuid4().hex[:8]}", "h", "s")

    def _persona(self, user, name="小念"):
        return store.create_persona(
            user["id"], name, f"/tmp/{uuid.uuid4().hex}", settings=""
        )

    def test_persona_isolation(self):
        owner = self._user()
        other = self._user()
        persona = self._persona(owner)
        other_persona = self._persona(other)
        store.add_moment(owner["id"], persona["id"], "今天天气很好")
        store.add_moment(other["id"], other_persona["id"], "别人的动态")

        mine = store.list_moments(owner["id"], persona["id"])
        self.assertEqual([item["content"] for item in mine], ["今天天气很好"])
        self.assertEqual(store.list_moments(other["id"], persona["id"]), [])
        self.assertEqual(store.list_moments(owner["id"], other_persona["id"]), [])

    def test_cross_user_get_and_delete_return_none(self):
        owner = self._user()
        other = self._user()
        persona = self._persona(owner)
        moment = store.add_moment(owner["id"], persona["id"], "私有内容")

        self.assertIsNone(store.get_moment(other["id"], moment["id"]))
        self.assertFalse(store.delete_moment(other["id"], moment["id"]))
        self.assertIsNotNone(store.get_moment(owner["id"], moment["id"]))

    def test_cursor_pagination_is_descending(self):
        user = self._user()
        persona = self._persona(user)
        for index in range(5):
            store.add_moment(user["id"], persona["id"], f"动态{index}")

        first = store.list_moments(user["id"], persona["id"], limit=2)
        self.assertEqual([item["content"] for item in first], ["动态4", "动态3"])
        second = store.list_moments(
            user["id"], persona["id"], limit=2, before_id=first[-1]["id"]
        )
        self.assertEqual([item["content"] for item in second], ["动态2", "动态1"])

    def test_like_is_idempotent(self):
        user = self._user()
        persona = self._persona(user)
        moment = store.add_moment(user["id"], persona["id"], "给我点赞吧")

        self.assertTrue(store.set_moment_like(moment["id"], user["id"], True))
        self.assertTrue(store.set_moment_like(moment["id"], user["id"], True))
        self.assertEqual(store.count_moment_likes(moment["id"]), 1)
        listed = store.list_moments(user["id"], persona["id"])[0]
        self.assertEqual(listed["like_count"], 1)
        self.assertEqual(listed["liked"], 1)

        self.assertFalse(store.set_moment_like(moment["id"], user["id"], False))
        self.assertEqual(store.count_moment_likes(moment["id"]), 0)
        self.assertEqual(store.list_moments(user["id"], persona["id"])[0]["liked"], 0)

    def test_delete_moment_cascades_likes_and_comments(self):
        user = self._user()
        persona = self._persona(user)
        moment = store.add_moment(user["id"], persona["id"], "会被删除")
        other = store.add_moment(user["id"], persona["id"], "需要保留")
        store.set_moment_like(moment["id"], user["id"], True)
        store.add_moment_comment(moment["id"], user["id"], "第一条评论")

        self.assertTrue(store.delete_moment(user["id"], moment["id"]))
        self.assertIsNone(store.get_moment(user["id"], moment["id"]))
        self.assertEqual(store.count_moment_likes(moment["id"]), 0)
        self.assertEqual(store.list_moment_comments(moment["id"]), [])
        self.assertEqual(store.count_moment_likes(other["id"]), 0)
        self.assertIsNotNone(store.get_moment(user["id"], other["id"]))

    def test_comment_reply_lifecycle(self):
        user = self._user()
        persona = self._persona(user)
        moment = store.add_moment(user["id"], persona["id"], "评论测试")
        comment = store.add_moment_comment(moment["id"], user["id"], "在吗")
        self.assertEqual(comment["reply_status"], "pending")
        self.assertTrue(store.set_moment_comment_reply(comment["id"], "在的", "done"))
        stored = store.get_moment_comment(comment["id"])
        self.assertEqual(stored["reply"], "在的")
        self.assertEqual(stored["reply_status"], "done")
        self.assertEqual(len(store.list_moment_comments(moment["id"])), 1)
        listed = store.list_moments(user["id"], persona["id"])[0]
        self.assertEqual(listed["comment_count"], 1)

    def test_count_and_last_moment(self):
        user = self._user()
        persona = self._persona(user)
        self.assertEqual(store.count_moments_since(persona["id"], ""), 0)
        self.assertEqual(store.last_moment_at(persona["id"]), "")
        store.add_moment(user["id"], persona["id"], "一")
        latest = store.add_moment(user["id"], persona["id"], "二")
        self.assertEqual(store.count_moments_since(persona["id"], ""), 2)
        self.assertEqual(store.count_moments_since(persona["id"], "9999"), 0)
        self.assertEqual(store.last_moment_at(persona["id"]), latest["created_at"])

    def test_delete_persona_removes_moments_and_children(self):
        user = self._user()
        persona = self._persona(user)
        moment = store.add_moment(user["id"], persona["id"], "随人格删除")
        store.set_moment_like(moment["id"], user["id"], True)
        store.add_moment_comment(moment["id"], user["id"], "评论")

        store.delete_persona(user["id"], persona["id"])
        self.assertIsNone(store.get_moment(user["id"], moment["id"]))
        self.assertEqual(store.count_moment_likes(moment["id"]), 0)
        self.assertEqual(store.list_moment_comments(moment["id"]), [])

    def test_purge_user_content_removes_moments_and_children(self):
        user = self._user()
        persona = self._persona(user)
        moment = store.add_moment(user["id"], persona["id"], "随账号清空")
        store.set_moment_like(moment["id"], user["id"], True)
        store.add_moment_comment(moment["id"], user["id"], "评论")

        store.purge_user_content(user["id"])
        self.assertIsNone(store.get_moment(user["id"], moment["id"]))
        self.assertEqual(store.count_moment_likes(moment["id"]), 0)
        self.assertEqual(store.list_moment_comments(moment["id"]), [])

    def test_delete_all_moments_leaves_no_children(self):
        """属性：删除人格的全部动态后，不存在任何游离的点赞或评论。"""
        user = self._user()
        persona = self._persona(user)
        moment_ids = []
        for index in range(4):
            moment = store.add_moment(user["id"], persona["id"], f"动态{index}")
            moment_ids.append(moment["id"])
            store.set_moment_like(moment["id"], user["id"], index % 2 == 0)
            store.add_moment_comment(moment["id"], user["id"], f"评论{index}")

        for moment in store.list_moments(user["id"], persona["id"], limit=50):
            store.delete_moment(user["id"], moment["id"])

        self.assertEqual(store.list_moments(user["id"], persona["id"], limit=50), [])
        for moment_id in moment_ids:
            self.assertEqual(store.list_moment_comments(moment_id), [])
            self.assertEqual(store.count_moment_likes(moment_id), 0)


class _FakeMomentAgent:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    def compose_moment(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        return self.text


class ComposeMomentTest(unittest.TestCase):
    def _settings(self, **advanced):
        settings = persona_settings.load(None)
        settings["advanced"].update(advanced)
        return settings

    def test_selects_sticker_by_keyword_and_strips_marker(self):
        settings = self._settings(reply_image=True)
        agent = _FakeMomentAgent("今天去了海边 [[IMAGE:2]]")
        text, sticker_id = moments.compose(
            agent, settings, [{"content": "喜欢海边"}], [{"id": 7}, {"id": 9}]
        )
        self.assertEqual(text, "今天去了海边")
        self.assertEqual(sticker_id, 9)
        self.assertIn("喜欢海边", agent.prompts[0])

    def test_marker_stripped_but_no_sticker_when_disabled(self):
        settings = self._settings(reply_image=False)
        agent = _FakeMomentAgent("随手拍了一张 [[IMAGE]]")
        text, sticker_id = moments.compose(agent, settings, [], [{"id": 7}])
        self.assertEqual(text, "随手拍了一张")
        self.assertEqual(sticker_id, 0)

    def test_blank_generation_returns_empty(self):
        settings = self._settings()
        self.assertEqual(moments.compose(_FakeMomentAgent(""), settings, []), ("", 0))
        self.assertEqual(moments.compose(_FakeMomentAgent("   "), settings, []), ("", 0))
        self.assertEqual(
            moments.compose(_FakeMomentAgent("[[IMAGE]]"), settings, []), ("", 0)
        )

    def test_build_prompt_includes_custom_direction_and_memories(self):
        settings = persona_settings.load(None)
        settings["moments"]["prompt"] = "多写工作日常"
        prompt = moments.build_prompt(settings, [{"content": "换了一个新键盘"}])
        self.assertIn("多写工作日常", prompt)
        self.assertIn("换了一个新键盘", prompt)


class _FakeSchedAgent:
    def __init__(self, text="今天随手记一笔", ready=True, boom=False):
        self.text = text
        self.boom = boom
        self.config = SimpleNamespace(ready=ready)

    def compose_moment(self, prompt, *args, **kwargs):
        if self.boom:
            raise RuntimeError("model down")
        return self.text


class MomentSchedulerTest(unittest.TestCase):
    def setUp(self):
        self._prev_db = os.environ.get("PERSONA_DB_PATH")
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.environ["PERSONA_DB_PATH"] = path
        store.init_db()
        self.agent = _FakeSchedAgent()
        self.charged = []
        self.refunded = []
        self.notes = []
        self.scheduler = moments.MomentScheduler(
            lambda user_id, persona: self.agent,
            self._charge,
            self._refund,
            self._notify,
            check_seconds=999,
        )
        self.now = datetime(2026, 9, 17, 12, 0, 0)

    def tearDown(self):
        if self._prev_db is None:
            os.environ.pop("PERSONA_DB_PATH", None)
        else:
            os.environ["PERSONA_DB_PATH"] = self._prev_db

    def _charge(self, user_id, persona_id):
        if isinstance(getattr(self, "charge_error", None), Exception):
            raise self.charge_error
        self.charged.append((user_id, persona_id))
        return 3

    def _refund(self, user_id, amount, persona_id):
        self.refunded.append((user_id, amount, persona_id))

    def _notify(self, user_id, kind, message):
        self.notes.append((kind, message))

    def _ready_persona(self, enabled=True, **overrides):
        user = store.create_user(f"sched-{uuid.uuid4().hex[:8]}", "h", "s")
        settings = persona_settings.load(None)
        settings["moments"]["enabled"] = enabled
        settings["moments"].update(overrides)
        settings["proactive"]["window_start"] = "00:00"
        settings["proactive"]["window_end"] = "23:59"
        persona = store.create_persona(
            user["id"], "小念", f"/tmp/{uuid.uuid4().hex}",
            settings=persona_settings.dump(settings),
        )
        store.update_persona(user["id"], persona["id"], status="ready")
        return user, store.get_persona(user["id"], persona["id"])

    def test_publishes_and_charges_once(self):
        user, persona = self._ready_persona()
        self.scheduler.tick(self.now)

        listed = store.list_moments(user["id"], persona["id"])
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["content"], "今天随手记一笔")
        self.assertEqual(listed[0]["source"], "auto")
        self.assertEqual(self.charged, [(user["id"], persona["id"])])
        self.assertEqual(self.refunded, [])

    def test_disabled_persona_skips(self):
        user, persona = self._ready_persona(enabled=False)
        self.scheduler.tick(self.now)
        self.assertEqual(store.list_moments(user["id"], persona["id"]), [])
        self.assertEqual(self.charged, [])

    def test_out_of_window_skips(self):
        user, persona = self._ready_persona()
        settings = persona_settings.load(store.get_persona(user["id"], persona["id"])["settings"])
        settings["proactive"]["window_start"] = "09:00"
        settings["proactive"]["window_end"] = "10:00"
        store.update_persona(
            user["id"], persona["id"], settings=persona_settings.dump(settings)
        )
        self.scheduler.tick(datetime(2026, 9, 17, 12, 0, 0))
        self.assertEqual(store.list_moments(user["id"], persona["id"]), [])
        self.assertEqual(self.charged, [])

    def test_daily_cap_skips(self):
        user, persona = self._ready_persona(max_per_day=1)
        store.add_moment(user["id"], persona["id"], "已有动态")
        self.scheduler.tick(self.now)
        self.assertEqual(len(store.list_moments(user["id"], persona["id"])), 1)
        self.assertEqual(self.charged, [])

    def test_min_gap_skips(self):
        user, persona = self._ready_persona()
        store.add_moment(user["id"], persona["id"], "刚刚发过")
        self.scheduler.tick(self.now)
        self.assertEqual(len(store.list_moments(user["id"], persona["id"])), 1)
        self.assertEqual(self.charged, [])

    def test_empty_generation_refunds_and_does_not_persist(self):
        user, persona = self._ready_persona()
        self.agent = _FakeSchedAgent(text="   ")
        self.scheduler.tick(self.now)
        self.assertEqual(store.list_moments(user["id"], persona["id"]), [])
        self.assertEqual(self.refunded, [(user["id"], 3, persona["id"])])

    def test_generation_error_refunds(self):
        user, persona = self._ready_persona()
        self.agent = _FakeSchedAgent(boom=True)
        self.scheduler.tick(self.now)
        self.assertEqual(store.list_moments(user["id"], persona["id"]), [])
        self.assertEqual(self.refunded, [(user["id"], 3, persona["id"])])

    def test_unready_agent_skips_without_charging(self):
        user, persona = self._ready_persona()
        self.agent = _FakeSchedAgent(ready=False)
        self.scheduler.tick(self.now)
        self.assertEqual(store.list_moments(user["id"], persona["id"]), [])
        self.assertEqual(self.charged, [])
        self.assertEqual(self.refunded, [])

    def test_insufficient_credits_notifies_once_per_day(self):
        user, persona = self._ready_persona()
        self.charge_error = store.InsufficientCredits(0)
        self.scheduler.tick(self.now)
        self.scheduler._last_attempt.clear()
        self.scheduler.tick(self.now)

        self.assertEqual(store.list_moments(user["id"], persona["id"]), [])
        self.assertEqual(self.charged, [])
        self.assertEqual(len(self.notes), 1)
        self.assertIn("积分", self.notes[0][1])


if __name__ == "__main__":
    unittest.main()
