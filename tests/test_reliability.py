import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from unittest import mock

if "PERSONA_DATA_DIR" not in os.environ:
    _TMP = tempfile.TemporaryDirectory()
    os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ.setdefault("PERSONA_SECRET_KEY", "unit-test-secret")
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import distill, media_reply, store, workspace  # noqa: E402
from ex_persona.llm import LLMError  # noqa: E402
from ex_persona.outbox import OutboxWorker  # noqa: E402
from ex_persona.webapp import app  # noqa: E402


def _unique_user_id() -> int:
    return uuid.uuid4().int % 1_000_000_000 + 1_000_000_000


class MediaReplyTest(unittest.TestCase):
    def test_parse_strips_allowed_markers(self):
        text, directives = media_reply.parse_reply("哈哈\n[[STICKER:2]]", {"STICKER"})
        self.assertEqual(text, "哈哈")
        self.assertEqual(directives, [{"kind": "STICKER", "keyword": "2"}])

    def test_parse_keeps_disabled_marker(self):
        text, directives = media_reply.parse_reply("看图[[IMAGE]]", {"STICKER"})
        self.assertEqual(text, "看图[[IMAGE]]")
        self.assertEqual(directives, [])

    def test_parse_bare_marker(self):
        text, directives = media_reply.parse_reply("好呀[[STICKER]]", {"STICKER"})
        self.assertEqual(text, "好呀")
        self.assertEqual(directives, [{"kind": "STICKER", "keyword": ""}])

    def test_enabled_kinds(self):
        settings = {"advanced": {"reply_sticker": True, "reply_image": False}}
        self.assertEqual(media_reply.enabled_kinds(settings), {"STICKER"})

    def test_choose_media_by_index_and_keyword(self):
        stickers = [{"name": "a.png", "path": "/a"}, {"name": "猫猫.png", "path": "/b"}]
        self.assertEqual(media_reply.choose_media(stickers, {"keyword": "2"})["path"], "/b")
        self.assertEqual(media_reply.choose_media(stickers, {"keyword": "猫"})["path"], "/b")

    def test_choose_media_falls_back_to_any(self):
        stickers = [{"name": "a.png", "path": "/a"}]
        self.assertEqual(media_reply.choose_media(stickers, {"keyword": "99"})["path"], "/a")

    def test_build_hint_lists_numbers(self):
        settings = {"advanced": {"reply_sticker": True, "reply_image": False}}
        hint = media_reply.build_hint([{"name": "x"}, {"name": "y"}], settings)
        self.assertIn("[[STICKER]]", hint)
        self.assertIn("1、2", hint)

    def test_build_hint_empty_when_disabled(self):
        self.assertEqual(media_reply.build_hint([{"name": "x"}], {"advanced": {}}), "")


class OutboxTest(unittest.TestCase):
    def test_marks_sent(self):
        uid = _unique_user_id()
        worker = OutboxWorker(lambda *a: True, min_interval=0, per_minute=100)
        self.assertTrue(worker.enqueue(uid, "contact", text="你好"))
        self.assertEqual(worker.drain_once(), 1)
        pending = [r for r in store.list_due_outbox(store.utcnow(), 50) if r["user_id"] == uid]
        self.assertEqual(pending, [])

    def test_retries_then_gives_up(self):
        uid = _unique_user_id()
        worker = OutboxWorker(
            lambda *a: False, min_interval=0, per_minute=100, max_attempts=2, base_backoff=0
        )
        worker.enqueue(uid, "contact", text="失败")
        worker.drain_once()
        worker.drain_once()
        stats = store.outbox_stats()
        self.assertGreaterEqual(stats["counts"].get("failed", 0), 1)
        failed = [row for row in stats["recent_failures"] if row["user_id"] == uid]
        self.assertEqual(len(failed), 1)
        self.assertGreaterEqual(failed[0]["attempts"], 1)

    def test_per_minute_limit(self):
        uid = _unique_user_id()
        worker = OutboxWorker(lambda *a: True, min_interval=0, per_minute=1)
        worker.enqueue(uid, "contact", text="一")
        worker.enqueue(uid, "contact", text="二")
        self.assertEqual(worker.drain_once(), 1)
        pending = [r for r in store.list_due_outbox(store.utcnow(), 50) if r["user_id"] == uid]
        self.assertEqual(len(pending), 1)

    def test_min_interval_blocks_second(self):
        uid = _unique_user_id()
        worker = OutboxWorker(lambda *a: True, min_interval=3600, per_minute=100)
        worker.enqueue(uid, "contact", text="一")
        worker.enqueue(uid, "contact", text="二")
        self.assertEqual(worker.drain_once(), 1)

    def test_enqueue_ignores_empty(self):
        worker = OutboxWorker(lambda *a: True)
        self.assertEqual(worker.enqueue(123456, "contact", text="", media=""), 0)
        self.assertEqual(worker.enqueue(123456, "", text="hi"), 0)


