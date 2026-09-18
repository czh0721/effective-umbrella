import os
import tempfile
import unittest
import uuid

if "PERSONA_DATA_DIR" not in os.environ:
    _TMP = tempfile.TemporaryDirectory()
    os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ.setdefault("PERSONA_SECRET_KEY", "unit-test-secret")
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import accounts, config, crypto, store  # noqa: E402
from ex_persona.webapp import app  # noqa: E402


def _fake_agent(reply, platform=False):
    class _Config:
        ready = True
        api_key = "k"
        base_url = "https://api.deepseek.com/v1"
        model = "deepseek-chat"
        platform = False

    _Config.platform = platform

    class _Agent:
        config = _Config()
        last_error = None
        last_fallback = False

        def reply(self, *args, **kwargs):
            return reply

    return _Agent()


def _make_admin(username, password="Password123!", name=""):
    return accounts.create_admin(username, password, name=name)


def _admin_login(client, username, password="Password123!"):
    response = client.post(
        "/api/admin/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


class AdminAndReportTest(unittest.TestCase):
    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_report_flow(self):
        with TestClient(app) as client:
            self._register(client, f"reporter-{uuid.uuid4().hex[:8]}")
            created = client.post("/api/reports", json={"category": "bug", "detail": "页面报错"})
            self.assertEqual(created.status_code, 200, created.text)
            report_id = created.json()["id"]
            self.assertTrue(report_id)
            self.assertEqual(store.count_reports(), 1)

            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)
            listing = client.get("/api/admin/reports")
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertEqual(listing.json()["items"][0]["id"], report_id)

            resolved = client.post(f"/api/admin/reports/{report_id}/resolve")
            self.assertEqual(resolved.status_code, 200, resolved.text)
            self.assertEqual(store.count_reports(status="open"), 0)

    def test_non_admin_blocked(self):
        with TestClient(app) as client:
            self._register(client, f"plain-{uuid.uuid4().hex[:8]}")
            blocked = client.get("/api/admin/summary")
            self.assertEqual(blocked.status_code, 401)

    def test_admin_summary_counts(self):
        with TestClient(app) as client:
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)
            summary = client.get("/api/admin/summary")
            self.assertEqual(summary.status_code, 200, summary.text)
            data = summary.json()
            self.assertGreaterEqual(data["users"], 1)
            self.assertIn("platform_usage", data)
            users = client.get("/api/admin/users")
            self.assertEqual(users.status_code, 200, users.text)
            self.assertTrue(users.json()["items"])

    def test_onboarding_reported(self):
        with TestClient(app) as client:
            self._register(client, f"onboard-{uuid.uuid4().hex[:8]}")
            data = client.get("/api/me").json()
            self.assertIn("onboarding", data)
            self.assertFalse(data["onboarding"]["has_persona"])
            self.assertFalse(data["onboarding"]["done"])

    def test_report_requires_login(self):
        with TestClient(app) as client:
            response = client.post("/api/reports", json={"detail": "x"})
            self.assertEqual(response.status_code, 401)


class AdminSeparationTest(unittest.TestCase):
    def test_registered_user_is_never_admin(self):
        username = f"sep-{uuid.uuid4().hex[:8]}"
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/register", json={"username": username, "password": "Password123!"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["user"]["role"], "user")
            self.assertEqual(client.get("/api/admin/summary").status_code, 401)
        user = store.get_user_by_username(username)
        self.assertEqual(user["role"], "user")
        self.assertNotIn("admin", {row["role"] for row in store.list_users()})

    def test_store_refuses_to_promote_user(self):
        user = store.create_user(f"noPromo-{uuid.uuid4().hex[:8]}", "h", "s")
        with self.assertRaises(ValueError):
            store.set_user_role(user["id"], "admin")
        self.assertEqual(store.get_user(user["id"])["role"], "user")

    def test_admin_session_cannot_access_user_api(self):
        with TestClient(app) as client:
            username = f"onlyadmin-{uuid.uuid4().hex[:8]}"
            _make_admin(username)
            _admin_login(client, username)
            self.assertEqual(client.get("/api/admin/me").status_code, 200)
            self.assertEqual(client.get("/api/me").status_code, 401)

    def test_user_session_cannot_access_admin_api(self):
        with TestClient(app) as client:
            client.post(
                "/api/auth/register",
                json={"username": f"onlyuser-{uuid.uuid4().hex[:8]}", "password": "Password123!"},
            )
            self.assertEqual(client.get("/api/me").status_code, 200)
            self.assertEqual(client.get("/api/admin/me").status_code, 401)

    def test_admin_login_page_rejects_user_credentials(self):
        with TestClient(app) as client:
            username = f"mixed-{uuid.uuid4().hex[:8]}"
            client.post(
                "/api/auth/register", json={"username": username, "password": "Password123!"}
            )
            response = client.post(
                "/api/admin/auth/login", json={"username": username, "password": "Password123!"}
            )
            self.assertEqual(response.status_code, 401, response.text)

    def test_report_notifies_admins(self):
        admin = _make_admin(f"adm-{uuid.uuid4().hex[:8]}")
        with TestClient(app) as client:
            client.post(
                "/api/auth/register",
                json={"username": f"rp-{uuid.uuid4().hex[:8]}", "password": "Password123!"},
            )
            created = client.post("/api/reports", json={"category": "abuse", "detail": "有人骂人"})
            self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(store.count_unread_admin_alerts(admin["id"]), 1)

    def test_admin_logout_clears_admin_session(self):
        with TestClient(app) as client:
            username = f"bye-{uuid.uuid4().hex[:8]}"
            _make_admin(username)
            _admin_login(client, username)
            self.assertEqual(client.get("/api/admin/me").status_code, 200)
            self.assertEqual(client.post("/api/admin/auth/logout").status_code, 200)
            self.assertEqual(client.get("/api/admin/me").status_code, 401)


