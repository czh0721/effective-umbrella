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


class AdminAndReportTest(unittest.TestCase):
    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "password123"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _login(self, client, username, password):
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _make_admin(self, username):
        salt, digest = accounts.hash_password("password123")
        return store.create_user(
            username=username, password_hash=digest, password_salt=salt, role="admin"
        )

    def test_report_flow(self):
        with TestClient(app) as client:
            self._register(client, f"reporter-{uuid.uuid4().hex[:8]}")
            created = client.post("/api/reports", json={"category": "bug", "detail": "页面报错"})
            self.assertEqual(created.status_code, 200, created.text)
            report_id = created.json()["id"]
            self.assertTrue(report_id)
            self.assertEqual(store.count_reports(), 1)

            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            self._make_admin(admin_username)
            self._login(client, admin_username, "password123")
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
            self.assertEqual(blocked.status_code, 403)

    def test_admin_summary_counts(self):
        with TestClient(app) as client:
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            self._make_admin(admin_username)
            self._login(client, admin_username, "password123")
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
                    json={"username": f"pf-{uuid.uuid4().hex[:8]}", "password": "password123"},
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
        salt, digest = accounts.hash_password("password123")
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
            salt, digest = accounts.hash_password("password123")
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
        salt, digest = accounts.hash_password("password123")
        user = store.create_user(
            username=f"usage-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
        )
        before = store.count_platform_calls_since(user["id"], "2000-01-01T00:00:00+00:00")
        store.add_platform_call(user["id"])
        after = store.count_platform_calls_since(user["id"], "2000-01-01T00:00:00+00:00")
        self.assertEqual(after, before + 1)


class AdminWhitelistTest(unittest.TestCase):
    def test_env_whitelist_does_not_promote_on_register(self):
        # 注册接口不再按白名单自动提权，避免注册同名大小写变体拿到后台权限。
        username = f"wl-{uuid.uuid4().hex[:8]}"
        os.environ["PERSONA_ADMIN_USERS"] = username
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/api/auth/register", json={"username": username, "password": "password123"}
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["user"]["role"], "user")
                self.assertEqual(client.get("/api/admin/summary").status_code, 403)
        finally:
            os.environ.pop("PERSONA_ADMIN_USERS", None)

    def test_env_whitelist_promotes_existing_account_at_startup(self):
        username = f"wl-{uuid.uuid4().hex[:8]}"
        salt, digest = accounts.hash_password("password123")
        user = store.create_user(username=username, password_hash=digest, password_salt=salt)
        os.environ["PERSONA_ADMIN_USERS"] = username.upper()
        try:
            from ex_persona import webapp as webapp_module

            webapp_module._apply_admin_whitelist()
            self.assertEqual(store.get_user(user["id"])["role"], "admin")
        finally:
            os.environ.pop("PERSONA_ADMIN_USERS", None)

    def test_set_role_store_and_report_notifies_admin(self):
        salt, digest = accounts.hash_password("password123")
        admin = store.create_user(
            username=f"adm-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
        )
        store.set_user_role(admin["id"], "admin")
        self.assertEqual(store.get_user(admin["id"])["role"], "admin")
        with TestClient(app) as client:
            client.post(
                "/api/auth/register",
                json={"username": f"rp-{uuid.uuid4().hex[:8]}", "password": "password123"},
            )
            created = client.post("/api/reports", json={"category": "abuse", "detail": "有人骂人"})
            self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(store.count_unread_alerts(admin["id"]), 1)


