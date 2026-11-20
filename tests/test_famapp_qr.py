import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import zxingcpp
from PIL import Image
from pymongo.errors import DuplicateKeyError


os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("FAMAPP_UPI_ID", "payments@example")
os.environ.setdefault("FAMAPP_PAYEE_NAME", "Test Payee")

import handlers.payment as payment  # noqa: E402
import database  # noqa: E402
from handlers.payment import (  # noqa: E402
    _build_famapp_upi_uri,
    _generate_famapp_qr_bytes,
)


class FakeFamAppImap:
    raw_email = b""

    def __init__(self, *_args):
        self.selected_mailbox = None

    def login(self, *_args):
        return "OK", [b"logged in"]

    def select(self, mailbox):
        self.selected_mailbox = mailbox
        return "OK", [b""]

    def search(self, *_args):
        return "OK", [b"1"]

    def fetch(self, *_args):
        return "OK", [(b"1 (RFC822 {0})", self.raw_email), b")"]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


def _received_email(
    amount: str,
    purpose: str | None = None,
    transaction_id: str = "TXN123456789",
    timestamp: datetime | None = None,
    extra_text: str = "",
) -> bytes:
    message = EmailMessage()
    message["From"] = "no-reply@famapp.in"
    message["Subject"] = f"You received ₹{amount} in your FamX account"
    message["Date"] = format_datetime(timestamp or datetime.now(timezone.utc))
    body = f"You have successfully received\n₹{amount}\nfrom ASHISH\n\n"
    if purpose:
        body += f"Purpose :\n{purpose}\n"
    if transaction_id:
        body += f"Transaction ID: {transaction_id}\n"
    body += extra_text
    message.set_content(body)
    return bytes(message)


