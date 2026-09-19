import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import store  # noqa: E402
from ex_persona.webapp import app  # noqa: E402

VALID_AVATAR = "data:image/png;base64," + "A" * 64


class PersonaAvatarApiTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _persona(self, client, name="头像分身"):
        response = client.post("/api/personas", json={"name": name})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["persona"]

    def test_update_avatar(self):
        with TestClient(app) as client:
            user = self._register(client, "avatar-set")
            persona = self._persona(client)
            response = client.patch(
                f"/api/personas/{persona['id']}", json={"avatar": VALID_AVATAR}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["persona"]["avatar"], VALID_AVATAR)
            self.assertEqual(
                store.get_persona(user["id"], persona["id"])["avatar"], VALID_AVATAR
            )

    def test_rejects_non_image_avatar(self):
        with TestClient(app) as client:
            self._register(client, "avatar-bad-prefix")
            persona = self._persona(client)
            response = client.patch(
                f"/api/personas/{persona['id']}", json={"avatar": "https://x/y.png"}
            )
            self.assertEqual(response.status_code, 400, response.text)

    def test_rejects_oversized_avatar(self):
        with TestClient(app) as client:
            self._register(client, "avatar-too-big")
            persona = self._persona(client)
            payload = "data:image/png;base64," + "A" * 400_000
            response = client.patch(f"/api/personas/{persona['id']}", json={"avatar": payload})
            self.assertEqual(response.status_code, 400, response.text)

    def test_clear_avatar(self):
        with TestClient(app) as client:
            user = self._register(client, "avatar-clear")
            persona = self._persona(client)
            client.patch(f"/api/personas/{persona['id']}", json={"avatar": VALID_AVATAR})
            response = client.patch(f"/api/personas/{persona['id']}", json={"avatar": ""})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["persona"]["avatar"], "")
            self.assertEqual(store.get_persona(user["id"], persona["id"])["avatar"], "")

    def test_cannot_update_other_users_persona(self):
        with TestClient(app) as client:
            owner = self._register(client, "avatar-owner")
            with TestClient(app) as other:
                self._register(other, "avatar-other")
                persona = self._persona(client)
                response = other.patch(
                    f"/api/personas/{persona['id']}", json={"avatar": VALID_AVATAR}
                )
                self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(store.get_persona(owner["id"], persona["id"])["avatar"], "")

    def test_unauthenticated_returns_401(self):
        with TestClient(app) as client:
            self._register(client, "avatar-auth")
            persona = self._persona(client)
        with TestClient(app) as anonymous:
            response = anonymous.patch(
                f"/api/personas/{persona['id']}", json={"avatar": VALID_AVATAR}
            )
            self.assertEqual(response.status_code, 401, response.text)


class PersonaAvatarFrontendTest(unittest.TestCase):
    def test_upload_ui_and_avatar_slots(self):
        web_root = Path(__file__).resolve().parents[1] / "web"
        agent = (web_root / "agent.html").read_text(encoding="utf-8")
        app_page = (web_root / "app.html").read_text(encoding="utf-8")
        moments = (web_root / "moments.html").read_text(encoding="utf-8")
        self.assertIn("点击更换头像", agent)
        self.assertIn("avatarImgMarkup", agent)
        self.assertIn("compressAvatarFile", agent)
        self.assertIn("avatarImgMarkup", app_page)
        self.assertIn("avatarImgMarkup", moments)


if __name__ == "__main__":
    unittest.main()