class PlatformFallbackTest(unittest.TestCase):
    def tearDown(self):
        for key in ("PERSONA_PLATFORM_API_KEY", "PERSONA_PLATFORM_DAILY_LIMIT"):
            os.environ.pop(key, None)

    def test_build_config_falls_back_to_platform(self):
        os.environ["PERSONA_PLATFORM_API_KEY"] = "platform-key"
        os.environ["PERSONA_PLATFORM_MODEL"] = "platform-model"
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/api/auth/register",
                    json={"username": f"pf-{uuid.uuid4().hex[:8]}", "password": "Password123!"},
                )
                user_id = response.json()["user"]["id"]
                from ex_persona.webapp import build_config

                cfg = build_config(user_id, None)
        finally:
            os.environ.pop("PERSONA_PLATFORM_MODEL", None)
        self.assertTrue(cfg.platform)
        self.assertEqual(cfg.model, "platform-model")
        self.assertTrue(cfg.ready)

    def test_user_key_wins_over_platform(self):
        os.environ["PERSONA_PLATFORM_API_KEY"] = "platform-key"
        salt, digest = accounts.hash_password("Password123!")
        user = store.create_user(
            username=f"own-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
        )
        store.set_model_config(user["id"], crypto.encrypt("own-key"), "https://api.deepseek.com/v1", "deepseek-chat")
        cfg = config.load_platform_config()
        self.assertTrue(cfg.ready)
        from ex_persona.webapp import build_config

        built = build_config(user["id"], None)
        self.assertFalse(built.platform)
        self.assertEqual(built.api_key, "own-key")

    def test_invalid_own_key_falls_back_to_platform(self):
        os.environ["PERSONA_PLATFORM_API_KEY"] = "platform-key"
        try:
            salt, digest = accounts.hash_password("Password123!")
            user = store.create_user(
                username=f"stale-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
            )
            store.set_model_config(
                user["id"], crypto.encrypt("legacy"), "https://api.deepseek.com/v1", "deepseek-chat"
            )
            store.set_model_key_status(user["id"], "invalid", "模型鉴权失败")
            from ex_persona.webapp import build_config

            built = build_config(user["id"], None)
        finally:
            os.environ.pop("PERSONA_PLATFORM_API_KEY", None)
        # 历史遗留且已失效的自带 Key 不应再拦截平台内置模型。
        self.assertTrue(built.platform)
        self.assertEqual(built.api_key, "platform-key")

    def test_platform_usage_counting(self):
        salt, digest = accounts.hash_password("Password123!")
        user = store.create_user(
            username=f"usage-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
        )
        before = store.count_platform_calls_since(user["id"], "2000-01-01T00:00:00+00:00")
        store.add_platform_call(user["id"])
        after = store.count_platform_calls_since(user["id"], "2000-01-01T00:00:00+00:00")
        self.assertEqual(after, before + 1)


