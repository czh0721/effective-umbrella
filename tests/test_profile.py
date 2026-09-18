import os
import tempfile
import unittest

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import accounts, store  # noqa: E402
from ex_persona.webapp import app  # noqa: E402

AVATAR = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class ProfileTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _client(self, username="profile-user"):
        salt, digest = accounts.hash_password("password123")
        store.create_user(username=username, password_hash=digest, password_salt=salt)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        login = client.post(
            "/api/auth/login", json={"username": username, "password": "password123"}
        )
        self.assertEqual(login.status_code, 200, login.text)
        return client

    def test_update_nickname_and_avatar(self):
        client = self._client()
        response = client.post("/api/profile", json={"nickname": "小念", "avatar": AVATAR})
        self.assertEqual(response.status_code, 200, response.text)
        user = response.json()["user"]
        self.assertEqual(user["nickname"], "小念")
        self.assertEqual(user["avatar"], AVATAR)

        me = client.get("/api/me").json()["user"]
        self.assertEqual(me["nickname"], "小念")
        self.assertEqual(me["avatar"], AVATAR)
        self.assertTrue(me.get("created_at"))

    def test_partial_update_keeps_avatar(self):
        client = self._client("profile-partial")
        client.post("/api/profile", json={"avatar": AVATAR})
        client.post("/api/profile", json={"nickname": "念念"})
        user = client.get("/api/me").json()["user"]
        self.assertEqual(user["nickname"], "念念")
        self.assertEqual(user["avatar"], AVATAR)

    def test_nickname_too_long_rejected(self):
        client = self._client("profile-long")
        response = client.post("/api/profile", json={"nickname": "念" * 21})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("昵称", response.json()["detail"])

    def test_avatar_must_be_image(self):
        client = self._client("profile-bad-avatar")
        response = client.post("/api/profile", json={"avatar": "https://example.com/a.png"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("头像", response.json()["detail"])

    def test_avatar_too_large_rejected(self):
        client = self._client("profile-big-avatar")
        huge = "data:image/png;base64," + "A" * 400_001
        response = client.post("/api/profile", json={"avatar": huge})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("过大", response.json()["detail"])

    def test_store_set_profile_noop_without_fields(self):
        user = store.create_user("profile-noop", "h", "s")
        store.set_user_profile(user["id"])
        self.assertIsNone(store.get_user(user["id"])["nickname"])


if __name__ == "__main__":
    unittest.main()
