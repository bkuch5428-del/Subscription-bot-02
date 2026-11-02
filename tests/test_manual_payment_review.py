import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

import database  # noqa: E402


class ManualPaymentReviewTests(unittest.TestCase):
    def test_manual_proof_submission_sets_pending_timestamp(self):
        submitted = {"_id": "ORD-MANUAL", "payment_status": "pending", "payment_provider": "manual"}
        orders = AsyncMock()
        orders.find_one_and_update = AsyncMock(return_value=submitted)

        async def submit():
            with patch.object(database, "_orders", orders):
                return await database.mark_manual_proof_submitted("ORD-MANUAL", 7)

        self.assertTrue(asyncio.run(submit()))
        update = orders.find_one_and_update.await_args.args[1]["$set"]
        self.assertEqual(update["payment_status"], "pending")
        self.assertIsInstance(update["manual_proof_submitted_at"], datetime)
        query = orders.find_one_and_update.await_args.args[0]
        self.assertEqual(query["payment_provider"], "manual")
        self.assertEqual(query["user_id"], 7)

    def test_expiry_query_excludes_pending_manual_but_not_other_providers(self):
        class EmptyCursor:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        orders = AsyncMock()
        orders.find = MagicMock(return_value=EmptyCursor())

        async def expire():
            with patch.object(database, "_orders", orders):
                return await database.expire_due_orders(datetime.now(timezone.utc) + timedelta(minutes=10))

        self.assertEqual(asyncio.run(expire()), [])
        query = orders.find.call_args.args[0]
        self.assertIn("$or", query)
        self.assertEqual(query["payment_status"], {"$in": ["created", "pending"]})

    def test_manual_pending_is_approveable_after_expiry(self):
        order = {
            "_id": "ORD-MANUAL-LATE",
            "user_id": 7,
            "plan_name": "Gold",
            "plan_validity": "30 days",
            "access_link": "https://example.com/access",
            "payment_status": "pending",
            "payment_provider": "manual",
            "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "referral_discount_used": 0,
        }
        orders = AsyncMock()
        orders.find_one = AsyncMock(return_value=order)
        orders.find_one_and_update = AsyncMock(return_value=order)

        async def approve():
            with (
                patch.object(database, "_orders", orders),
                patch.object(database, "cancel_reminder", AsyncMock()),
            ):
                return await database.approve_order(
                    order["_id"],
                    expected_user_id=7,
                    expected_payment_provider="manual",
                )

        result = asyncio.run(approve())
        self.assertIsNotNone(result)
        approve_query = orders.find_one_and_update.await_args.args[0]
        self.assertEqual(approve_query["payment_provider"], "manual")
        self.assertIn("payment_provider", str(approve_query["$or"]))

    def test_automatic_expiry_still_requires_unexpired_rules(self):
        order = {
            "_id": "ORD-FAMAPP",
            "user_id": 7,
            "plan_name": "Gold",
            "plan_validity": "30 days",
            "access_link": "",
            "payment_status": "pending",
            "payment_provider": "famapp",
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            "referral_discount_used": 0,
        }
        orders = AsyncMock()
        orders.find_one = AsyncMock(return_value=order)
        orders.find_one_and_update = AsyncMock(return_value=None)

        async def approve():
            with (
                patch.object(database, "_orders", orders),
                patch.object(database, "cancel_reminder", AsyncMock()),
            ):
                return await database.approve_order(order["_id"], expected_user_id=7)

        self.assertIsNone(asyncio.run(approve()))

    def test_concurrent_approval_only_one_call_updates(self):
        order = {
            "_id": "ORD-MANUAL-CONCURRENT",
            "user_id": 7,
            "plan_name": "Gold",
            "plan_validity": "30 days",
            "access_link": "",
            "payment_status": "pending",
            "payment_provider": "manual",
            "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
            "referral_discount_used": 0,
        }
        orders = AsyncMock()
        orders.find_one = AsyncMock(return_value=order)
        orders.find_one_and_update = AsyncMock(side_effect=[order, None])

        async def approve_both():
            with (
                patch.object(database, "_orders", orders),
                patch.object(database, "cancel_reminder", AsyncMock()),
            ):
                return await asyncio.gather(
                    database.approve_order(order["_id"], expected_user_id=7),
                    database.approve_order(order["_id"], expected_user_id=7),
                )

        results = asyncio.run(approve_both())
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_legacy_pending_manual_order_without_timestamp_is_reviewable(self):
        order = {
            "_id": "ORD-LEGACY-MANUAL",
            "user_id": 7,
            "plan_name": "Gold",
            "plan_validity": "30 days",
            "payment_status": "pending",
            "payment_provider": "manual",
            "expires_at": datetime.now(timezone.utc) - timedelta(days=2),
        }
        orders = AsyncMock()
        orders.find_one = AsyncMock(return_value=order)
        orders.find_one_and_update = AsyncMock(return_value=order)

        async def approve():
            with (
                patch.object(database, "_orders", orders),
                patch.object(database, "cancel_reminder", AsyncMock()),
            ):
                return await database.approve_order(order["_id"], expected_user_id=7)

        self.assertIsNotNone(asyncio.run(approve()))


if __name__ == "__main__":
    unittest.main()
