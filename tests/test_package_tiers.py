import os
import tempfile
import unittest

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""

from ex_persona import store  # noqa: E402


class PackageTiersTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def test_tiers_board_active(self):
        packages = store.list_credit_packages(active_only=True)
        names = {item["name"] for item in packages}
        for name, credits, coins, _badge, _sort, days, _bc, bonus_tickets in store.PACKAGE_TIERS:
            item = next((p for p in packages if p["name"] == name), None)
            self.assertIsNotNone(item, name)
            self.assertEqual(item["credits"], credits, name)
            self.assertEqual(item["coins"], coins, name)
            self.assertEqual(item["validity_days"], days, name)
            self.assertEqual(item["bonus_tickets"], bonus_tickets, name)
            self.assertEqual(item["price_cents"], coins * 10, name)
        self.assertNotIn("标准包", names)
        self.assertNotIn("尊享包", names)

    def test_tiers_margin_about_60_percent(self):
        cost_per_turn = 0.008
        for name, credits, coins, _badge, _sort, _days, _bc, _bt in store.PACKAGE_TIERS:
            rounds = credits / 20
            cost = rounds * cost_per_turn
            revenue = coins / 10
            margin = 1 - cost / revenue
            self.assertAlmostEqual(margin, 0.6, places=3, msg=name)

    def test_retired_entry_package_deactivated(self):
        store.upsert_credit_package(None, "体验包", 1000, 100, "", 1, True, coins=10, validity_days=30)
        with store.connect() as conn:
            store._meta_set(conn, "packages_retired_v1", "0")
        store._initialized = False
        store.init_db()
        retired = store.list_credit_packages(active_only=False)
        entry = next(p for p in retired if p["name"] == "体验包")
        self.assertEqual(entry["active"], 0)

    def test_admin_edit_survives_reinit(self):
        item = next(p for p in store.list_credit_packages() if p["name"] == "轻享月卡")
        store.upsert_credit_package(
            item["id"], "轻享月卡", 1234, 0, "", 11, True, coins=5, validity_days=30
        )
        store._initialized = False
        store.init_db()
        refreshed = next(p for p in store.list_credit_packages() if p["name"] == "轻享月卡")
        self.assertEqual(refreshed["credits"], 1234)
        self.assertEqual(refreshed["coins"], 5)
        store.upsert_credit_package(
            item["id"], "轻享月卡", 4900, 490, "", 11, True, coins=49, validity_days=30
        )


if __name__ == "__main__":
    unittest.main()
