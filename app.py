import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from google import genai
from werkzeug.security import check_password_hash, generate_password_hash


load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    DATABASE=os.getenv(
        "DATABASE_PATH", os.path.join(app.instance_path, "aranya.sqlite3")
    ),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None

FREE_MESSAGE_LIMIT = 10
CREDIT_PACK_MESSAGES = 10
CREDIT_PACK_PRICE_PAISE = 9900
MAX_MESSAGE_LENGTH = 4000
RAZORPAY_API_URL = "https://api.razorpay.com/v1"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database():
    path = app.config["DATABASE"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                free_messages_used INTEGER NOT NULL DEFAULT 0,
                paid_credits INTEGER NOT NULL DEFAULT 0,
                profile_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT 'A new conversation',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_conversation_id_idx
                ON messages(conversation_id, id);
            CREATE TABLE IF NOT EXISTS payment_orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount_paise INTEGER NOT NULL,
                credits INTEGER NOT NULL,
                payment_id TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL
            );
            """
        )


@app.before_request
def protect_state_changes():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    expected = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        if request.is_json:
            return jsonify({"error": "Invalid or missing security token."}), 400
        abort(400, description="Invalid or missing security token.")
    return None


@app.context_processor
def add_csrf_token():
    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    return {"csrf_token": csrf_token}


def current_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    with database() as db:
        return db.execute(
            "SELECT id, name, email, free_messages_used, paid_credits, profile_summary "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("user_id") is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Please sign in to continue."}), 401
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def safe_redirect_target(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        try:
            parsed = urlsplit(target)
        except ValueError:
            return url_for("chat_page")
        if not parsed.netloc and not parsed.scheme:
            return target
    return url_for("chat_page")


def create_system_prompt():
    return """You are Aranya, an AI companion offering supportive conversation,
    coping strategies, and general emotional guidance. You are not a therapist,
    doctor, or emergency service.

    Be warm, empathetic, non-judgmental, and ask open questions when helpful.
    Do not diagnose, claim certainty about a user's mental health, or recommend
    medication. Encourage professional help when appropriate. If someone may be
    in immediate danger, encourage contacting local emergency services or a
    trusted person now. In India, TELE-MANAS is available at 14416 or
    1800-891-4416."""


def check_for_crisis(message):
    crisis_keywords = (
        "suicide",
        "kill myself",
        "want to die",
        "end my life",
        "harm myself",
        "self-harm",
        "cutting myself",
        "hurt myself",
    )
    normalized = message.casefold()
    return any(keyword in normalized for keyword in crisis_keywords)


def get_crisis_response():
    return """I'm really sorry you're facing this. If you may act on thoughts of