class PlatformQuotaRouteTest(unittest.TestCase):
    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "password123"}
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
            "/api/auth/register", json={"username": username, "password": "password123"}
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
            "/api/auth/register", json={"username": username, "password": "password123"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _make_admin(self, username):
        salt, digest = accounts.hash_password("password123")
        return store.create_user(
            username=username, password_hash=digest, password_salt=salt, role="admin"
        )

    def test_disabled_user_cannot_login_or_use_session(self):
        username = f"dis-{uuid.uuid4().hex[:8]}"
        with TestClient(app) as client:
            user = self._register(client, username)
            self.assertEqual(client.get("/api/me").status_code, 200)
            store.set_user_status(user["id"], "disabled")
            # 停用后现有会话立即失效，且不能重新登录。
            self.assertEqual(client.get("/api/me").status_code, 401)
            relogin = client.post(
                "/api/auth/login", json={"username": username, "password": "password123"}
            )
            self.assertEqual(relogin.status_code, 403, relogin.text)

    def test_admin_can_disable_and_enable_user(self):
        salt, digest = accounts.hash_password("password123")
        target = store.create_user(
            username=f"tgt-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
        )
        admin_name = f"adm-{uuid.uuid4().hex[:8]}"
        admin = self._make_admin(admin_name)
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login", json={"username": admin_name, "password": "password123"}
            )
            self.assertEqual(login.status_code, 200, login.text)
            disabled = client.post(
                f"/api/admin/users/{target['id']}/status", json={"status": "disabled"}
            )
            self.assertEqual(disabled.status_code, 200, disabled.text)
            self.assertEqual(store.get_user(target["id"])["status"], "disabled")
            # 不允许停用自己。
            self_off = client.post(
                f"/api/admin/users/{admin['id']}/status", json={"status": "disabled"}
            )
            self.assertEqual(self_off.status_code, 400, self_off.text)
            enabled = client.post(
                f"/api/admin/users/{target['id']}/status", json={"status": "active"}
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
            self.assertEqual(store.get_user(target["id"])["status"], "active")

    def test_bridge_silent_for_disabled_account(self):
        salt, digest = accounts.hash_password("password123")
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
                json={"username": f"csrf-{uuid.uuid4().hex[:8]}", "password": "password123"},
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
    def _register(self, client, username, password="password123"):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _login(self, client, username, password="password123"):
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _make_admin(self, username, password="password123"):
        salt, digest = accounts.hash_password(password)
        return store.create_user(
            username=username, password_hash=digest, password_salt=salt, role="admin"
        )

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

    def test_admin_role_change_writes_audit(self):
        with TestClient(app) as client:
            admin_name = f"ar-{uuid.uuid4().hex[:8]}"
            self._make_admin(admin_name)
            self._login(client, admin_name)
            salt, digest = accounts.hash_password("password123")
            target = store.create_user(
                username=f"tg-{uuid.uuid4().hex[:8]}", password_hash=digest, password_salt=salt
            )
            response = client.post(f"/api/admin/users/{target['id']}/role", json={"role": "admin"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(store.get_user(target["id"])["role"], "admin")
            audit = client.get("/api/admin/audit")
            self.assertEqual(audit.status_code, 200, audit.text)
            actions = [item["action"] for item in audit.json()["items"]]
            self.assertIn("user.set_role", actions)

    def test_admin_cannot_demote_self(self):
        with TestClient(app) as client:
            admin_name = f"sd-{uuid.uuid4().hex[:8]}"
            admin = self._make_admin(admin_name)
            self._login(client, admin_name)
            response = client.post(f"/api/admin/users/{admin['id']}/role", json={"role": "user"})
            self.assertEqual(response.status_code, 400, response.text)

    def test_admin_reset_password_invalidates_target(self):
        with TestClient(app) as admin_client, TestClient(app) as user_client:
            admin_name = f"pr-{uuid.uuid4().hex[:8]}"
            self._make_admin(admin_name)
            self._login(admin_client, admin_name)
            target = self._register(user_client, f"tp-{uuid.uuid4().hex[:8]}")
            response = admin_client.post(
                f"/api/admin/users/{target['id']}/password", json={"password": "newpassword123"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(user_client.get("/api/me").status_code, 401)
            self._login(user_client, target["username"], "newpassword123")

    def test_admin_cannot_reset_other_admin_password(self):
        with TestClient(app) as client:
            actor_name = f"ra-{uuid.uuid4().hex[:8]}"
            self._make_admin(actor_name)
            self._login(client, actor_name)
            victim = self._make_admin(f"rv-{uuid.uuid4().hex[:8]}")
            response = client.post(
                f"/api/admin/users/{victim['id']}/password", json={"password": "newpassword123"}
            )
            self.assertEqual(response.status_code, 403, response.text)

    def test_audit_requires_admin(self):
        with TestClient(app) as client:
            self._register(client, f"na-{uuid.uuid4().hex[:8]}")
            self.assertEqual(client.get("/api/admin/audit").status_code, 403)


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
            salt, digest = accounts.hash_password("password123")
            user = store.create_user(
                username=f"lg-adm-{uuid.uuid4().hex[:8]}",
                password_hash=digest,
                password_salt=salt,
                role="admin",
            )
            client.post(
                "/api/auth/login",
                json={"username": user["username"], "password": "password123"},
            )
            response = client.get("/admin/login", follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "/admin")

    def test_admin_page_redirects_non_admin(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/register",
                json={"username": f"pg-{uuid.uuid4().hex[:8]}", "password": "password123"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            page = client.get("/admin", follow_redirects=False)
            self.assertEqual(page.status_code, 302)
            self.assertEqual(page.headers["location"], "/app")

    def test_admin_page_served_for_admin(self):
        with TestClient(app) as client:
            salt, digest = accounts.hash_password("password123")
            user = store.create_user(
                username=f"pg-adm-{uuid.uuid4().hex[:8]}",
                password_hash=digest,
                password_salt=salt,
                role="admin",
            )
            client.post(
                "/api/auth/login",
                json={"username": user["username"], "password": "password123"},
            )
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
