from flask import Flask, request
import requests
import openai
import os
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment vars
load_dotenv()

app = Flask(__name__)

# Config
CLICKSEND_USERNAME = os.getenv("CLICKSEND_USERNAME")
CLICKSEND_API_KEY = os.getenv("CLICKSEND_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

WHITELIST_FILE = "whitelist.txt"
USAGE_FILE = "usage.json"
USAGE_LIMIT = 200
RESET_DAYS = 30
DB_PATH = os.getenv("DB_PATH", "chat.db")
WELCOME_MSG = (
    "Welcome to the Dirty Coast chatbot powered by OpenAI. "
    "If at anytime you wish to no longer receive texts from this number please respond with STOP "
    "and you will be removed from your subscription."
)

# === SQLite memory ===
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.commit()

def save_message(phone, role, content):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO messages (phone, role, content) VALUES (?, ?, ?)",
                  (phone, role, content))
        conn.commit()

def load_history(phone, limit=10):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT role, content
            FROM messages
            WHERE phone = ?
            ORDER BY id DESC
            LIMIT ?
        """, (phone, limit))
        rows = c.fetchall()
    return [{"role": r, "content": t} for (r, t) in reversed(rows)]

init_db()

# === Whitelist ===
def load_whitelist():
    try:
        with open(WHITELIST_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

def add_to_whitelist(phone):
    wl = load_whitelist()
    if phone not in wl:
        with open(WHITELIST_FILE, "a") as f:
            f.write(phone + "\n")
        return True  # New addition
    return False

def remove_from_whitelist(phone):
    wl = load_whitelist()
    if phone in wl:
        wl.remove(phone)
        with open(WHITELIST_FILE, "w") as f:
            for num in wl:
                f.write(num + "\n")
        return True
    return False

WHITELIST = load_whitelist()

# === Usage limits ===
def load_usage():
    try:
        with open(USAGE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_usage(data):
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f, indent=2)

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

# === ClickSend ===
def send_sms(to_number, message):
    url = "https://rest.clicksend.com/v3/sms/send"
    headers = {"Content-Type": "application/json"}
    payload = {"messages": [{
        "source": "python",
        "body": message[:1600],
        "to": to_number,
        "custom_string": "gpt_reply"
    }]}
    resp = requests.post(
        url,
        auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
        headers=headers,
        json=payload,
        timeout=15
    )
    return resp.json()

# === GPT ===
def ask_gpt(phone, user_msg):
    history = load_history(phone, limit=10)
    messages = [
        {"role": "system", "content": "You are a concise SMS assistant. Reply in 1–2 short sentences, plain language."}
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": user_msg})
    resp = openai.ChatCompletion.create(
        model="gpt-4",
        messages=messages,
        max_tokens=50,
        temperature=0.7
    )
    reply = resp.choices[0].message.content.strip()
    if len(reply) > 320:
        trimmed = reply[:320]
        if "." in trimmed:
            trimmed = trimmed[:trimmed.rfind(".")+1]
        reply = trimmed
    return reply

# === Webhook ===
@app.route("/sms", methods=["POST"])
def sms_webhook():
    sender = request.form.get("from")
    body = request.form.get("body", "").strip()

    if not sender or not body:
        return "Missing fields", 400

    # STOP command handling
    if body.upper() == "STOP":
        if remove_from_whitelist(sender):
            send_sms(sender, "You have been unsubscribed and will no longer receive messages.")
        return "OK", 200

    # Add to whitelist if new and send welcome
    is_new = add_to_whitelist(sender)
    if is_new:
        send_sms(sender, WELCOME_MSG)
        WHITELIST.add(sender)

    if sender not in WHITELIST:
        return "Number not authorized", 403

    if not can_send(sender):
        return "Monthly message limit reached (200). Try again next month.", 403

    # Save incoming message
    save_message(sender, "user", body)

    # GPT reply
    try:
        reply = ask_gpt(sender, body)
    except Exception as e:
        print("GPT error:", e)
        reply = "Sorry, I had trouble. Try again later."

    # Save and send reply
    save_message(sender, "assistant", reply)
    send_sms(sender, reply)

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