class TaskStoreTest(unittest.TestCase):
    def test_task_persisted_with_result(self):
        task_id = f"task-{uuid.uuid4().hex}"
        store.create_task(task_id, 999999, None, "distill")
        store.update_task(
            task_id, progress=50, message="处理中", status="running", result={"a": 1}
        )
        task = store.get_task(task_id)
        self.assertEqual(task["progress"], 50)
        self.assertEqual(task["message"], "处理中")
        self.assertEqual(task["result"], {"a": 1})

    def test_init_db_migrates_legacy_tasks_table(self):
        path = os.path.join(tempfile.mkdtemp(), "legacy.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, persona_id INTEGER,"
            "kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',"
            "progress INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',"
            "error TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',"
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        )
        conn.commit()
        conn.close()
        with mock.patch.dict(os.environ, {"PERSONA_DB_PATH": path}):
            store.init_db()
            conn = sqlite3.connect(path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(tasks)")}
            conn.close()
        self.assertIn("client_id", columns)
        self.assertIn("idx_tasks_client", indexes)

    def test_set_binding_token_rotates(self):
        uid = _unique_user_id()
        store.upsert_wechat_binding(uid, bridge_token=f"old-{uid}", home_dir="/tmp")
        store.set_binding_token(uid, f"new-{uid}")
        self.assertEqual(store.get_wechat_binding(uid)["bridge_token"], f"new-{uid}")
        self.assertIsNone(store.get_binding_by_token(f"old-{uid}"))


class MediaRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_route_chat_strips_sticker_and_queues_media(self):
        from ex_persona import webapp as webapp_module

        with TestClient(app) as client:
            user = self._register(client, f"media-{uuid.uuid4().hex[:8]}")
            persona = client.post("/api/personas", json={"name": "小念"}).json()["persona"]
            uploaded = client.post(
                f"/api/personas/{persona['id']}/stickers",
                files={"file": ("cute.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            store.update_persona(
                user["id"], persona["id"],
                settings=json.dumps({"advanced": {"reply_sticker": True}}),
            )
            store.upsert_wechat_binding(
                user["id"],
                bridge_token=f"tok-{uuid.uuid4().hex[:8]}",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            token = store.get_wechat_binding(user["id"])["bridge_token"]

            class _FakeAgent:
                class config:
                    ready = True

                def reply(self, *args, **kwargs):
                    return "在呀\n[[STICKER:1]]"

            captured: dict = {}

            def _fake_queue(uid, persona, contact, stickers, directives):
                captured["contact"] = contact
                captured["stickers"] = stickers
                captured["directives"] = directives
                return len(directives)

            original_agent = webapp_module.get_agent
            original_queue = webapp_module._queue_media_replies
            original_extract = webapp_module._start_extract
            webapp_module.get_agent = lambda uid, p: _FakeAgent()
            webapp_module._queue_media_replies = _fake_queue
            webapp_module._start_extract = lambda *a, **k: None
            try:
                response = client.post(
                    f"/v1/chat/completions/{token}",
                    json={
                        "messages": [{"role": "user", "content": "在吗"}],
                        "user": "contact-1",
                    },
                )
            finally:
                webapp_module.get_agent = original_agent
                webapp_module._queue_media_replies = original_queue
                webapp_module._start_extract = original_extract
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "在呀")
            self.assertEqual(captured["contact"], "contact-1")
            self.assertEqual(captured["directives"], [{"kind": "STICKER", "keyword": "1"}])
            self.assertEqual(len(captured["stickers"]), 1)

    def _prepare(self, client, settings=None):
        user = self._register(client, f"media-{uuid.uuid4().hex[:8]}")
        persona = client.post("/api/personas", json={"name": "小念"}).json()["persona"]
        if settings is not None:
            store.update_persona(user["id"], persona["id"], settings=json.dumps(settings))
        store.upsert_wechat_binding(
            user["id"],
            bridge_token=f"tok-{uuid.uuid4().hex[:8]}",
            home_dir=str(workspace.home_dir(user["id"])),
            persona_id=persona["id"],
        )
        return store.get_wechat_binding(user["id"])["bridge_token"]

    def test_image_placeholder_silent_by_default(self):
        from ex_persona import webapp as webapp_module

        with TestClient(app) as client:
            token = self._prepare(client)

            def _boom(*args, **kwargs):
                raise AssertionError("未开启回应图片时不应调用大模型")

            original = webapp_module.get_agent
            webapp_module.get_agent = _boom
            try:
                response = client.post(
                    f"/v1/chat/completions/{token}",
                    json={"messages": [{"role": "user", "content": "[图片]"}], "user": "contact-2"},
                )
            finally:
                webapp_module.get_agent = original
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "")

    def test_image_placeholder_replies_when_enabled(self):
        from ex_persona import webapp as webapp_module

        with TestClient(app) as client:
            token = self._prepare(client, {"advanced": {"reply_to_images": True}})

            class _FakeAgent:
                class config:
                    ready = True

                def reply(self, *args, **kwargs):
                    return "看到啦"

            original_agent = webapp_module.get_agent
            original_extract = webapp_module._start_extract
            webapp_module.get_agent = lambda uid, p: _FakeAgent()
            webapp_module._start_extract = lambda *a, **k: None
            try:
                response = client.post(
                    f"/v1/chat/completions/{token}",
                    json={"messages": [{"role": "user", "content": "[图片]"}], "user": "contact-3"},
                )
            finally:
                webapp_module.get_agent = original_agent
                webapp_module._start_extract = original_extract
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "看到啦")


class HardeningTest(unittest.TestCase):
    """安全与隐私加固：登录限流、会话回收、账号彻底清理、上传校验、响应头。"""

    @classmethod
    def setUpClass(cls):
        store.init_db()

    def setUp(self):
        from ex_persona import webapp as webapp_module

        self.webapp = webapp_module
        with webapp_module._auth_attempts_lock:
            webapp_module._auth_attempts.clear()

    def tearDown(self):
        with self.webapp._auth_attempts_lock:
            self.webapp._auth_attempts.clear()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_login_throttle_blocks_after_repeated_failures(self):
        with TestClient(app) as client:
            self._register(client, f"thr-{uuid.uuid4().hex[:8]}")
            for _ in range(self.webapp.AUTH_MAX_ATTEMPTS):
                client.post(
                    "/api/auth/login",
                    json={"username": "ghost-throttle", "password": "wrongpass123"},
                )
            blocked = client.post(
                "/api/auth/login",
                json={"username": "ghost-throttle", "password": "wrongpass123"},
            )
            self.assertEqual(blocked.status_code, 429, blocked.text)

    def test_change_password_revokes_other_sessions(self):
        with TestClient(app) as client:
            name = f"pw-{uuid.uuid4().hex[:8]}"
            self._register(client, name)
            other = TestClient(app)
            self.assertEqual(
                other.post(
                    "/api/auth/login", json={"username": name, "password": "Password123!"}
                ).status_code,
                200,
            )
            self.assertEqual(other.get("/api/me").status_code, 200)
            changed = client.post(
                "/api/account/password",
                json={"current": "Password123!", "new": "newPassword456!"},
            )
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(other.get("/api/me").status_code, 401)
            self.assertEqual(client.get("/api/me").status_code, 200)

    def test_delete_account_purges_data(self):
        with TestClient(app) as client:
            user = self._register(client, f"del-{uuid.uuid4().hex[:8]}")
            persona = client.post("/api/personas", json={"name": "小删"}).json()["persona"]
            store.add_turn(user["id"], persona["id"], "user", "你好", contact="c1")
            store.add_memory(user["id"], persona["id"], "c1", "fact", "喜欢猫")
            balance_before = store.get_credits(user["id"])
            response = client.post("/api/account/delete", json={"current": "Password123!", "new": ""})
            self.assertEqual(response.status_code, 200, response.text)
            # 账号软删：保留账号行与账本用于对账，个人内容全部清除。
            self.assertEqual(store.get_user(user["id"])["status"], "deleted")
            self.assertEqual(store.count_turns(user["id"], persona["id"]), 0)
            self.assertEqual(store.list_memories(user["id"], persona["id"], "c1"), [])
            self.assertEqual(store.list_personas(user["id"]), [])
            self.assertEqual(store.get_credits(user["id"]), balance_before)

    def test_sticker_upload_rejects_non_image(self):
        with TestClient(app) as client:
            self._register(client, f"img-{uuid.uuid4().hex[:8]}")
            persona = client.post("/api/personas", json={"name": "小图"}).json()["persona"]
            response = client.post(
                f"/api/personas/{persona['id']}/stickers",
                files={
                    "file": (
                        "evil.html",
                        b"<html><script>alert(1)</script></html>",
                        "text/html",
                    )
                },
            )
            self.assertEqual(response.status_code, 400, response.text)

    def test_security_headers_and_manifest(self):
        with TestClient(app) as client:
            response = client.get("/login")
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
            self.assertIn("strict-origin", response.headers.get("Referrer-Policy", ""))
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
            manifest = client.get("/manifest.webmanifest")
            self.assertEqual(manifest.status_code, 200)
            self.assertIn("manifest", manifest.headers.get("content-type", ""))



class DistillJsonTest(unittest.TestCase):
    def test_parses_valid_json(self):
        with mock.patch(
            "ex_persona.distill.chat_with_meta", return_value=('{"summary": "ok"}', "stop")
        ) as patched:
            result = distill._distill_json(object(), [])
        self.assertEqual(result, {"summary": "ok"})
        self.assertEqual(patched.call_count, 1)

    def test_retries_with_more_budget_when_truncated(self):
        replies = [
            ('{"a": 1, "b":', "length"),
            ('{"a": 1, "b": 2}', "stop"),
        ]
        with mock.patch("ex_persona.distill.chat_with_meta", side_effect=replies) as patched:
            result = distill._distill_json(object(), [])
        self.assertEqual(result, {"a": 1, "b": 2})
        self.assertEqual(patched.call_count, 2)
        self.assertEqual(patched.call_args_list[1].kwargs["max_tokens"], 8000)

    def test_repairs_invalid_json(self):
        replies = [
            ('{"a": 1 "b": 2}', "stop"),
            ('{"a": 1, "b": 2}', "stop"),
        ]
        with mock.patch("ex_persona.distill.chat_with_meta", side_effect=replies):
            result = distill._distill_json(object(), [])
        self.assertEqual(result, {"a": 1, "b": 2})

    def test_raises_friendly_error_when_repair_fails(self):
        with mock.patch(
            "ex_persona.distill.chat_with_meta", return_value=("not json", "stop")
        ):
            with self.assertRaises(LLMError) as error:
                distill._distill_json(object(), [])
        self.assertIn("JSON", str(error.exception))


class TaskRecoveryTest(unittest.TestCase):
    def test_fail_stale_tasks_marks_running_as_error(self):
        task = store.create_task(
            f"stale-{uuid.uuid4().hex[:8]}", _unique_user_id(), None, "distill"
        )
        store.update_task(task["id"], status="running", progress=40)
        self.assertEqual(store.fail_stale_tasks("服务重启"), 1)
        recovered = store.get_task(task["id"])
        self.assertEqual(recovered["status"], "error")
        self.assertIn("服务重启", recovered["error"])


class PersonaLifecycleTest(unittest.TestCase):
    def test_update_persona_can_toggle_active(self):
        user_id = _unique_user_id()
        persona = store.create_persona(user_id, "甲", directory="")
        self.assertEqual(persona["is_active"], 1)
        store.update_persona(user_id, persona["id"], is_active=0)
        self.assertEqual(store.get_persona(user_id, persona["id"])["is_active"], 0)

    def test_delete_active_persona_promotes_another(self):
        user_id = _unique_user_id()
        first = store.create_persona(user_id, "甲", directory="")
        second = store.create_persona(user_id, "乙", directory="")
        self.assertEqual(first["is_active"], 1)
        self.assertEqual(second["is_active"], 0)
        store.delete_persona(user_id, first["id"])
        promoted = store.get_persona(user_id, second["id"])
        self.assertEqual(promoted["is_active"], 1)
        self.assertEqual(store.get_active_persona(user_id)["id"], second["id"])

    def test_farewell_retires_and_activate_restores(self):
        with TestClient(app) as client:
            username = f"lifecycle-{uuid.uuid4().hex[:8]}"
            client.post(
                "/api/auth/register", json={"username": username, "password": "Password123!"}
            )
            me = client.get("/api/me").json()["user"]
            persona = client.post("/api/personas", json={"name": "告别"}).json()["persona"]
            store.update_persona(me["id"], persona["id"], status="retired", is_active=0)
            retired = store.get_persona(me["id"], persona["id"])
            self.assertEqual(retired["status"], "retired")
            self.assertEqual(retired["is_active"], 0)
            activated = client.post(f"/api/personas/{persona['id']}/activate")
            self.assertEqual(activated.status_code, 200, activated.text)
            restored = store.get_persona(me["id"], persona["id"])
            self.assertEqual(restored["status"], "ready")
            self.assertEqual(restored["is_active"], 1)


if __name__ == "__main__":
    unittest.main()
