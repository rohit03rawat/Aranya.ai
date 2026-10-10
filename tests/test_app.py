import hashlib
import hmac
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import Mock, patch
from types import SimpleNamespace

import app as aranya


class AranyaAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        aranya.app.config.update(
            TESTING=True,
            DATABASE=os.path.join(self.temp_dir.name, "test.sqlite3"),
        )
        aranya.init_db()
        self.client = aranya.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf_token(self, response):
        match = re.search(
            r'<meta name="csrf-token" content="([^"]+)"', response.get_data(as_text=True)
        )
        if match:
            return match.group(1)
        match = re.search(
            r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def register(self, client, email):
        form = client.get("/register")
        response = client.post(
            "/register",
            data={
                "csrf_token": self.csrf_token(form),
                "name": "Test Person",
                "email": email,
                "password": "correct-horse-battery",
                "confirm_password": "correct-horse-battery",
            },
        )
        self.assertEqual(response.status_code, 302)
        return client.get("/chat")

    def conversation_and_csrf(self, client):
        with client.session_transaction() as user_session:
            user_id = user_session["user_id"]
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection as db:
                conversation_id = db.execute(
                    "SELECT id FROM conversations WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()[0]
        page = client.get(f"/chat?conversation_id={conversation_id}")
        return conversation_id, self.csrf_token(page)

    def test_registration_hashes_password_and_requires_login_for_chat(self):
        response = self.register(self.client, "User@example.com")
        self.assertEqual(response.status_code, 200)
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection as db:
                email, password_hash = db.execute(
                    "SELECT email, password_hash FROM users"
                ).fetchone()
        self.assertEqual(email, "user@example.com")
        self.assertNotEqual(password_hash, "correct-horse-battery")
        self.assertTrue(
            aranya.check_password_hash(password_hash, "correct-horse-battery")
        )

        anonymous = aranya.app.test_client()
        self.assertEqual(anonymous.get("/chat").status_code, 302)
        self.assertEqual(anonymous.post("/api/chat", json={}).status_code, 400)

    def test_login_and_logout_manage_the_signed_in_session(self):
        self.register(self.client, "signin@example.com")
        profile = self.client.get("/profile")
        logout = self.client.post(
            "/logout", data={"csrf_token": self.csrf_token(profile)}
        )
        self.assertEqual(logout.status_code, 302)
        self.assertEqual(self.client.get("/chat").status_code, 302)

        form = self.client.get("/login")
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(form),
                "email": "signin@example.com",
                "password": "correct-horse-battery",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/chat").status_code, 200)

    def test_landing_and_history_pages_render(self):
        landing = self.client.get("/")
        self.assertEqual(landing.status_code, 200)
        self.assertIn("A little space to talk.", landing.get_data(as_text=True))
        self.assertIn("data-theme-toggle", landing.get_data(as_text=True))

        login = self.client.get("/login")
        register = self.client.get("/register")
        self.assertIn("data-theme-toggle", login.get_data(as_text=True))
        self.assertIn("data-theme-toggle", register.get_data(as_text=True))

        self.register(self.client, "history-page@example.com")
        chat = self.client.get("/chat")
        history = self.client.get("/history")
        profile = self.client.get("/profile")
        for page in (chat, history, profile):
            self.assertIn("data-theme-toggle", page.get_data(as_text=True))
        self.assertEqual(history.status_code, 200)
        self.assertIn("Your conversations.", history.get_data(as_text=True))

        theme_css = self.client.get("/static/css/theme.css")
        theme_js = self.client.get("/static/js/theme.js")
        self.assertEqual(theme_css.status_code, 200)
        self.assertIn(':root[data-theme="dark"]', theme_css.get_data(as_text=True))
        self.assertEqual(theme_js.status_code, 200)

    def test_chat_quota_and_history_are_account_scoped(self):
        self.register(self.client, "one@example.com")
        conversation_id, csrf = self.conversation_and_csrf(self.client)
        payload = {"conversation_id": conversation_id, "message": "How can I feel calmer?"}

        with patch("app.generate_response", return_value="Try taking one slow breath."):
            for _ in range(aranya.FREE_MESSAGE_LIMIT):
                response = self.client.post(
                    "/api/chat",
                    json=payload,
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status_code, 200)
            limited = self.client.post(
                "/api/chat",
                json=payload,
                headers={"X-CSRF-Token": csrf},
            )

        self.assertEqual(limited.status_code, 402)
        self.assertEqual(limited.json["code"], "limit_reached")
        history = self.client.get(
            f"/api/history?conversation_id={conversation_id}"
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json["history"]), aranya.FREE_MESSAGE_LIMIT * 2)

        another_client = aranya.app.test_client()
        self.register(another_client, "two@example.com")
        other_history = another_client.get(
            f"/api/history?conversation_id={conversation_id}"
        )
        self.assertEqual(other_history.status_code, 404)

    def test_mutating_api_requires_csrf_token(self):
        self.register(self.client, "csrf@example.com")
        conversation_id, _ = self.conversation_and_csrf(self.client)
        response = self.client.post(
            "/api/chat",
            json={"conversation_id": conversation_id, "message": "Hello"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "Invalid or missing security token.")

    def test_paid_credits_extend_chat_after_free_allowance(self):
        self.register(self.client, "credits@example.com")
        conversation_id, csrf = self.conversation_and_csrf(self.client)
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection:
                connection.execute(
                    "UPDATE users SET free_messages_used = ?, paid_credits = ?",
                    (aranya.FREE_MESSAGE_LIMIT, 2),
                )
        payload = {"conversation_id": conversation_id, "message": "One more thought"}

        with patch("app.generate_response", return_value="I'm listening."):
            first = self.client.post(
                "/api/chat", json=payload, headers={"X-CSRF-Token": csrf}
            )
            second = self.client.post(
                "/api/chat", json=payload, headers={"X-CSRF-Token": csrf}
            )
            exhausted = self.client.post(
                "/api/chat", json=payload, headers={"X-CSRF-Token": csrf}
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["paid_credits"], 1)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json["paid_credits"], 0)
        self.assertEqual(exhausted.status_code, 402)

    def test_failed_generation_refunds_the_reserved_free_message(self):
        self.register(self.client, "retry@example.com")
        conversation_id, csrf = self.conversation_and_csrf(self.client)
        with patch("app.client", None):
            response = self.client.post(
                "/api/chat",
                json={"conversation_id": conversation_id, "message": "Hello"},
                headers={"X-CSRF-Token": csrf},
            )
        self.assertEqual(response.status_code, 502)
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection as db:
                used = db.execute(
                    "SELECT free_messages_used FROM users"
                ).fetchone()[0]
                saved = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(used, 0)
        self.assertEqual(saved, 0)

    def test_profile_draft_is_not_saved_without_user_approval(self):
        self.register(self.client, "profile@example.com")
        conversation_id, csrf = self.conversation_and_csrf(self.client)
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO messages "
                    "(conversation_id, user_id, role, content, created_at) "
                    "VALUES (?, 1, 'user', 'Work has been stressful.', '2026-01-01')",
                    (conversation_id,),
                )
        mocked_client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(return_value=SimpleNamespace(text="I feel stressed at work."))
            )
        )
        with patch("app.client", mocked_client):
            draft = self.client.post(
                "/api/profile/draft", headers={"X-CSRF-Token": csrf}
            )
        self.assertEqual(draft.status_code, 200)
        self.assertEqual(draft.json["draft"], "I feel stressed at work.")
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection as db:
                summary = db.execute(
                    "SELECT profile_summary FROM users"
                ).fetchone()[0]
        self.assertEqual(summary, "")

        profile_page = self.client.get("/profile")
        self.assertEqual(profile_page.status_code, 200)
        saved = self.client.post(
            "/profile",
            data={"csrf_token": csrf, "profile_summary": draft.json["draft"]},
        )
        self.assertEqual(saved.status_code, 302)
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection as db:
                summary = db.execute(
                    "SELECT profile_summary FROM users"
                ).fetchone()[0]
        self.assertEqual(summary, draft.json["draft"])

    def test_credit_order_uses_server_configured_price(self):
        self.register(self.client, "neworder@example.com")
        form = self.client.get("/profile")
        order_id = "order_test_created"
        mocked_response = SimpleNamespace(
            raise_for_status=Mock(),
            json=Mock(
                return_value={
                    "id": order_id,
                    "amount": aranya.CREDIT_PACK_PRICE_PAISE,
                    "currency": "INR",
                }
            ),
        )
        with patch.dict(
            os.environ,
            {"RAZORPAY_KEY_ID": "test_key", "RAZORPAY_KEY_SECRET": "test_secret"},
        ), patch("app.requests.post", return_value=mocked_response) as create_order:
            response = self.client.post(
                "/api/credits/order",
                headers={"X-CSRF-Token": self.csrf_token(form)},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["amount"], 9900)
        self.assertEqual(response.json["credits"], 10)
        self.assertNotIn("test_secret", response.get_data(as_text=True))
        self.assertEqual(create_order.call_args.kwargs["json"]["amount"], 9900)
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection as db:
                amount, credits, status = db.execute(
                    "SELECT amount_paise, credits, status FROM payment_orders "
                    "WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
        self.assertEqual((amount, credits, status), (9900, 10, "created"))

    def test_captured_payment_adds_credits_exactly_once(self):
        self.register(self.client, "payment@example.com")
        with self.client.session_transaction() as user_session:
            user_id = user_session["user_id"]
        order_id = "order_test_123"
        payment_id = "pay_test_123"
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO payment_orders "
                    "(order_id, user_id, amount_paise, credits, created_at) "
                    "VALUES (?, ?, ?, ?, '2026-01-01')",
                    (order_id, user_id, aranya.CREDIT_PACK_PRICE_PAISE, 10),
                )
        secret = "test_secret"
        signature = hmac.new(
            secret.encode(),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        authorized = SimpleNamespace(
            raise_for_status=Mock(),
            json=Mock(
                return_value={
                    "order_id": order_id,
                    "amount": aranya.CREDIT_PACK_PRICE_PAISE,
                    "currency": "INR",
                    "status": "authorized",
                }
            ),
        )
        captured = SimpleNamespace(
            raise_for_status=Mock(),
            json=Mock(return_value={"status": "captured"}),
        )
        form = self.client.get("/profile")
        payload = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        }
        with patch.dict(
            os.environ,
            {"RAZORPAY_KEY_ID": "test_key", "RAZORPAY_KEY_SECRET": secret},
        ), patch("app.requests.get", return_value=authorized), patch(
            "app.requests.post", return_value=captured
        ) as capture_request:
            first = self.client.post(
                "/api/credits/verify",
                json=payload,
                headers={"X-CSRF-Token": self.csrf_token(form)},
            )
            replay = self.client.post(
                "/api/credits/verify",
                json=payload,
                headers={"X-CSRF-Token": self.csrf_token(form)},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["credits_added"], 10)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["credits_added"], 0)
        capture_request.assert_called_once()
        with closing(sqlite3.connect(aranya.app.config["DATABASE"])) as connection:
            with connection as db:
                balance, status = db.execute(
                    "SELECT u.paid_credits, o.status "
                    "FROM users u JOIN payment_orders o ON o.user_id = u.id "
                    "WHERE o.order_id = ?",
                    (order_id,),
                ).fetchone()
        self.assertEqual(balance, 10)
        self.assertEqual(status, "paid")


if __name__ == "__main__":
    unittest.main()