harming yourself, please contact local emergency services or go to the nearest
emergency department now, and if possible tell someone you trust to stay with
you. In India, you can call TELE-MANAS at 14416 or 1800-891-4416."""


def generate_response(conversation_id, user_id, message):
    if check_for_crisis(message):
        return get_crisis_response()
    if client is None:
        raise RuntimeError("The Gemini service is not configured.")

    with database() as db:
        history = db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT 6",
            (conversation_id,),
        ).fetchall()
        user = db.execute(
            "SELECT profile_summary FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    history = list(reversed(history))
    context = "\n".join(
        f"{'User' if row['role'] == 'user' else 'Assistant'}: {row['content']}"
        for row in history
    )
    profile_context = user["profile_summary"] if user else ""
    prompt = (
        f"{create_system_prompt()}\n\n"
        f"User-approved support profile (optional): "
        f"{profile_context or '(No profile summary saved.)'}\n\n"
        f"Recent conversation:\n{context or '(No previous messages.)'}\n\n"
        f"User: {message}\nAssistant:"
    )
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite", contents=prompt
    )
    if not response.text:
        raise RuntimeError("The Gemini service returned an empty response.")
    return response.text.strip()


def refund_message_allowance(user_id, allowance_type):
    with database() as db:
        if allowance_type == "free":
            db.execute(
                "UPDATE users SET free_messages_used = MAX(free_messages_used - 1, 0) "
                "WHERE id = ?",
                (user_id,),
            )
        else:
            db.execute(
                "UPDATE users SET paid_credits = paid_credits + 1 WHERE id = ?",
                (user_id,),
            )


@app.route("/")
def home():
    return render_template("landing.html", user=current_user())


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id") is not None:
        return redirect(url_for("chat_page"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not name or len(name) > 100 or not email or len(email) > 254:
            return render_template(
                "register.html", error="Please enter a valid name and email."
            ), 400
        if len(password) < 8 or len(password) > 128:
            return render_template(
                "register.html",
                error="Your password must be between 8 and 128 characters.",
            ), 400
        if password != confirm_password:
            return render_template(
                "register.html", error="The passwords do not match."
            ), 400
        try:
            with database() as db:
                cursor = db.execute(
                    "INSERT INTO users (name, email, password_hash, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (name, email, generate_password_hash(password), utc_now()),
                )
                user_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            return render_template(
                "register.html", error="An account with that email already exists."
            ), 409
        session.clear()
        session["user_id"] = user_id
        return redirect(url_for("chat_page"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id") is not None:
        return redirect(url_for("chat_page"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        if len(password) > 128:
            return render_template(
                "login.html", error="Email or password is incorrect."
            ), 401
        with database() as db:
            user = db.execute(
                "SELECT id, password_hash FROM users WHERE email = ?", (email,)
            ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            return render_template(
                "login.html", error="Email or password is incorrect."
            ), 401
        session.clear()
        session["user_id"] = user["id"]
        return redirect(safe_redirect_target(request.args.get("next")))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have been signed out.")
    return redirect(url_for("home"))


@app.route("/chat")
@login_required
def chat_page():
    user_id = session["user_id"]
    conversation_id = request.args.get("conversation_id")
    with database() as db:
        if conversation_id:
            conversation = db.execute(
                "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
            if conversation is None:
                abort(404)
        else:
            conversation_id = str(uuid.uuid4())
            now = utc_now()
            db.execute(
                "INSERT INTO conversations (id, user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, user_id, now, now),
            )
        user = db.execute(
            "SELECT name, free_messages_used, paid_credits FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return render_template(
        "index.html",
        user=user,
        conversation_id=conversation_id,
        free_messages_remaining=max(
            FREE_MESSAGE_LIMIT - user["free_messages_used"], 0
        ),
        paid_credits=user["paid_credits"],
        payment_enabled=bool(
            os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET")
        ),
        razorpay_key_id=os.getenv("RAZORPAY_KEY_ID", ""),
        credit_pack_messages=CREDIT_PACK_MESSAGES,
        credit_pack_price_rupees=CREDIT_PACK_PRICE_PAISE // 100,
    )


@app.route("/history")
@login_required
def history_page():
    with database() as db:
        conversations = db.execute(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   COUNT(m.id) AS message_count
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.user_id = ?
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            """,
            (session["user_id"],),
        ).fetchall()
    return render_template("history.html", conversations=conversations)


@app.route("/profile")
@login_required
def profile_page():
    user = current_user()
    return render_template("profile.html", user=user)


@app.route("/profile", methods=["POST"])
@login_required
def save_profile():
    summary = request.form.get("profile_summary", "").strip()
    if len(summary) > 2000:
        flash("Keep your profile summary under 2,000 characters.", "error")
        return redirect(url_for("profile_page"))
    with database() as db:
        db.execute(
            "UPDATE users SET profile_summary = ? WHERE id = ?",
            (summary, session["user_id"]),
        )
    flash("Your profile has been updated.")
    return redirect(url_for("profile_page"))


