import os
import re
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

from ex_persona import accounts, store  # noqa: E402
from ex_persona.webapp import WEB_DIR, _distill_error_info, app  # noqa: E402


class TotpTwoFactorTest(unittest.TestCase):
    def _account(self, client):
        username = f"totp-{uuid.uuid4().hex[:8]}"
        password = "Password123!"
        response = client.post(
            "/api/auth/register", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return username, password, response.json()["user"]

    def test_totp_full_flow(self):
        with TestClient(app) as client:
            username, password, user = self._account(client)
            self.assertFalse(client.get("/api/account/security").json()["totp_enabled"])

            setup = client.post("/api/account/2fa/setup")
            self.assertEqual(setup.status_code, 200, setup.text)
            secret = setup.json()["secret"]
            self.assertIn("otpauth://totp/", setup.json()["uri"])

            wrong = client.post("/api/account/2fa/enable", json={"code": "000000"})
            self.assertEqual(wrong.status_code, 400, wrong.text)
            enabled = client.post(
                "/api/account/2fa/enable", json={"code": accounts.totp_code_now(secret)}
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
            self.assertTrue(store.get_user(user["id"])["totp_enabled"])

            client.post("/api/auth/logout")
            login = client.post(
                "/api/auth/login", json={"username": username, "password": password}
            )
            self.assertEqual(login.status_code, 200, login.text)
            self.assertTrue(login.json().get("totp_required"))
            # 密码正确但尚未通过二次验证时，会话不可用于访问接口。
            self.assertEqual(client.get("/api/me").status_code, 401)

            bad = client.post("/api/auth/2fa", json={"code": "000000"})
            self.assertEqual(bad.status_code, 401, bad.text)
            ok = client.post("/api/auth/2fa", json={"code": accounts.totp_code_now(secret)})
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(client.get("/api/me").status_code, 200)

    def test_disable_requires_password(self):
        with TestClient(app) as client:
            username, password, user = self._account(client)
            secret = client.post("/api/account/2fa/setup").json()["secret"]
            client.post("/api/account/2fa/enable", json={"code": accounts.totp_code_now(secret)})

            bad = client.post("/api/account/2fa/disable", json={"password": "wrong-pass"})
            self.assertEqual(bad.status_code, 401, bad.text)
            good = client.post("/api/account/2fa/disable", json={"password": password})
            self.assertEqual(good.status_code, 200, good.text)
            self.assertFalse(store.get_user(user["id"])["totp_enabled"])

    def test_verify_totp_rejects_bad_input(self):
        secret = accounts.generate_totp_secret()
        self.assertFalse(accounts.verify_totp("", "123456"))
        self.assertFalse(accounts.verify_totp(secret, "abcdef"))
        self.assertFalse(accounts.verify_totp(secret, "12345"))
        self.assertTrue(accounts.verify_totp(secret, accounts.totp_code_now(secret)))


class RecoveryRequestTest(unittest.TestCase):
    def test_recover_unknown_user_is_generic_and_admin_sees_known(self):
        with TestClient(app) as client:
            unknown = client.post(
                "/api/auth/recover", json={"username": f"ghost-{uuid.uuid4().hex[:8]}"}
            )
            self.assertEqual(unknown.status_code, 200, unknown.text)
            self.assertTrue(unknown.json()["ok"])

            username = f"rec-{uuid.uuid4().hex[:8]}"
            client.post(
                "/api/auth/register", json={"username": username, "password": "Password123!"}
            )
            known = client.post(
                "/api/auth/recover",
                json={"username": username, "contact": "wx:test", "note": "换手机了"},
            )
            self.assertEqual(known.status_code, 200, known.text)

        admin_name = f"adm-{uuid.uuid4().hex[:8]}"
        accounts.create_admin(admin_name, "Password123!")
        with TestClient(app) as client:
            login = client.post(
                "/api/admin/auth/login", json={"username": admin_name, "password": "Password123!"}
            )
            self.assertEqual(login.status_code, 200, login.text)
            listing = client.get("/api/admin/recovery")
            self.assertEqual(listing.status_code, 200, listing.text)
            items = [item for item in listing.json()["items"] if item["username"] == username]
            self.assertTrue(items)
            request_id = items[0]["id"]
            resolved = client.post(f"/api/admin/recovery/{request_id}/resolve")
            self.assertEqual(resolved.status_code, 200, resolved.text)
            remaining = client.get("/api/admin/recovery").json()["items"]
            self.assertFalse([item for item in remaining if item["id"] == request_id])

    def test_recovery_requires_login_for_admin_list(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/admin/recovery").status_code, 401)


class TwoFactorSetupContractTest(unittest.TestCase):
    """回归：settings 页曾以 GET 调用仅支持 POST 的 2fa/setup，导致点击无反应（405）。"""

    def test_setup_endpoint_requires_post(self):
        with TestClient(app) as client:
            username = f"post-{uuid.uuid4().hex[:8]}"
            client.post(
                "/api/auth/register",
                json={"username": username, "password": "Password123!"},
            )
            self.assertEqual(client.get("/api/account/2fa/setup").status_code, 405)

    def test_settings_page_calls_setup_with_post(self):
        html = (WEB_DIR / "settings.html").read_text(encoding="utf-8")
        match = re.search(r'api\(\s*"/api/account/2fa/setup"([^)]*)\)', html)
        self.assertIsNotNone(match, "settings.html 未调用 /api/account/2fa/setup")
        call = match.group(1)
        self.assertIn("method", call)
        self.assertIn("POST", call)


class DistillErrorInfoTest(unittest.TestCase):
    def test_error_kinds(self):
        cases = {
            "invalid api key provided": "llm_auth",
            "HTTP 401 unauthorized": "llm_auth",
            "429 too many requests": "llm_rate",
            "request timed out": "llm_network",
            "Expecting value: line 1 column 1 (char 0) json decode": "source_invalid",
            "no such file or directory": "source_missing",
            "some totally unknown failure": "unknown",
        }
        for message, expected in cases.items():
            kind, hint = _distill_error_info(RuntimeError(message))
            self.assertEqual(kind, expected, message)
            self.assertTrue(hint)

    def test_platform_quota_wins(self):
        from ex_persona.webapp import PlatformCapped

        kind, hint = _distill_error_info(PlatformCapped("今日额度已用尽"))
        self.assertEqual(kind, "platform_quota")
        self.assertTrue(hint)


class PasswordPolicyTest(unittest.TestCase):
    def test_strength_rules(self):
        accounts.validate_password_strength("Abcdefg1!x", "someone")
        for bad, reason in [
            ("Ab1!short", "至少"),
            ("alllowercase1!", "大写字母"),
            ("ALLUPPERCASE1!", "小写字母"),
            ("NoDigitsHere!!", "数字"),
            ("NoSymbols12345", "符号"),
            ("A" * 129 + "1!", "最多"),
        ]:
            with self.assertRaises(accounts.AccountError, msg=bad) as ctx:
                accounts.validate_password_strength(bad, "someone")
            self.assertIn(reason, ctx.exception.message)

    def test_rejects_weak_password(self):
        result = accounts.check_password_strength("Qwerty123", "someone")
        self.assertTrue(any("弱密码" in item for item in result["problems"]))

    def test_rejects_username_inside_password(self):
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.validate_password_strength("Xxalice99!Zz", "alice")
        self.assertIn("用户名", ctx.exception.message)

    def test_check_password_strength_reports_problems(self):
        result = accounts.check_password_strength("weak", "alice")
        self.assertFalse(result["ok"])
        self.assertTrue(result["problems"])

    def test_register_rejects_weak_password(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/register",
                json={"username": f"pw-{uuid.uuid4().hex[:8]}", "password": "password123"},
            )
            self.assertEqual(response.status_code, 400, response.text)


class LoginLockoutTest(unittest.TestCase):
    def _register(self, client):
        username = f"lock-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return username

    def _fail_five_times(self, client, username):
        last = None
        for _ in range(5):
            last = client.post(
                "/api/auth/login", json={"username": username, "password": "WrongPass1!"}
            )
        return last

    def test_five_failures_lock_even_with_correct_password(self):
        with TestClient(app) as client:
            username = self._register(client)
            client.post("/api/auth/logout")
            last = self._fail_five_times(client, username)
            self.assertEqual(last.status_code, 429, last.text)
            self.assertTrue(int(last.headers.get("Retry-After", "0")) > 0)

            blocked = client.post(
                "/api/auth/login", json={"username": username, "password": "Password123!"}
            )
            self.assertEqual(blocked.status_code, 429, blocked.text)

    def test_admin_can_unlock_account(self):
        admin_name = f"unlock-{uuid.uuid4().hex[:8]}"
        accounts.create_admin(admin_name, "Password123!")
        with TestClient(app) as client:
            username = self._register(client)
            client.post("/api/auth/logout")
            self._fail_five_times(client, username)

        with TestClient(app) as admin_client:
            login = admin_client.post(
                "/api/admin/auth/login", json={"username": admin_name, "password": "Password123!"}
            )
            self.assertEqual(login.status_code, 200, login.text)
            locked = admin_client.get("/api/admin/security/locked").json()["items"]
            target = [item for item in locked if item["username"] == username]
            self.assertTrue(target)
            unlock = admin_client.post(
                "/api/admin/security/unlock", json={"kind": "user", "id": target[0]["id"]}
            )
            self.assertEqual(unlock.status_code, 200, unlock.text)

        with TestClient(app) as client:
            ok = client.post(
                "/api/auth/login", json={"username": username, "password": "Password123!"}
            )
            self.assertEqual(ok.status_code, 200, ok.text)


class AdminForceTotpTest(unittest.TestCase):
    def setUp(self):
        store.set_feature_flag("admin_force_totp", 0, "test")

    def tearDown(self):
        store.set_feature_flag("admin_force_totp", 0, "test")

    def test_force_totp_binding_flow(self):
        admin_name = f"force-{uuid.uuid4().hex[:8]}"
        accounts.create_admin(admin_name, "Password123!")
        store.set_feature_flag("admin_force_totp", 1, "test")
        with TestClient(app) as client:
            login = client.post(
                "/api/admin/auth/login", json={"username": admin_name, "password": "Password123!"}
            )
            self.assertEqual(login.status_code, 200, login.text)
            self.assertTrue(login.json().get("totp_setup_required"))
            self.assertEqual(client.get("/api/admin/summary").status_code, 403)

            setup = client.post("/api/admin/account/2fa/setup")
            self.assertEqual(setup.status_code, 200, setup.text)
            secret = setup.json()["secret"]
            enable = client.post(
                "/api/admin/account/2fa/enable", json={"code": accounts.totp_code_now(secret)}
            )
            self.assertEqual(enable.status_code, 200, enable.text)
            self.assertEqual(client.get("/api/admin/summary").status_code, 200)


class PasswordChangeSessionTest(unittest.TestCase):
    def test_change_password_invalidates_other_sessions(self):
        with TestClient(app) as first:
            username = f"sess-{uuid.uuid4().hex[:8]}"
            first.post(
                "/api/auth/register", json={"username": username, "password": "Password123!"}
            )
            with TestClient(app) as second:
                login = second.post(
                    "/api/auth/login", json={"username": username, "password": "Password123!"}
                )
                self.assertEqual(login.status_code, 200, login.text)
                self.assertEqual(second.get("/api/me").status_code, 200)

                change = first.post(
                    "/api/account/password",
                    json={"current": "Password123!", "new": "Newpass123!x"},
                )
                self.assertEqual(change.status_code, 200, change.text)
                self.assertEqual(second.get("/api/me").status_code, 401)
                self.assertEqual(first.get("/api/me").status_code, 200)


if __name__ == "__main__":
    unittest.main()
