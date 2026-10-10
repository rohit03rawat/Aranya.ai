# Aranya

Aranya is a Flask-based AI companion for supportive conversation and general emotional guidance. It is not a therapist, doctor, or emergency service.

## Features

- Public landing page, account registration, sign-in, and sign-out
- Site-wide light/dark mode with a saved preference across pages
- Password hashes and server-side session authentication
- SQLite-backed user accounts, conversations, and chat history
- Ten free user messages per account, then paid message credits
- Optional Razorpay checkout for a ₹99 pack of 10 messages
- Conversation history page and an optional profile summary
- Profile summary drafts are generated only on request and are not stored until the user reviews and saves them
- Crisis response guidance with India’s TELE-MANAS contact numbers

## Local setup

Use Python 3.10 or newer.

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```dotenv
GEMINI_API_KEY=your_gemini_api_key
FLASK_SECRET_KEY=replace_with_a_long_random_secret
DATABASE_PATH=instance/aranya.sqlite3

# Optional. Use Razorpay test keys while testing checkout.
RAZORPAY_KEY_ID=rzp_test_your_key_id
RAZORPAY_KEY_SECRET=your_razorpay_key_secret

# Set to 1 when the site is served over HTTPS.
COOKIE_SECURE=0
```

Generate a Flask secret locally instead of reusing a sample value:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Run the application:

```powershell
python app.py
```

Open http://127.0.0.1:5000. If Razorpay credentials are absent, checkout stays disabled; chat and account features still run. Use Razorpay test credentials until the full payment flow has been tested in the Razorpay dashboard.

## Deployment

Set `GEMINI_API_KEY` and a stable, randomly generated `FLASK_SECRET_KEY` in the hosting provider’s secret settings. For Razorpay checkout, configure the key ID and secret as server-side environment variables; never put the secret in browser code or commit it.

The default SQLite database is under `instance/`. SQLite is suitable for a small single-instance deployment, but the host must provide persistent storage and mount it at that path (or set `DATABASE_PATH` to a persistent volume). A temporary deployment filesystem will lose accounts and history when the instance is replaced. For multi-instance production, use a managed database instead.

Set `COOKIE_SECURE=1` when the application is served over HTTPS. Configure the hosting provider to run `python app.py` and expose its assigned `PORT`.

## Data and safety

Conversation content is stored in the configured SQLite database and sent to Google Gemini to generate replies. The optional profile draft also sends saved conversation excerpts to Gemini, but is only generated after the signed-in user clicks the draft button; it is not saved until the user submits it. Users can edit or clear the saved profile summary at any time. Chat history remains saved to the account.

The SQLite database is not encrypted at rest by this application. Protect the host and database backups, restrict database access, and publish an appropriate privacy policy before opening the service to real users. Do not use this app as a substitute for professional or emergency mental health care.

In India, TELE-MANAS is available at **14416** or **1800-891-4416**. If there is immediate danger, contact local emergency services.
