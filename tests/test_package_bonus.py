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


class PackageBonusTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _user(self, username, coins=0):
        user = store.create_user(username, "h", "s")
        if coins:
            store.grant_coins(user["id"], coins)
        return user

    def _package(self, package_id=None, name="赠送包", **kwargs):
        return store.upsert_credit_package(
            package_id,
            name,
            kwargs.pop("credits", 100),
            0,
            "",
            1,
            True,
            coins=kwargs.pop("coins", 10),
            validity_days=kwargs.pop("validity_days", 30),
            bonus_credits=kwargs.pop("bonus_credits", 0),
            bonus_tickets=kwargs.pop("bonus_tickets", 0),
        )

    def test_bonus_fields_persisted(self):
        package = self._package(name="月度福利包", bonus_credits=20, bonus_tickets=2)
        self.assertEqual(package["bonus_credits"], 20)
        self.assertEqual(package["bonus_tickets"], 2)
        listed = next(p for p in store.list_credit_packages() if p["id"] == package["id"])
        self.assertEqual(listed["bonus_credits"], 20)
        self.assertEqual(listed["bonus_tickets"], 2)

    def test_purchase_grants_base_plus_bonus_credits(self):
        user = self._user("bonus-credits", coins=500)
        package = self._package(name="月度福利包", credits=100, bonus_credits=20)
        result = store.purchase_package(user["id"], package["id"], idem="buy-1")
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["bonus_credits"], 20)
        self.assertEqual(store.get_credits(user["id"]), 120)
        batch = store.list_credit_batches(user["id"])[0]
        self.assertEqual(batch["amount"], 120)
        self.assertEqual(batch["remaining"], 120)
        self.assertIn("赠送 20", batch["reason"])

    def test_purchase_does_not_grant_tickets(self):
        # 蒸馏券已取消：套餐即使配置了 bonus_tickets，也不再发放蒸馏券。
        user = self._user("bonus-tickets", coins=500)
        package = self._package(name="季度福利包", validity_days=90, bonus_tickets=3)
        result = store.purchase_package(user["id"], package["id"])
        self.assertEqual(result["bonus_tickets"], 0)
        self.assertEqual(store.get_distill_tickets(user["id"]), 0)
        self.assertEqual(store.list_distill_ticket_ledger(user["id"], limit=5), [])

    def test_purchase_bonus_idempotent(self):
        user = self._user("bonus-idem", coins=500)
        package = self._package(name="年度福利包", validity_days=365, bonus_credits=50, bonus_tickets=1)
        first = store.purchase_package(user["id"], package["id"], idem="same")
        replay = store.purchase_package(user["id"], package["id"], idem="same")
        self.assertFalse(first["duplicate"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(store.get_credits(user["id"]), 150)
        self.assertEqual(store.get_distill_tickets(user["id"]), 0)

    def test_admin_negative_bonus_rejected(self):
        with TestClient(app) as client:
            accounts.create_admin("bonus-admin", "Password123!")
            login = client.post(
                "/api/admin/auth/login", json={"username": "bonus-admin", "password": "Password123!"}
            )
            self.assertEqual(login.status_code, 200, login.text)
            response = client.post(
                "/api/admin/packages",
                json={
                    "name": "非法套餐",
                    "credits": 100,
                    "coins": 10,
                    "validity_days": 30,
                    "bonus_credits": -1,
                },
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("赠送", response.json()["detail"])

    def test_admin_bonus_roundtrip(self):
        with TestClient(app) as client:
            accounts.create_admin("bonus-admin-ok", "Password123!")
            client.post(
                "/api/admin/auth/login",
                json={"username": "bonus-admin-ok", "password": "Password123!"},
            )
            response = client.post(
                "/api/admin/packages",
                json={
                    "name": "年度尊享",
                    "credits": 5000,
                    "coins": 400,
                    "validity_days": 365,
                    "bonus_credits": 300,
                    "bonus_tickets": 2,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            package = response.json()["package"]
            self.assertEqual(package["bonus_credits"], 300)
            self.assertEqual(package["bonus_tickets"], 2)
            self.assertEqual(package["validity_label"], "年度")


if __name__ == "__main__":
    unittest.main()
