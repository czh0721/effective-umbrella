import os
import tempfile
import unittest

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from ex_persona import store  # noqa: E402


class DistillTicketTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _user(self, username, coins=0):
        user = store.create_user(username, "h", "s")
        if coins:
            store.grant_coins(user["id"], coins)
        return user

    def test_new_user_has_no_tickets_by_default(self):
        user = self._user("ticket-default")
        self.assertEqual(store.get_distill_tickets(user["id"]), 0)

    def test_grant_and_deduct_tickets(self):
        user = self._user("ticket-grant")
        store.grant_distill_tickets(user["id"], 3, reason="管理员发放", actor="admin:x")
        self.assertEqual(store.get_distill_tickets(user["id"]), 3)
        store.grant_distill_tickets(user["id"], -1, reason="管理员扣减", actor="admin:x")
        self.assertEqual(store.get_distill_tickets(user["id"]), 2)

    def test_grant_ticket_below_zero_raises(self):
        user = self._user("ticket-negative")
        with self.assertRaises(store.InsufficientDistillTickets):
            store.grant_distill_tickets(user["id"], -1)

    def test_purchase_ticket_spends_coins(self):
        user = self._user("ticket-buy", coins=500)
        result = store.purchase_distill_ticket(user["id"], 2, idem="buy-1")
        self.assertFalse(result["duplicate"])
        self.assertEqual(store.get_distill_tickets(user["id"]), 2)
        self.assertEqual(store.get_coins(user["id"]), 500 - result["spent"])
        self.assertGreater(result["spent"], 0)

    def test_purchase_ticket_idempotent(self):
        user = self._user("ticket-buy-idem", coins=500)
        first = store.purchase_distill_ticket(user["id"], 1, idem="same")
        replay = store.purchase_distill_ticket(user["id"], 1, idem="same")
        self.assertFalse(first["duplicate"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(store.get_distill_tickets(user["id"]), 1)

    def test_purchase_ticket_without_coins_rolls_back(self):
        user = self._user("ticket-buy-poor", coins=0)
        with self.assertRaises(store.InsufficientCoins):
            store.purchase_distill_ticket(user["id"], 1)
        self.assertEqual(store.get_distill_tickets(user["id"]), 0)
        self.assertEqual(store.get_coins(user["id"]), 0)

    def test_reserve_requires_balance(self):
        user = self._user("ticket-reserve-none")
        with self.assertRaises(store.InsufficientDistillTickets):
            store.reserve_distill_ticket(user["id"], "task-x")
        self.assertEqual(store.get_distill_tickets(user["id"]), 0)

    def test_reserve_then_refund(self):
        user = self._user("ticket-refund")
        store.grant_distill_tickets(user["id"], 1)
        store.reserve_distill_ticket(user["id"], "task-1")
        self.assertEqual(store.get_distill_tickets(user["id"]), 0)
        store.refund_distill_ticket(user["id"], "task-1")
        self.assertEqual(store.get_distill_tickets(user["id"]), 1)

    def test_refund_is_idempotent(self):
        user = self._user("ticket-refund-idem")
        store.grant_distill_tickets(user["id"], 1)
        store.reserve_distill_ticket(user["id"], "task-2")
        first = store.refund_distill_ticket(user["id"], "task-2")
        second = store.refund_distill_ticket(user["id"], "task-2")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(store.get_distill_tickets(user["id"]), 1)

    def test_summary_counts_consumed(self):
        user = self._user("ticket-summary", coins=500)
        store.purchase_distill_ticket(user["id"], 2, idem="s1")
        store.reserve_distill_ticket(user["id"], "t-ok")
        store.reserve_distill_ticket(user["id"], "t-fail")
        store.refund_distill_ticket(user["id"], "t-fail")
        summary = store.distill_tickets_summary(user["id"])
        self.assertEqual(summary["balance"], 1)
        self.assertEqual(summary["purchased"], 2)
        self.assertEqual(summary["consumed"], 1)

    def test_reconcile_refunds_interrupted_task(self):
        user = self._user("ticket-reconcile")
        store.grant_distill_tickets(user["id"], 1)
        store.create_task("task-stale", user["id"], None, "distill")
        store.reserve_distill_ticket(user["id"], "task-stale")
        self.assertEqual(store.get_distill_tickets(user["id"]), 0)
        result = store.reconcile_distill_tickets()
        self.assertEqual(result["refunded"], 1)
        self.assertEqual(store.get_distill_tickets(user["id"]), 1)
        task = store.get_task("task-stale")
        self.assertEqual(task["status"], "error")
        self.assertEqual(task["error_kind"], "interrupted")


if __name__ == "__main__":
    unittest.main()
