import os
import tempfile
import unittest
import uuid
from unittest import mock

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import config, crypto, store, workspace  # noqa: E402
from ex_persona import webapp as webapp_module  # noqa: E402
from ex_persona.webapp import app  # noqa: E402


def _configure_voice(reply_cost=20, enabled=True):
    store.set_platform_config(
        "", "https://api.deepseek.com/v1", "deepseek-chat", True, 0, 0,
        minimax_api_key_encrypted=crypto.encrypt("mm-key") if enabled else "",
        voice_tts_model="speech-02-turbo",
        voice_clone_cost=500,
        voice_reply_cost=reply_cost,
        voice_enabled=enabled,
    )


class _FakeThread:
    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


class VoiceReplyTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _prepare(self, client, *, balance=500, reply_cost=20, enabled=True):
        user = client.post(
            "/api/auth/register",
            json={"username": f"vr-{uuid.uuid4().hex[:8]}", "password": "Password123!"},
        ).json()["user"]
        persona = client.post("/api/personas", json={"name": "小念"}).json()["persona"]
        if balance:
            store.grant_credits(user["id"], balance, reason="test")
        _configure_voice(reply_cost=reply_cost, enabled=enabled)
        return user, persona

    def test_start_skips_when_disabled(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client, enabled=False)
            fresh = store.get_persona(user["id"], persona["id"])
            self.assertFalse(webapp_module._start_voice_reply(user["id"], fresh, "c1", "你好"))

    def test_start_skips_empty_reply(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client)
            fresh = store.get_persona(user["id"], persona["id"])
            self.assertFalse(webapp_module._start_voice_reply(user["id"], fresh, "c1", "   "))

    def test_start_spawns_task_when_ready(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client)
            fresh = store.get_persona(user["id"], persona["id"])
            with mock.patch.object(webapp_module.threading, "Thread", _FakeThread):
                result = webapp_module._start_voice_reply(user["id"], fresh, "c1", "想你了")
            self.assertTrue(result)

    def test_task_success_charges_and_enqueues(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client, balance=500, reply_cost=20)
            start = store.get_credits(user["id"])
            platform = config.load_platform_config()
            with mock.patch.object(
                webapp_module.voice, "minimax_tts", return_value=b"ID3" + b"\x00" * 16
            ), mock.patch.object(
                webapp_module._outbox, "enqueue", return_value=True
            ) as queued, mock.patch.object(webapp_module, "_record_send"):
                webapp_module._voice_reply_task(
                    user["id"], persona["id"], "c1", "想你了", platform
                )
            self.assertEqual(store.get_credits(user["id"]), start - 20)
            self.assertTrue(queued.called)
            self.assertTrue(queued.call_args.kwargs.get("media"))

    def test_task_synth_failure_does_not_charge(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client, balance=500, reply_cost=20)
            start = store.get_credits(user["id"])
            platform = config.load_platform_config()
            with mock.patch.object(
                webapp_module.voice, "minimax_tts",
                side_effect=webapp_module.voice.VoiceError("bad"),
            ), mock.patch.object(webapp_module._outbox, "enqueue") as queued:
                webapp_module._voice_reply_task(
                    user["id"], persona["id"], "c1", "想你了", platform
                )
            self.assertEqual(store.get_credits(user["id"]), start)
            self.assertFalse(queued.called)

    def test_task_insufficient_credits_skips(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client, balance=10, reply_cost=20)
            platform = config.load_platform_config()
            with mock.patch.object(
                webapp_module.voice, "minimax_tts", return_value=b"ID3"
            ), mock.patch.object(webapp_module._outbox, "enqueue") as queued:
                webapp_module._voice_reply_task(
                    user["id"], persona["id"], "c1", "想你了", platform
                )
            self.assertFalse(queued.called)

    def test_task_enqueue_failure_refunds(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client, balance=500, reply_cost=20)
            start = store.get_credits(user["id"])
            platform = config.load_platform_config()
            with mock.patch.object(
                webapp_module.voice, "minimax_tts", return_value=b"ID3"
            ), mock.patch.object(
                webapp_module._outbox, "enqueue", return_value=False
            ):
                webapp_module._voice_reply_task(
                    user["id"], persona["id"], "c1", "想你了", platform
                )
            self.assertEqual(store.get_credits(user["id"]), start)

    def test_route_chat_triggers_voice_reply(self):
        token = f"tok-{uuid.uuid4().hex[:8]}"
        with TestClient(app) as client:
            user, persona = self._prepare(client, balance=500, reply_cost=20)
            store.upsert_wechat_binding(
                user["id"], bridge_token=token,
                home_dir=str(workspace.home_dir(user["id"])), persona_id=persona["id"],
            )
            saved = client.put(
                f"/api/personas/{persona['id']}/settings",
                json={"settings": {"advanced": {"reply_voice": True}}},
            )
            self.assertEqual(saved.status_code, 200, saved.text)

            class _Config:
                ready = True
                platform = True
                model = "deepseek-chat"
                base_url = ""

            class _Agent:
                config = _Config()
                last_error = None
                last_fallback = False

                def reply(self, *args, **kwargs):
                    return "我也想你，早点休息。"

            original = webapp_module.get_agent
            webapp_module.get_agent = lambda uid, p: _Agent()
            with mock.patch.object(webapp_module, "_start_voice_reply") as started:
                try:
                    response = client.post(
                        f"/v1/chat/completions/{token}",
                        json={"user": "c1", "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = original
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(started.called)
            self.assertEqual(started.call_args.args[3], "我也想你，早点休息。")


if __name__ == "__main__":
    unittest.main()