class FamAppQrTests(unittest.TestCase):
    def test_qr_decodes_to_the_exact_order_specific_upi_uri(self):
        amount = "129.50"
        purpose = "FAP20260901ABC123"
        expected_uri = _build_famapp_upi_uri(amount, purpose)

        qr_bytes = _generate_famapp_qr_bytes(amount, purpose)
        with Image.open(BytesIO(qr_bytes)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.width, image.height)
            self.assertEqual(image.mode, "1")
            decoded = zxingcpp.read_barcode(image, is_pure=True)

        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.text, expected_uri)

    def test_upi_uri_rejects_amounts_that_would_be_rounded(self):
        with self.assertRaises(ValueError):
            _build_famapp_upi_uri("129.501", "FAP20260901ABC123")

    def test_verifier_accepts_real_famapp_received_email(self):
        order_id = "ORD-TEST-20260829"
        amount = "1.00"
        purpose = "FAP20260901GAH9Y8"
        order = {
            "final_price": amount,
            "plan_price": amount,
            "payment_purpose": purpose,
            "payment_provider": "famapp",
            "payment_status": "created",
            "user_id": 7,
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            "upi_uri": _build_famapp_upi_uri(amount, purpose),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }

        with (
            patch.object(payment, "get_order", new=AsyncMock(return_value=order)) as get_order,
            patch.object(payment, "imaplib") as imaplib,
            patch.object(payment, "IMAP_USERNAME", "test@example.com"),
            patch.object(payment, "IMAP_APP_PASSWORD", "test-password"),
            patch.object(payment, "claim_famapp_payment", new=AsyncMock(return_value=True)) as claim,
            self.assertLogs(payment.logger, level="INFO") as logs,
        ):
            imaplib.IMAP4_SSL.side_effect = FakeFamAppImap
            FakeFamAppImap.raw_email = _received_email("1.00", purpose)
            result = asyncio.run(payment._verify_payment_famapp(order_id, amount))

        self.assertEqual(result, "success")
        claim.assert_awaited_once_with(order_id, 7, "TXN123456789", "amount")
        get_order.assert_awaited_once_with(order_id)
        messages = "\n".join(logs.output)
        self.assertIn("candidate_email_count=1", messages)
        self.assertIn(f"payment_qr_upi_purpose={purpose}", messages)
        self.assertIn("masked_sender=n***@famapp.in", messages)
        self.assertNotIn("no-reply@famapp.in", messages)
        self.assertIn("received_amount=1.00", messages)
        self.assertIn(f"extracted_purpose={purpose}", messages)
        self.assertIn("amount_match=True", messages)
        self.assertIn("purpose_match=True", messages)
        self.assertIn("final_match=True", messages)

    def test_new_famapp_order_reserves_unique_qr_amount_and_displays_base_price(self):
        bot = AsyncMock()
        bot.send_photo.return_value = SimpleNamespace(message_id=11)
        bot.send_message.return_value = SimpleNamespace(message_id=12)
        create = AsyncMock(
            side_effect=[
                payment.FamAppAmountReservationError("already reserved"),
                None,
            ]
        )
        plan = {
            "name": "Gold",
            "price": "149",
            "validity": "30 days",
            "access_link": "https://example.com/access",
        }

        async def create_screen():
            with (
                patch.object(payment, "create_order", new=create),
                patch.object(payment, "get_setting", new=AsyncMock(return_value=None)),
                patch.object(payment, "update_order_messages", new=AsyncMock()),
                patch.object(payment, "set_pending_reminder", new=AsyncMock()),
            ):
                return await payment._send_payment_screen(
                    bot=bot,
                    chat_id=7,
                    user_id=7,
                    plan=plan,
                    plan_id=2,
                    final_price_str="149",
                    price_section="💰 <b>Original Price:</b> ₹149",
                    discount_pct=0,
                )

        order_id = asyncio.run(create_screen())
        first = create.await_args_list[0].kwargs
        reserved = create.await_args_list[1].kwargs
        self.assertEqual(order_id, reserved["order_id"])
        self.assertEqual(reserved["plan_price"], "149")
        self.assertEqual(reserved["famapp_base_amount"], "149.00")
        adjustment = payment.Decimal(reserved["famapp_adjustment"])
        self.assertGreaterEqual(adjustment, payment.Decimal("0.01"))
        self.assertLess(adjustment, payment.Decimal("0.50"))
        self.assertNotEqual(first["famapp_qr_amount"], reserved["famapp_qr_amount"])
        self.assertEqual(
            payment.Decimal(reserved["famapp_qr_amount"]),
            payment.Decimal("149.00") + adjustment,
        )
        with Image.open(BytesIO(reserved["qr_image"])) as image:
            decoded = zxingcpp.read_barcode(image, is_pure=True)
        self.assertIsNotNone(decoded)
        uri_amount = payment.urllib.parse.parse_qs(
            payment.urllib.parse.urlsplit(decoded.text).query
        )["am"][0]
        self.assertEqual(uri_amount, reserved["famapp_qr_amount"])
        message_text = bot.send_message.await_args.args[1]
        self.assertIn("Plan:</b> Gold — ₹149", message_text)
        self.assertIn(f"Pay ₹{reserved['famapp_qr_amount']}", message_text)
        self.assertIn("Pay the amount pre-filled by the QR without editing it", message_text)

    def _verify_email(
        self,
        amount: str,
        email_amount: str,
        purpose: str | None = None,
        claim=True,
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
        timestamp: datetime | None = None,
        transaction_id: str = "TXN123456789",
        extra_text: str = "",
    ):
        order_id = "ORD-DECIMAL-VERIFY"
        now = datetime.now(timezone.utc)
        order = {
            "final_price": amount,
            "famapp_qr_amount": amount,
            "plan_price": "149",
            "payment_purpose": "FAP20261120ABC123",
            "payment_provider": "famapp",
            "payment_status": "created",
            "user_id": 7,
            "created_at": created_at or now - timedelta(minutes=2),
            "expires_at": (expires_at or now + timedelta(minutes=10)).isoformat(),
        }
        claim_mock = AsyncMock(return_value=claim)
        with (
            patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
            patch.object(payment, "imaplib") as imaplib,
            patch.object(payment, "IMAP_USERNAME", "test@example.com"),
            patch.object(payment, "IMAP_APP_PASSWORD", "test-password"),
            patch.object(payment, "claim_famapp_payment", new=claim_mock),
        ):
            imaplib.IMAP4_SSL.side_effect = FakeFamAppImap
            FakeFamAppImap.raw_email = _received_email(
                email_amount,
                purpose,
                transaction_id=transaction_id,
                timestamp=timestamp,
                extra_text=extra_text,
            )
            result = asyncio.run(payment._verify_payment_famapp(order_id, amount, expected_user_id=7))
        return result, claim_mock

    def test_exact_decimal_email_without_purpose_uses_unique_amount_fallback(self):
        result, claim = self._verify_email("149.23", "149.23")
        self.assertEqual(result, "success")
        claim.assert_awaited_once_with(
            "ORD-DECIMAL-VERIFY",
            7,
            "TXN123456789",
            "amount",
        )

    def test_exact_final_qr_amount_matches_without_purpose_or_decimal_fallback(self):
        with self.assertLogs(payment.logger, level="INFO") as logs:
            result, claim = self._verify_email("149.26", "149.26")

        self.assertEqual(result, "success")
        claim.assert_awaited_once_with(
            "ORD-DECIMAL-VERIFY",
            7,
            "TXN123456789",
            "amount",
        )
        messages = "\n".join(logs.output)
        self.assertIn("expected_amount=149.26", messages)
        self.assertIn("received_amount=149.26", messages)
        self.assertIn("amount_match=True", messages)
        self.assertIn("purpose_match=False", messages)
        self.assertIn("final_match=True", messages)
        self.assertNotIn("decimal_amount_match=", messages)

    def test_exact_107_email_matches_stored_final_qr_amount(self):
        with self.assertLogs(payment.logger, level="INFO") as logs:
            result, claim = self._verify_email("1.07", "1.07")

        self.assertEqual(result, "success")
        claim.assert_awaited_once_with(
            "ORD-DECIMAL-VERIFY",
            7,
            "TXN123456789",
            "amount",
        )
        messages = "\n".join(logs.output)
        self.assertIn("stored_original_plan_amount=149", messages)
        self.assertIn("stored_decimal_adjustment=<none>", messages)
        self.assertIn("stored_final_qr_amount=1.07", messages)
        self.assertIn("received_amount=1.07", messages)
        self.assertIn("exact_comparison_result=True", messages)
        self.assertIn("amount_match=True", messages)
        self.assertNotIn("decimal_amount_match=", messages)

    def test_decimal_fallback_does_not_require_a_usable_payment_purpose(self):
        result, claim = self._verify_email("149.23", "149.23", purpose="not-a-valid-purpose")
        self.assertEqual(result, "success")
        claim.assert_awaited_once_with(
            "ORD-DECIMAL-VERIFY",
            7,
            "TXN123456789",
            "amount",
        )

    def test_wrong_or_rounded_decimal_email_does_not_verify(self):
        with self.assertLogs(payment.logger, level="INFO") as logs:
            wrong_result, wrong_claim = self._verify_email("149.23", "149.00")
            rounded_result, rounded_claim = self._verify_email("149.20", "149.2")
        self.assertEqual(wrong_result, "pending")
        self.assertEqual(rounded_result, "pending")
        wrong_claim.assert_not_awaited()
        rounded_claim.assert_not_awaited()
        messages = "\n".join(logs.output)
        self.assertIn("email_amount_does_not_match_final_qr_amount", messages)
        self.assertIn("email_amount_precision_not_two", messages)

    def test_duplicate_transaction_claim_is_rejected(self):
        result, claim = self._verify_email("149.23", "149.23", claim=False)
        self.assertEqual(result, "pending")
        claim.assert_awaited_once()

    def test_duplicate_transaction_id_is_rejected_by_global_unique_constraint(self):
        async def claim_duplicate():
            with patch.object(
                database._orders,
                "find_one_and_update",
                new=AsyncMock(side_effect=DuplicateKeyError("duplicate transaction id")),
            ):
                return await database.claim_famapp_payment(
                    "ORD-OTHER",
                    7,
                    "TXN123456789",
                    "amount",
                )

        self.assertFalse(asyncio.run(claim_duplicate()))

    def test_missing_transaction_and_ambiguous_amount_stay_pending(self):
        missing_tx, missing_tx_claim = self._verify_email(
            "149.23",
            "149.23",
            transaction_id="",
        )
        ambiguous, ambiguous_claim = self._verify_email(
            "149.23",
            "149.23",
            extra_text="Updated balance: ₹500.00\n",
        )
        self.assertEqual(missing_tx, "pending")
        self.assertEqual(ambiguous, "pending")
        missing_tx_claim.assert_not_awaited()
        ambiguous_claim.assert_not_awaited()

    def test_expired_orders_and_out_of_window_emails_do_not_claim_transactions(self):
        now = datetime.now(timezone.utc)
        expired, expired_claim = self._verify_email(
            "149.23",
            "149.23",
            expires_at=now - timedelta(seconds=1),
        )
        out_of_window, outside_claim = self._verify_email(
            "149.23",
            "149.23",
            created_at=now - timedelta(minutes=1),
            timestamp=now - timedelta(minutes=3),
        )
        self.assertEqual(expired, "expired")
        self.assertEqual(out_of_window, "pending")
        expired_claim.assert_not_awaited()
        outside_claim.assert_not_awaited()

    def test_manual_screenshot_submission_reaches_review(self):
        user = type("User", (), {"id": 7, "username": "buyer"})()
        message = type("Message", (), {})()
        message.from_user = user
        message.photo = [type("Photo", (), {"file_id": "proof-file"})()]
        message.answer = AsyncMock()
        bot = AsyncMock()
        order = {
            "order_id": "ORD-MANUAL-1",
            "user_id": user.id,
            "plan_name": "Gold",
            "plan_price": "199",
            "final_price": "199",
            "payment_status": "created",
        }

        async def submit_proof():
            payment._waiting_proof[user.id] = order["order_id"]
            with (
                patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
                patch.object(payment, "mark_manual_proof_submitted", new=AsyncMock(return_value=True)) as submit_status,
                patch.object(payment, "LOG_CHANNEL_ID", 999),
            ):
                bot.send_photo = AsyncMock()
                await payment.handle_proof_photo(message, bot)
            return submit_status

        submit_status = asyncio.run(submit_proof())
        submit_status.assert_awaited_once_with(order["order_id"], user.id)
        bot.send_photo.assert_awaited_once()
        message.answer.assert_awaited_once()

    def test_payment_status_message_is_reused_on_repeated_update(self):
        call = SimpleNamespace(
            message=SimpleNamespace(
                chat=SimpleNamespace(id=7),
                answer=AsyncMock(return_value=SimpleNamespace(message_id=101)),
            )
        )
        bot = AsyncMock()
        info = {}

        async def update_status_message():
            with patch.object(payment, "update_order_status_message", new=AsyncMock()) as save_status:
                first_id = await payment._edit_or_create_status_message(
                    call,
                    bot,
                    7,
                    "ORD-STATUS-1",
                    info,
                    "first",
                )
                second_id = await payment._edit_or_create_status_message(
                    call,
                    bot,
                    7,
                    "ORD-STATUS-1",
                    info,
                    "second",
                )
            return first_id, second_id, save_status

        first_id, second_id, save_status = asyncio.run(update_status_message())
        self.assertEqual(first_id, 101)
        self.assertEqual(second_id, 101)
        call.message.answer.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once_with(
            chat_id=7,
            message_id=101,
            text="second",
            reply_markup=None,
        )
        save_status.assert_awaited_once_with("ORD-STATUS-1", 7, 101)

    def test_generated_purpose_flows_through_qr_and_verification(self):
        amount = "1.00"
        purpose = payment._generate_famapp_purpose()
        self.assertRegex(purpose, r"^FAP\d{8}[A-Z0-9]{6}$")
        self.assertNotIn("-", purpose)

        upi_uri = _build_famapp_upi_uri(amount, purpose)
        qr_bytes = _generate_famapp_qr_bytes(amount, purpose)
        with Image.open(BytesIO(qr_bytes)) as image:
            decoded = zxingcpp.read_barcode(image, is_pure=True)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.text, upi_uri)
        self.assertEqual(
            payment.urllib.parse.parse_qs(
                payment.urllib.parse.urlsplit(decoded.text).query
            )["tn"][0],
            purpose,
        )

        email_bytes = _received_email("1.00", purpose)
        order = {
            "final_price": amount,
            "plan_price": amount,
            "payment_purpose": purpose,
            "payment_provider": "famapp",
            "payment_status": "created",
            "user_id": 7,
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            "upi_uri": upi_uri,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }

        with (
            patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
            patch.object(payment, "imaplib") as imaplib,
            patch.object(payment, "IMAP_USERNAME", "test@example.com"),
            patch.object(payment, "IMAP_APP_PASSWORD", "test-password"),
            patch.object(FakeFamAppImap, "raw_email", email_bytes),
            patch.object(payment, "claim_famapp_payment", new=AsyncMock(return_value=True)),
            self.assertLogs(payment.logger, level="INFO") as logs,
        ):
            imaplib.IMAP4_SSL.side_effect = FakeFamAppImap
            result = asyncio.run(payment._verify_payment_famapp("ORD-GENERATED", amount))

        self.assertEqual(result, "success")
        messages = "\n".join(logs.output)
        self.assertIn(f"expected_payment_purpose={purpose}", messages)
        self.assertIn(f"extracted_purpose={purpose}", messages)
        self.assertIn("amount_match=True", messages)
        self.assertIn("purpose_match=True", messages)
        self.assertIn("final_match=True", messages)

    def test_parser_extracts_purpose_from_html_multipart_famapp_email(self):
        message = EmailMessage()
        message["From"] = "no-reply@famapp.in"
        message["Subject"] = "You received ₹1.0 in your FamX account"
        message["Date"] = "Sat, 29 Aug 2026 12:00:00 +0000"
        message.set_content(
            "You have successfully received\n"
            "₹1.0\n"
            "from ASHISH\n\n"
            "Purpose :\n"
            "FAP20260901GAH9Y8\n"
        )
        message.add_alternative(
            """
            <html><body>
              <p>Example reference: FAP20260829UNRELATED</p>
              <div><span>Purpose</span> :<br>
                <span>FAP20260901GAH9Y8</span>
              </div>
            </body></html>
            """,
            subtype="html",
        )

        parsed = payment._parse_famapp_email(
            bytes(message),
            "html-multipart-1",
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["amount"], payment.Decimal("1.00"))
        self.assertEqual(parsed["purpose"], "FAP20260901GAH9Y8")


if __name__ == "__main__":
    unittest.main()