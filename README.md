# Aranya.ai

# 🧠 Therapy Chatbot using Gemini API

A supportive mental health chatbot built with Flask and Google's Gemini API (gemini-2.0-flash), designed to provide emotional support and coping strategies. Hosted using Render.

Live Demo - https://aranya-ai.onrender.com
---

## 💡 Features

- 🗣️ Empathetic conversation responses
- 🔁 Remembers message history
- 🚨 Detects crisis words and provides help
- 🤖 Uses Google's Gemini AI (Generative AI)
- 🌐 Hosted on Render (Free Tier)

---

## ⚙️ Tech Stack

- Python
- Flask
- Google Generative AI (Gemini)
- HTML
- Render

---

## 🚀 Setup Instructions

### 1. Clone the repository

```bash
git clone https://github.com/your-username/therapy-chatbot.git
cd therapy-chatbot

2. Create virtual environment (optional)

python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

3. Install dependencies

pip install -r requirements.txt

4. Add environment variable

Create a .env file in the root folder and add your Gemini API key:

GEMINI_API_KEY=your_gemini_api_key_here

5. Run the app locally

python app.py

Visit http://localhost:5000 in your browser.
🌍 Deployment on Render

    Push code to GitHub.

    Go to Render.com, click "New Web Service", and connect your GitHub repo.

    Set environment variable:

GEMINI_API_KEY = your_gemini_api_key

Set the Start Command:

    python app.py

    Note: Render free tier apps may sleep after inactivity. Use UptimeRobot to keep it active 24/7.

📁 File Structure

├── app.py
├── .env
├── requirements.txt
├── templates/
│   └── index.html
└── README.md

⚠️ Disclaimer

This chatbot is for educational use only and not a substitute for professional mental health support.
❤️ Credits

Made with Flask + Gemini for learning and mental wellness awareness 🌱
