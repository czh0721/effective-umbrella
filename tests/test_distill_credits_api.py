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


class DistillCreditApiTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_credits_payload_exposes_credit_cost(self):
        with TestClient(app) as client:
            self._register(client, "distill-credit-api")
            data = client.get("/api/credits").json()
        self.assertEqual(data["distill_credit_cost"], 100)
        self.assertNotIn("distill_tickets", data)

    def test_purchase_ticket_endpoint_gone(self):
        with TestClient(app) as client:
            self._register(client, "distill-credit-gone")
            response = client.post("/api/distill-tickets/purchase", json={"quantity": 1})
        self.assertEqual(response.status_code, 410, response.text)
        self.assertIn("积分", response.json()["detail"])

    def test_no_ticket_copy_in_frontend(self):
        web_root = Path(__file__).resolve().parents[1] / "web"
        for name in ("admin.html", "settings.html", "create_distill.html", "agent.html"):
            self.assertNotIn("蒸馏券", (web_root / name).read_text(encoding="utf-8"), name)
        offenders = [
            str(path.relative_to(web_root))
            for path in sorted(web_root.rglob("*.html"))
            if "蒸馏券" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_registration_grants_single_credit_payload(self):
        with TestClient(app) as client:
            user = self._register(client, "register-single-gift")
        reasons = [item["reason"] for item in store.list_credit_ledger(user["id"], limit=10)]
        self.assertIn("新用户注册赠送", reasons)
        self.assertFalse(any("蒸馏" in reason for reason in reasons), reasons)
        self.assertEqual(store.get_credits(user["id"]), 100)


if __name__ == "__main__":
    unittest.main()
