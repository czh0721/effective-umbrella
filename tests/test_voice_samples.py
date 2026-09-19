import os
import tempfile
import unittest
import uuid

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import store, workspace  # noqa: E402
from ex_persona.webapp import app  # noqa: E402

MP3_BYTES = b"ID3\x03\x00\x00\x00" + b"\x00" * 128
WAV_BYTES = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 64


class VoiceSampleApiTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _prepare(self, client):
        token = f"tok-{uuid.uuid4().hex[:8]}"
        user = self._register(client, f"voice-{uuid.uuid4().hex[:8]}")
        persona = client.post("/api/personas", json={"name": "小念"}).json()["persona"]
        store.upsert_wechat_binding(
            user["id"],
            bridge_token=token,
            home_dir=str(workspace.home_dir(user["id"])),
            persona_id=persona["id"],
        )
        return user, persona, token

    def _post(self, client, token, data=MP3_BYTES, message_id="1001", duration="3200", name="v.mp3"):
        return client.post(
            f"/api/wechat/voice/{token}",
            files={"file": (name, data, "audio/mpeg")},
            headers={
                "X-WeChat-From": "contact-1",
                "X-WeChat-Message-ID": message_id,
                "X-Voice-Duration-Ms": duration,
            },
        )

    def test_saves_sample(self):
        with TestClient(app) as client:
            user, persona, token = self._prepare(client)
            response = self._post(client, token)
            self.assertEqual(response.status_code, 200, response.text)
            samples = store.list_voice_samples(user["id"], persona["id"], "contact-1")
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]["duration_ms"], 3200)
            self.assertEqual(samples[0]["source_message_id"], "1001")
            self.assertTrue(os.path.exists(samples[0]["path"]))

    def test_duplicate_message_is_idempotent(self):
        with TestClient(app) as client:
            user, persona, token = self._prepare(client)
            first = self._post(client, token)
            second = self._post(client, token)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertTrue(second.json().get("duplicate"))
            self.assertEqual(len(store.list_voice_samples(user["id"], persona["id"], "contact-1")), 1)

    def test_rejects_oversized_file(self):
        with TestClient(app) as client:
            _, _, token = self._prepare(client)
            big = b"ID3" + b"\x00" * (5 * 1024 * 1024 + 10)
            response = self._post(client, token, data=big)
            self.assertEqual(response.status_code, 413, response.text)

    def test_rejects_non_audio(self):
        with TestClient(app) as client:
            _, _, token = self._prepare(client)
            response = self._post(client, token, data=b"<html>not audio</html>")
            self.assertEqual(response.status_code, 400, response.text)

    def test_invalid_token_returns_404(self):
        with TestClient(app) as client:
            self._register(client, f"voice-bad-{uuid.uuid4().hex[:6]}")
            response = self._post(client, "missing-token")
            self.assertEqual(response.status_code, 404, response.text)

    def test_keeps_latest_twenty(self):
        with TestClient(app) as client:
            user, persona, token = self._prepare(client)
            for index in range(25):
                response = self._post(client, token, message_id=f"m{index}", duration="1000")
                self.assertEqual(response.status_code, 200, response.text)
            samples = store.list_voice_samples(user["id"], persona["id"], "contact-1")
            self.assertEqual(len(samples), 20)
            self.assertEqual(samples[0]["source_message_id"], "m24")

    def test_wav_container_accepted(self):
        with TestClient(app) as client:
            _, _, token = self._prepare(client)
            response = self._post(client, token, data=WAV_BYTES, name="v.wav")
            self.assertEqual(response.status_code, 200, response.text)


class VoiceConfigTest(unittest.TestCase):
    def test_platform_config_voice_fields(self):
        store.init_db()
        row = store.set_platform_config(
            "", "https://api.deepseek.com/v1", "deepseek-chat", False, 20, 200,
            voice_tts_model="speech-02-hd", voice_clone_cost=500, voice_reply_cost=20,
            voice_enabled=True, minimax_api_key_encrypted="enc",
        )
        self.assertEqual(row["voice_tts_model"], "speech-02-hd")
        self.assertEqual(row["voice_clone_cost"], 500)
        self.assertEqual(row["voice_reply_cost"], 20)
        self.assertEqual(row["voice_enabled"], 1)
        self.assertEqual(row["minimax_api_key_encrypted"], "enc")

    def test_persona_settings_voice_defaults(self):
        from ex_persona import persona_settings

        settings = persona_settings.load(None)
        voice = settings["voice"]
        self.assertEqual(voice["preset"], "female-1")
        self.assertEqual(voice["clone_status"], "none")
        merged = persona_settings.validate({"voice": {"preset": "bogus", "clone_status": "weird"}})
        self.assertEqual(merged["voice"]["preset"], "female-1")
        self.assertEqual(merged["voice"]["clone_status"], "none")


if __name__ == "__main__":
    unittest.main()
