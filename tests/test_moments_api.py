import os
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import persona_settings, store, webapp  # noqa: E402
from ex_persona.config import PlatformConfig  # noqa: E402


class _FakeAgent:
    def __init__(
        self,
        platform=True,
        ready=True,
        moment_text="今天心情不错",
        reply_text="谢谢你呀",
        boom=False,
    ):
        self.config = SimpleNamespace(platform=platform, ready=ready)
        self.moment_text = moment_text
        self.reply_text = reply_text
        self.boom = boom
        self.compose_calls = []
        self.reply_calls = []

    def compose_moment(self, prompt, *args, **kwargs):
        self.compose_calls.append(prompt)
        if self.boom:
            raise RuntimeError("model down")
        return self.moment_text

    def reply(self, message, extra_context=None, **kwargs):
        self.reply_calls.append((message, extra_context))
        if self.boom:
            raise RuntimeError("model down")
        return self.reply_text


def _platform(enabled=True, cost=3):
    return PlatformConfig(
        api_key="platform-key", base_url="http://x/v1", model="m",
        enabled=enabled, per_turn_cost=cost, new_user_gift=0,
    )


class MomentsApiTest(unittest.TestCase):
    def setUp(self):
        store.init_db()
        self.agent = _FakeAgent()
        self.patches = [
            mock.patch.object(webapp, "load_platform_config", lambda: _platform()),
            mock.patch.object(webapp, "build_config", lambda *a, **k: GeneralPlatform()),
            mock.patch.object(webapp, "get_agent", lambda *a, **k: self.agent),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _client(self):
        return TestClient(webapp.app)

    def _register(self, client):
        username = f"mom-api-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "password123"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _persona(self, user, max_per_day=2, enabled=True, **overrides):
        settings = persona_settings.load(None)
        settings["moments"]["enabled"] = enabled
        settings["moments"]["max_per_day"] = max_per_day
        settings["moments"].update(overrides)
        persona = store.create_persona(
            user["id"], "小念", f"/tmp/{uuid.uuid4().hex}",
            settings=persona_settings.dump(settings),
        )
        store.update_persona(user["id"], persona["id"], status="ready")
        return store.get_persona(user["id"], persona["id"])

    def _fund(self, user, amount=100):
        store.grant_credits(user["id"], amount, reason="test", actor="system")

    def test_full_flow(self):
        with self._client() as client:
            user = self._register(client)
            self._fund(user)
            persona = self._persona(user)

            listed = client.get(f"/api/personas/{persona['id']}/moments")
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.json()["moments"], [])
            self.assertEqual(listed.json()["per_turn_cost"], 3)

            published = client.post(f"/api/personas/{persona['id']}/moments")
            self.assertEqual(published.status_code, 200, published.text)
            moment = published.json()["moment"]
            self.assertEqual(moment["content"], "今天心情不错")
            self.assertEqual(moment["source"], "manual")
            self.assertEqual(store.get_credits(user["id"]), 97)

            # 点赞幂等
            like = client.post(f"/api/moments/{moment['id']}/like")
            self.assertEqual(like.json(), {"liked": True, "like_count": 1})
            self.assertEqual(client.post(f"/api/moments/{moment['id']}/like").json()["like_count"], 1)
            unlike = client.delete(f"/api/moments/{moment['id']}/like")
            self.assertEqual(unlike.json(), {"liked": False, "like_count": 0})

            # 评论触发回应并按轮扣费
            comment = client.post(
                f"/api/moments/{moment['id']}/comments", json={"content": "在干嘛"}
            )
            self.assertEqual(comment.status_code, 200, comment.text)
            body = comment.json()["comment"]
            self.assertEqual(body["reply"], "谢谢你呀")
            self.assertEqual(body["reply_status"], "done")
            self.assertEqual(store.get_credits(user["id"]), 94)

            comments = client.get(f"/api/moments/{moment['id']}/comments")
            self.assertEqual(len(comments.json()["comments"]), 1)

            # 删除动态并级联
            removed = client.delete(f"/api/moments/{moment['id']}")
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertEqual(client.get(f"/api/personas/{persona['id']}/moments").json()["moments"], [])
            self.assertEqual(client.get(f"/api/moments/{moment['id']}/comments").status_code, 404)
            self.assertEqual(client.delete(f"/api/moments/{moment['id']}").status_code, 404)

    def test_cross_user_is_isolated(self):
        with self._client() as client:
            owner = self._register(client)
            self._fund(owner)
            persona = self._persona(owner)
            moment = client.post(f"/api/personas/{persona['id']}/moments").json()["moment"]

            client.post("/api/auth/logout")
            intruder = self._register(client)
            self._fund(intruder)

            self.assertEqual(
                client.get(f"/api/personas/{persona['id']}/moments").status_code, 404
            )
            self.assertEqual(
                client.post(f"/api/personas/{persona['id']}/moments").status_code, 404
            )
            self.assertEqual(client.get(f"/api/moments/{moment['id']}/comments").status_code, 404)
            self.assertEqual(client.post(f"/api/moments/{moment['id']}/like").status_code, 404)
            self.assertEqual(client.delete(f"/api/moments/{moment['id']}").status_code, 404)
            self.assertEqual(
                client.post(
                    f"/api/moments/{moment['id']}/comments", json={"content": "hi"}
                ).status_code,
                404,
            )

    def test_daily_cap_blocks_manual_publish(self):
        with self._client() as client:
            user = self._register(client)
            self._fund(user)
            persona = self._persona(user, max_per_day=1)

            self.assertEqual(
                client.post(f"/api/personas/{persona['id']}/moments").status_code, 200
            )
            blocked = client.post(f"/api/personas/{persona['id']}/moments")
            self.assertEqual(blocked.status_code, 429, blocked.text)
            self.assertEqual(store.get_credits(user["id"]), 97)
            self.assertEqual(
                len(client.get(f"/api/personas/{persona['id']}/moments").json()["moments"]), 1
            )

    def test_publish_failure_refunds_and_persists_nothing(self):
        self.agent.moment_text = "   "
        with self._client() as client:
            user = self._register(client)
            self._fund(user)
            persona = self._persona(user)

            failed = client.post(f"/api/personas/{persona['id']}/moments")
            self.assertEqual(failed.status_code, 502, failed.text)
            self.assertEqual(store.get_credits(user["id"]), 100)
            self.assertEqual(
                client.get(f"/api/personas/{persona['id']}/moments").json()["moments"], []
            )

    def test_insufficient_credits_rejects_without_persisting(self):
        with self._client() as client:
            user = self._register(client)
            persona = self._persona(user)
            moment = store.add_moment(user["id"], persona["id"], "动态")

            rejected = client.post(
                f"/api/moments/{moment['id']}/comments", json={"content": "你好"}
            )
            self.assertEqual(rejected.status_code, 402, rejected.text)
            self.assertEqual(
                client.get(f"/api/moments/{moment['id']}/comments").json()["comments"], []
            )

    def test_comment_failure_refunds_then_retry_succeeds(self):
        self.agent.boom = True
        with self._client() as client:
            user = self._register(client)
            self._fund(user)
            persona = self._persona(user)
            moment = store.add_moment(user["id"], persona["id"], "动态")

            failed = client.post(
                f"/api/moments/{moment['id']}/comments", json={"content": "在吗"}
            )
            self.assertEqual(failed.status_code, 200, failed.text)
            self.assertEqual(failed.json()["comment"]["reply_status"], "failed")
            self.assertEqual(store.get_credits(user["id"]), 100)

            self.agent.boom = False
            retried = client.post(
                f"/api/moments/comments/{failed.json()['comment']['id']}/retry"
            )
            self.assertEqual(retried.status_code, 200, retried.text)
            self.assertEqual(retried.json()["comment"]["reply_status"], "done")
            self.assertEqual(retried.json()["comment"]["reply"], "谢谢你呀")
            self.assertEqual(store.get_credits(user["id"]), 97)

    def test_own_key_persona_is_free(self):
        base = _FakeAgent(platform=False)
        with mock.patch.object(webapp, "build_config", lambda *a, **k: OwnKeyConfig()):
            with mock.patch.object(webapp, "get_agent", lambda *a, **k: base):
                with self._client() as client:
                    user = self._register(client)
                    persona = self._persona(user)
                    listed = client.get(f"/api/personas/{persona['id']}/moments").json()
                    self.assertEqual(listed["per_turn_cost"], 0)

                    published = client.post(f"/api/personas/{persona['id']}/moments")
                    self.assertEqual(published.status_code, 200, published.text)
                    self.assertEqual(store.get_credits(user["id"]), 0)


def GeneralPlatform():
    return SimpleNamespace(platform=True)


def OwnKeyConfig():
    return SimpleNamespace(platform=False)


if __name__ == "__main__":
    unittest.main()