@app.route("/api/history")
@login_required
def chat_history():
    conversation_id = request.args.get("conversation_id", "")
    with database() as db:
        owns_conversation = db.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, session["user_id"]),
        ).fetchone()
        if owns_conversation is None:
            return jsonify({"error": "Conversation not found."}), 404
        rows = db.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE conversation_id = ? AND user_id = ? ORDER BY id",
            (conversation_id, session["user_id"]),
        ).fetchall()
    return jsonify(
        {
            "history": [
                {
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }
    )


@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Send a JSON request."}), 400
    message = data.get("message")
    conversation_id = data.get("conversation_id")
    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "Please enter a message."}), 400
    message = message.strip()
    if len(message) > MAX_MESSAGE_LENGTH:
        return jsonify(
            {"error": f"Messages must be {MAX_MESSAGE_LENGTH} characters or fewer."}
        ), 400
    if not isinstance(conversation_id, str):
        return jsonify({"error": "Conversation not found."}), 404

    user_id = session["user_id"]
    allowance_type = None
    try:
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            conversation = db.execute(
                "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
            if conversation is None:
                return jsonify({"error": "Conversation not found."}), 404
            user = db.execute(
                "SELECT free_messages_used, paid_credits FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user["free_messages_used"] < FREE_MESSAGE_LIMIT:
                db.execute(
                    "UPDATE users SET free_messages_used = free_messages_used + 1 "
                    "WHERE id = ?",
                    (user_id,),
                )
                allowance_type = "free"
                free_messages_remaining = max(
                    FREE_MESSAGE_LIMIT - user["free_messages_used"] - 1, 0
                )
                paid_credits = user["paid_credits"]
            elif user["paid_credits"] > 0:
                db.execute(
                    "UPDATE users SET paid_credits = paid_credits - 1 WHERE id = ?",
                    (user_id,),
                )
                allowance_type = "paid"
                free_messages_remaining = 0
                paid_credits = user["paid_credits"] - 1
            else:
                return jsonify(
                    {
                        "error": "You’ve used your 10 free messages. Buy a credit pack to keep chatting.",
                        "code": "limit_reached",
                    }
                ), 402

        try:
            response_text = generate_response(conversation_id, user_id, message)
        except Exception:
            logger.exception("Failed to generate a chat response")
            refund_message_allowance(user_id, allowance_type)
            return jsonify(
                {"error": "I couldn’t generate a reply just now. Please try again."}
            ), 502

        now = utc_now()
        with database() as db:
            db.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content, created_at) "
                "VALUES (?, ?, 'user', ?, ?)",
                (conversation_id, user_id, message, now),
            )
            db.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content, created_at) "
                "VALUES (?, ?, 'assistant', ?, ?)",
                (conversation_id, user_id, response_text, now),
            )
            db.execute(
                """
                UPDATE conversations
                SET title = CASE
                    WHEN title = 'A new conversation' THEN ?
                    ELSE title
                END, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (message[:60], now, conversation_id, user_id),
            )
        return jsonify(
            {
                "response": response_text,
                "free_messages_remaining": free_messages_remaining,
                "paid_credits": paid_credits,
            }
        )
    except Exception:
        if allowance_type:
            refund_message_allowance(user_id, allowance_type)
        logger.exception("Failed to persist a chat exchange")
        return jsonify({"error": "Your message could not be saved. Please retry."}), 500


@app.route("/api/profile/draft", methods=["POST"])
@login_required
def profile_summary_draft():
    if client is None:
        return jsonify({"error": "Profile summaries are not configured right now."}), 503
    with database() as db:
        messages = db.execute(
            """
            SELECT m.role, m.content
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE c.user_id = ? AND m.role = 'user'
            ORDER BY m.id DESC
            LIMIT 80
            """,
            (session["user_id"],),
        ).fetchall()
    if not messages:
        return jsonify({"error": "Chat a little first to create a summary draft."}), 400
    transcript = "\n".join(
        f"{'User' if row['role'] == 'user' else 'Assistant'}: {row['content']}"
        for row in reversed(messages)
    )
    prompt = (
        "Create a short, compassionate first-person profile draft for the user "
        "based only on themes the user explicitly shared in these conversations. "
        "Do not diagnose, infer sensitive traits, or state uncertain assumptions as facts. "
        "Mention recurring concerns, what they say helps, and areas they may want support "
        "with. Use plain language and no more than 150 words. This is only a draft for "
        "the user to review and edit; do not address the user.\n\n"
        f"Conversations:\n{transcript}"
    )
    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite", contents=prompt
        )
        draft = (response.text or "").strip()
        if not draft:
            raise RuntimeError("The Gemini service returned an empty profile draft.")
        return jsonify({"draft": draft[:2000]})
    except Exception:
        logger.exception("Failed to generate a profile summary draft")
        return jsonify(
            {"error": "I couldn’t prepare a profile draft. Please try again."}
        ), 502


@app.route("/api/credits/order", methods=["POST"])
@login_required
def create_credit_order():
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return jsonify({"error": "Credit purchases are not configured yet."}), 503
    receipt = f"aranya_{session['user_id']}_{uuid.uuid4().hex[:16]}"
    try:
        response = requests.post(
            f"{RAZORPAY_API_URL}/orders",
            auth=(key_id, key_secret),
            json={
                "amount": CREDIT_PACK_PRICE_PAISE,
                "currency": "INR",
                "receipt": receipt,
                "notes": {
                    "user_id": str(session["user_id"]),
                    "credits": str(CREDIT_PACK_MESSAGES),
                },
            },
            timeout=15,
        )
        response.raise_for_status()
        order = response.json()
        order_id = order.get("id")
        if (
            not order_id
            or order.get("amount") != CREDIT_PACK_PRICE_PAISE
            or order.get("currency") != "INR"
        ):
            raise ValueError("Razorpay returned an invalid order.")
        with database() as db:
            db.execute(
                "INSERT INTO payment_orders "
                "(order_id, user_id, amount_paise, credits, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    order_id,
                    session["user_id"],
                    CREDIT_PACK_PRICE_PAISE,
                    CREDIT_PACK_MESSAGES,
                    utc_now(),
                ),
            )
        return jsonify(
            {
                "order_id": order_id,
                "amount": CREDIT_PACK_PRICE_PAISE,
                "currency": "INR",
                "key_id": key_id,
                "credits": CREDIT_PACK_MESSAGES,
            }
        )
    except (requests.RequestException, ValueError, sqlite3.Error):
        logger.exception("Unable to create a Razorpay order")
        return jsonify(
            {"error": "Could not start checkout. Please try again later."}
        ), 502


@app.route("/api/credits/verify", methods=["POST"])
@login_required
def verify_credit_payment():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Send a JSON request."}), 400
    order_id = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature = data.get("razorpay_signature")
    if not all(isinstance(value, str) and value for value in (order_id, payment_id, signature)):
        return jsonify({"error": "Payment details are incomplete."}), 400

    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return jsonify({"error": "Payment verification is not configured."}), 503
    expected_signature = hmac.new(
        key_secret.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        return jsonify({"error": "Payment verification failed."}), 400

    try:
        with database() as db:
            order = db.execute(
                "SELECT * FROM payment_orders WHERE order_id = ? AND user_id = ?",
                (order_id, session["user_id"]),
            ).fetchone()
        if order is None:
            return jsonify({"error": "Payment order not found."}), 404
        if order["status"] == "paid":
            return jsonify({"success": True, "credits_added": 0})

        payment_response = requests.get(
            f"{RAZORPAY_API_URL}/payments/{payment_id}",
            auth=(key_id, key_secret),
            timeout=15,
        )
        payment_response.raise_for_status()
        payment = payment_response.json()
        if (
            payment.get("order_id") != order_id
            or payment.get("amount") != order["amount_paise"]
            or payment.get("currency") != "INR"
            or payment.get("status") not in {"authorized", "captured"}
        ):
            return jsonify({"error": "Payment has not been authorized."}), 400
        if payment.get("status") == "authorized":
            payment_response = requests.post(
                f"{RAZORPAY_API_URL}/payments/{payment_id}/capture",
                auth=(key_id, key_secret),
                json={"amount": order["amount_paise"], "currency": "INR"},
                timeout=15,
            )
            payment_response.raise_for_status()
            payment = payment_response.json()
            if payment.get("status") != "captured":
                return jsonify({"error": "Payment could not be captured."}), 400

        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            latest_order = db.execute(
                "SELECT status, credits FROM payment_orders "
                "WHERE order_id = ? AND user_id = ?",
                (order_id, session["user_id"]),
            ).fetchone()
            if latest_order is None:
                return jsonify({"error": "Payment order not found."}), 404
            if latest_order["status"] == "paid":
                return jsonify({"success": True, "credits_added": 0})
            db.execute(
                "UPDATE payment_orders SET status = 'paid', payment_id = ? "
                "WHERE order_id = ? AND status = 'created'",
                (payment_id, order_id),
            )
            if db.total_changes == 0:
                return jsonify({"error": "Payment has already been processed."}), 409
            db.execute(
                "UPDATE users SET paid_credits = paid_credits + ? WHERE id = ?",
                (latest_order["credits"], session["user_id"]),
            )
        return jsonify(
            {"success": True, "credits_added": order["credits"]}
        )
    except (requests.RequestException, ValueError, sqlite3.Error):
        logger.exception("Unable to verify a Razorpay payment")
        return jsonify(
            {"error": "Payment could not be verified. Contact support before retrying."}
        ), 502


@app.route("/index")
def legacy_chat_url():
    return redirect(url_for("chat_page"))


def test_gemini_api():
    if client is None:
        logger.error("GEMINI_API_KEY is not configured.")
        return False
    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents="Hello, can you give me a short response to test if you're working?",
        )
        logger.info("Gemini API test successful: %s", bool(response.text))
        return bool(response.text)
    except Exception:
        logger.exception("Gemini API test failed")
        return False


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
