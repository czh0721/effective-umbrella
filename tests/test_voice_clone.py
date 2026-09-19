import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import accounts, config, crypto, persona_settings, store  # noqa: E402
from ex_persona import webapp as webapp_module  # noqa: E402
from ex_persona.webapp import app  # noqa: E402


def _configure_voice(clone_cost=500, reply_cost=20, enabled=True):
    store.set_platform_config(
        "", "https://api.deepseek.com/v1", "deepseek-chat", True, 0, 0,
        minimax_api_key_encrypted=crypto.encrypt("mm-key") if enabled else "",
        voice_tts_model="speech-02-turbo",
        voice_clone_cost=clone_cost,
        voice_reply_cost=reply_cost,
        voice_enabled=enabled,
    )


class VoiceCloneTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _prepare(self, client, *, balance=2000, seconds=12.0, enabled=True):
        user = client.post(
            "/api/auth/register",
            json={"username": f"vc-{uuid.uuid4().hex[:8]}", "password": "Password123!"},
        ).json()["user"]
        persona = client.post("/api/personas", json={"name": "小念"}).json()["persona"]
        persona = store.get_persona(user["id"], persona["id"])
        store.grant_credits(user["id"], balance, reason="test")
        directory = Path(persona["dir"]) / "voices" / "c1"
        directory.mkdir(parents=True, exist_ok=True)
        sample = directory / "msg-1.mp3"
        sample.write_bytes(b"ID3" + b"\x00" * 32)
        parts = 2
        each = int(seconds * 1000 / parts)
        for index in range(parts):
            store.add_voice_sample(
                user["id"], persona["id"], "c1", str(sample), sample.stat().st_size, each, f"m{index}"
            )
        _configure_voice(enabled=enabled)
        return user, persona

    def test_clone_requires_consent(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client)
            response = client.post(
                f"/api/personas/{persona['id']}/voice/clone",
                json={"contact": "c1", "consent": False},
            )
            self.assertEqual(response.status_code, 400, response.text)

    def test_clone_requires_samples(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client)
            response = client.post(
                f"/api/personas/{persona['id']}/voice/clone",
                json={"contact": "nobody", "consent": True},
            )
            self.assertEqual(response.status_code, 400, response.text)

    def test_clone_rejects_short_samples(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client, seconds=4.0)
            response = client.post(
                f"/api/personas/{persona['id']}/voice/clone",
                json={"contact": "c1", "consent": True},
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("样本不足", response.json()["detail"])

    def test_clone_deducts_and_marks_pending(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client, balance=2000)
            start = store.get_credits(user["id"])
            with mock.patch.object(webapp_module, "_start_voice_clone") as started:
                response = client.post(
                    f"/api/personas/{persona['id']}/voice/clone",
                    json={"contact": "c1", "consent": True},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["clone_status"], "pending")
            self.assertEqual(store.get_credits(user["id"]), start - 500)
            clone = store.get_voice_clone(user["id"], persona["id"], "c1")
            self.assertEqual(clone["status"], "pending")
            self.assertTrue(started.called)

    def test_clone_insufficient_credits(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client, balance=100)
            response = client.post(
                f"/api/personas/{persona['id']}/voice/clone",
                json={"contact": "c1", "consent": True},
            )
            self.assertEqual(response.status_code, 402, response.text)

    def test_clone_rejects_running(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client)
            store.upsert_voice_clone(
                user["id"], persona["id"], "c1", status="pending", voice_id="v-running"
            )
            response = client.post(
                f"/api/personas/{persona['id']}/voice/clone",
                json={"contact": "c1", "consent": True},
            )
            self.assertEqual(response.status_code, 409, response.text)

    def test_clone_task_success_marks_ready(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client)
            store.upsert_voice_clone(
                user["id"], persona["id"], "c1", status="pending", voice_id="v-1"
            )
            platform = config.load_platform_config()
            with mock.patch.object(
                webapp_module.voice, "minimax_clone", return_value={"voice_id": "v-1"}
            ):
                webapp_module._voice_clone_task(
                    user["id"], persona["id"], "c1", ["/tmp/none.mp3"], "v-1", 500, platform
                )
            clone = store.get_voice_clone(user["id"], persona["id"], "c1")
            self.assertEqual(clone["status"], "ready")
            fresh = store.get_persona(user["id"], persona["id"])
            settings = persona_settings.load(fresh.get("settings"))
            self.assertEqual(settings["voice"]["clone_status"], "ready")
            self.assertEqual(settings["voice"]["clone_voice_id"], "v-1")

    def test_clone_task_failure_refunds(self):
        with TestClient(app) as client:
            user, persona = self._prepare(client, balance=2000)
            start = store.get_credits(user["id"])
            store.deduct_credits(user["id"], 500, reason="音色克隆扣费")
            store.upsert_voice_clone(
                user["id"], persona["id"], "c1", status="pending", voice_id="v-2"
            )
            platform = config.load_platform_config()
            with mock.patch.object(
                webapp_module.voice, "minimax_clone",
                side_effect=webapp_module.voice.VoiceError("boom"),
            ):
                webapp_module._voice_clone_task(
                    user["id"], persona["id"], "c1", ["/tmp/none.mp3"], "v-2", 500, platform
                )
            self.assertEqual(store.get_credits(user["id"]), start)
            clone = store.get_voice_clone(user["id"], persona["id"], "c1")
            self.assertEqual(clone["status"], "failed")


class VoiceAdminConfigTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def test_voice_config_roundtrip_masks_key(self):
        username = f"vadmin-{uuid.uuid4().hex[:8]}"
        accounts.create_admin(username, "Password123!")
        with TestClient(app) as client:
            login = client.post(
                "/api/admin/auth/login", json={"username": username, "password": "Password123!"}
            )
            self.assertEqual(login.status_code, 200, login.text)
            response = client.put(
                "/api/admin/platform",
                json={
                    "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                    "enabled": True, "minimax_api_key": "mm-secret-key",
                    "voice_tts_model": "speech-02-hd", "voice_clone_cost": 600,
                    "voice_reply_cost": 30, "voice_enabled": True,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("mm-secret-key", response.text)
            data = response.json()
            self.assertTrue(data["has_voice_key"])
            self.assertEqual(data["voice_tts_model"], "speech-02-hd")
            self.assertEqual(data["voice_clone_cost"], 600)
            self.assertEqual(data["voice_reply_cost"], 30)
            self.assertTrue(data["voice_enabled"])
            self.assertTrue(data["voice_ready"])


if __name__ == "__main__":
    unittest.main()
