from flask import Flask, request
import requests
import openai
import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Load credentials
CLICKSEND_USERNAME = os.getenv("CLICKSEND_USERNAME")
CLICKSEND_API_KEY = os.getenv("CLICKSEND_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Constants
WHITELIST_FILE = "whitelist.txt"
USAGE_FILE = "usage.json"
USAGE_LIMIT = 200
RESET_DAYS = 30

# Load whitelist
def load_whitelist():
    try:
        with open(WHITELIST_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        print("⚠️ 'whitelist.txt' not found.")
        return set()

WHITELIST = load_whitelist()

# Load and save usage
def load_usage():
    try:
        with open(USAGE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_usage(data):
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f, indent=2)

# Limit check
def can_send(sender):
    usage = load_usage()
    now = datetime.utcnow()
    record = usage.get(sender, {"count": 0, "last_reset": now.isoformat()})

    last_reset = datetime.fromisoformat(record["last_reset"])
    if now - last_reset > timedelta(days=RESET_DAYS):
        record["count"] = 0
        record["last_reset"] = now.isoformat()

    if record["count"] >= USAGE_LIMIT:
        return False

    record["count"] += 1
    usage[sender] = record
    save_usage(usage)
    return True

# Send SMS via ClickSend
def send_sms(to_number, message):
    url = "https://rest.clicksend.com/v3/sms/send"
    headers = {"Content-Type": "application/json"}
    payload = {
        "messages": [
            {
                "source": "python",
                "body": message[:1600],
                "to": to_number,
                "custom_string": "gpt_reply"
            }
        ]
    }
    response = requests.post(
        url,
        auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
        headers=headers,
        json=payload
    )
    return response.json()

# Ask ChatGPT for short reply
def ask_gpt(message):
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Answer concisely in 1–2 short sentences. Be clear and SMS-friendly."},
            {"role": "user", "content": message}
        ],
        max_tokens=50,
        temperature=0.7
    )
    reply = response.choices[0].message.content.strip()
    if len(reply) > 320:
        trimmed = reply[:320]
        if "." in trimmed:
            trimmed = trimmed[:trimmed.rfind(".")+1]
        reply = trimmed
    return reply

# Webhook route
@app.route("/sms", methods=["POST"])
def sms_webhook():
    print("🛰 HEADERS:", dict(request.headers))
    print("🛰 RAW BODY:", request.data.decode(errors='replace'))
    print("🧾 FORM DATA:", dict(request.form))

    sender = request.form.get("from")
    body = request.form.get("body")

    if not sender or not body:
        print("❌ Missing sender or body")
        return "Missing fields", 400

    if sender not in WHITELIST:
        print(f"🚫 Unauthorized number: {sender}")
        return "Number not authorized", 403

    if not can_send(sender):
        print(f"⏳ Limit reached for {sender}")
        return "Monthly message limit reached (200). Try again later.", 403

    print(f"📩 SMS from {sender}: {body}")

    try:
        reply = ask_gpt(body)
    except Exception as e:
        print("❌ GPT error:", e)
        reply = "Sorry, I had trouble generating a response. Try again later."

    sms_result = send_sms(sender, reply)
    print(f"📤 SMS Sent: {sms_result}")

    return "OK", 200

if __name__ == "__main__":
    app.run(debug=True, port=5000)