class PlatformQuotaRouteTest(unittest.TestCase):
    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_platform_daily_cap_replies_with_notice(self):
        os.environ["PERSONA_PLATFORM_DAILY_LIMIT"] = "1"
        try:
            with TestClient(app) as client:
                user = self._register(client, f"cap-{uuid.uuid4().hex[:8]}")
                persona = client.post("/api/personas", json={"name": "限额"}).json()["persona"]
                token = f"tok-{uuid.uuid4().hex[:8]}"
                store.upsert_wechat_binding(
                    user["id"],
                    bridge_token=token,
                    home_dir=f"/tmp/{token}",
                    persona_id=persona["id"],
                )
                from ex_persona import webapp as webapp_module

                original = webapp_module.get_agent
                webapp_module.get_agent = lambda uid, p: _fake_agent("在呢", platform=True)
                try:
                    first = client.post(
                        f"/v1/chat/completions/{token}",
                        json={"user": "c1", "messages": [{"role": "user", "content": "你好"}]},
                    )
                    second = client.post(
                        f"/v1/chat/completions/{token}",
                        json={"user": "c1", "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = original
        finally:
            os.environ.pop("PERSONA_PLATFORM_DAILY_LIMIT", None)
        self.assertEqual(first.status_code, 200, first.text)
        # 达到平台日额度后不再抛 429（会被 weclaw 丢弃），改为以正常回复告知用户。
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["choices"][0]["message"]["content"], webapp_module.REPLY_PLATFORM_CAPPED)
        self.assertEqual(store.count_platform_calls_since(user["id"], "2000-01-01T00:00:00+00:00"), 1)


class BridgeContactIsolationTest(unittest.TestCase):
    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_reply_without_contact_does_not_leak_other_contacts(self):
        with TestClient(app) as client:
            user = self._register(client, f"iso-{uuid.uuid4().hex[:8]}")
            persona = client.post("/api/personas", json={"name": "小念"}).json()["persona"]
            token = f"tok-{uuid.uuid4().hex[:8]}"
            store.upsert_wechat_binding(
                user["id"], bridge_token=token, home_dir=f"/tmp/{token}",
                persona_id=persona["id"],
            )
            store.add_turn(user["id"], persona["id"], "user", "来自A的联系人消息", contact="alice")
            store.add_turn(user["id"], persona["id"], "assistant", "A的回复", contact="alice")
            store.add_turn(user["id"], persona["id"], "user", "来自B的联系人消息", contact="bob")

            captured = {}
            fake = _fake_agent("好的")

            def _reply(message, history=None, temperature=None, extra_context=None):
                captured["history"] = history
                return "好的"

            fake.reply = _reply
            from ex_persona import webapp as webapp_module

            original = webapp_module.get_agent
            webapp_module.get_agent = lambda uid, p: fake
            try:
                response = client.post(
                    f"/v1/chat/completions/{token}",
                    json={"messages": [{"role": "user", "content": "在吗"}]},
                )
            finally:
                webapp_module.get_agent = original

            self.assertEqual(response.status_code, 200, response.text)
            serialized = repr(captured.get("history"))
            self.assertNotIn("来自A的联系人消息", serialized)
            self.assertNotIn("来自B的联系人消息", serialized)
            self.assertNotIn("A的回复", serialized)


class AccountStatusTest(unittest.TestCase):
    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_disabled_user_cannot_login_or_use_session(self):
        username = f"dis-{uuid.uuid4().hex[:8]}"
        with TestClient(app) as client:
            user = self._register(client, username)
            self.assertEqual(client.get("/api/me").status_code, 200)
            store.set_user_status(user["id"], "disabled")
            # 停用后现有会话立即失效，且不能重新登录。
            self.assertEqual(client.get("/api/me").status_code, 401)
            relogin = client.post(
                "/api/auth/login", json={"username": username, "password": "Password123!"}
            )
            self.assertEqual(relogin.status_code, 403, relogin.text)

    def test_admin_can_disable_and_enable_user(self):
        salt, digest = accounts.hash_password("Password123!")
        target = store.create_user(
            username=f"tgt-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
        )
        admin_name = f"adm-{uuid.uuid4().hex[:8]}"
        _make_admin(admin_name)
        with TestClient(app) as client:
            _admin_login(client, admin_name)
            disabled = client.post(
                f"/api/admin/users/{target['id']}/status", json={"status": "disabled"}
            )
            self.assertEqual(disabled.status_code, 200, disabled.text)
            self.assertEqual(store.get_user(target["id"])["status"], "disabled")
            enabled = client.post(
                f"/api/admin/users/{target['id']}/status", json={"status": "active"}
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
            self.assertEqual(store.get_user(target["id"])["status"], "active")

    def test_bridge_silent_for_disabled_account(self):
        salt, digest = accounts.hash_password("Password123!")
        owner = store.create_user(
            username=f"own-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
        )
        persona = store.create_persona(owner["id"], "小念", f"/tmp/{uuid.uuid4().hex}")
        token = f"tok-{uuid.uuid4().hex[:8]}"
        store.upsert_wechat_binding(
            owner["id"], bridge_token=token, home_dir=f"/tmp/{token}", persona_id=persona["id"]
        )
        store.set_user_status(owner["id"], "disabled")
        with TestClient(app) as client:
            response = client.post(
                f"/v1/chat/completions/{token}",
                json={"user": "c1", "messages": [{"role": "user", "content": "你好"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "")


class CsrfMiddlewareTest(unittest.TestCase):
    def test_cross_site_write_blocked(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/login",
                json={"username": "x", "password": "y"},
                headers={"sec-fetch-site": "cross-site"},
            )
            self.assertEqual(response.status_code, 403, response.text)

    def test_mismatched_origin_blocked(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/login",
                json={"username": "x", "password": "y"},
                headers={"origin": "https://evil.example"},
            )
            self.assertEqual(response.status_code, 403, response.text)

    def test_same_host_origin_allowed(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/login",
                json={"username": "x", "password": "y"},
                headers={"origin": "http://testserver"},
            )
            self.assertNotEqual(response.status_code, 403, response.text)

    def test_originless_write_allowed(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/register",
                json={"username": f"csrf-{uuid.uuid4().hex[:8]}", "password": "Password123!"},
            )
            self.assertEqual(response.status_code, 200, response.text)

    def test_bridge_path_exempt_from_csrf(self):
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions/bogus-token",
                json={"user": "c1", "messages": [{"role": "user", "content": "hi"}]},
                headers={"sec-fetch-site": "cross-site"},
            )
            self.assertNotEqual(response.status_code, 403, response.text)


class AdminSecurityTest(unittest.TestCase):
    def _register(self, client, username, password="Password123!"):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_hsts_header_on_https(self):
        with TestClient(app) as client:
            response = client.get("/health", headers={"x-forwarded-proto": "https"})
            self.assertEqual(response.status_code, 200)
            header = response.headers.get("strict-transport-security", "")
            self.assertIn("max-age=31536000", header)

    def test_logout_all_invalidates_session(self):
        with TestClient(app) as client:
            self._register(client, f"la-{uuid.uuid4().hex[:8]}")
            self.assertEqual(client.get("/api/me").status_code, 200)
            response = client.post("/api/account/logout-all")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get("/api/me").status_code, 401)

    def test_admin_can_reset_user_password(self):
        salt, digest = accounts.hash_password("Password123!")
        target = store.create_user(
            username=f"rp-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
        )
        admin_name = f"pr-{uuid.uuid4().hex[:8]}"
        _make_admin(admin_name)
        with TestClient(app) as client:
            _admin_login(client, admin_name)
            response = client.post(
                f"/api/admin/users/{target['id']}/password", json={"password": "newPassword123!"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            login = client.post(
                "/api/auth/login",
                json={"username": target["username"], "password": "newPassword123!"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            audit = client.get("/api/admin/audit")
            actions = [item["action"] for item in audit.json()["items"]]
            self.assertIn("user.reset_password", actions)

    def test_audit_requires_admin(self):
        with TestClient(app) as client:
            self._register(client, f"na-{uuid.uuid4().hex[:8]}")
            self.assertEqual(client.get("/api/admin/audit").status_code, 401)


class AdminPageRouteTest(unittest.TestCase):
    def test_admin_page_redirects_anonymous_to_login(self):
        with TestClient(app) as client:
            response = client.get("/admin", follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "/admin/login")

    def test_admin_login_page_served(self):
        with TestClient(app) as client:
            page = client.get("/admin/login")
            self.assertEqual(page.status_code, 200)
            self.assertIn("管理后台登录", page.text)

    def test_admin_login_redirects_admin_to_console(self):
        with TestClient(app) as client:
            username = f"lg-adm-{uuid.uuid4().hex[:8]}"
            _make_admin(username)
            _admin_login(client, username)
            response = client.get("/admin/login", follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "/admin")

    def test_admin_page_redirects_logged_in_user_to_login(self):
        with TestClient(app) as client:
            client.post(
                "/api/auth/register",
                json={"username": f"pg-{uuid.uuid4().hex[:8]}", "password": "Password123!"},
            )
            page = client.get("/admin", follow_redirects=False)
            self.assertEqual(page.status_code, 302)
            self.assertEqual(page.headers["location"], "/admin/login")

    def test_admin_page_served_for_admin(self):
        with TestClient(app) as client:
            username = f"pg-adm-{uuid.uuid4().hex[:8]}"
            _make_admin(username)
            _admin_login(client, username)
            page = client.get("/admin")
            self.assertEqual(page.status_code, 200)
            self.assertIn("管理后台", page.text)

    def test_legacy_admin_path_redirects(self):
        with TestClient(app) as client:
            response = client.get("/app/admin", follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "/admin")


if __name__ == "__main__":
    unittest.main()
