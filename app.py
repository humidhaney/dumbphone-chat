from flask import Flask, request
import requests
import openai
import os
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from dotenv import load_dotenv
import urllib.parse
import re, calendar
from dataclasses import dataclass
from typing import Optional, Dict, Any
import datetime as dt

# Load env vars
load_dotenv()

app = Flask(__name__)

# === Config & API Keys ===
CLICKSEND_USERNAME = os.getenv("CLICKSEND_USERNAME")
CLICKSEND_API_KEY = os.getenv("CLICKSEND_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

openai.api_key = OPENAI_API_KEY

WHITELIST_FILE = "whitelist.txt"
USAGE_FILE = "usage.json"
USAGE_LIMIT = 200
RESET_DAYS = 30
DB_PATH = os.getenv("DB_PATH", "chat.db")

WELCOME_MSG = (
    "Welcome to the Dirty Coast chatbot powered by OpenAI. "
    "You can ask me to search the web, check business hours, get news, or find sunrise/sunset times. "
    "If at anytime you wish to unsubscribe, reply with STOP."
)

# === SQLite for message memory ===
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

# === Whitelist functions ===
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
        return True
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

# === Usage limit functions ===
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

# === ClickSend SMS ===
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

# === SerpAPI Search ===
def web_search(q, num=3):
    if not SERPAPI_API_KEY:
        return "Search unavailable (no SERPAPI_API_KEY set)."
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google",
        "q": q,
        "num": num,
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
        "gl": "us",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return f"Search error ({r.status_code})"
        data = r.json()
    except Exception as e:
        return f"Search error: {e}"

    org = (data.get("organic_results") or [])
    if not org:
        kg = data.get("knowledge_graph") or {}
        summary = kg.get("title") or kg.get("website") or ""
        if summary:
            return str(summary)[:320]
        return "No results found."

    top = org[0]
    title = top.get("title", "")
    link = top.get("link", "")
    snippet = top.get("snippet", "")
    return f"{title} — {snippet} ({link})".strip()[:320] or "No results found."

# === Sunrise/Sunset via Sunrise-Sunset.org ===
def sunrise_sunset_lookup(query: str):
    if not SERPAPI_API_KEY:
        return "Sunrise/sunset unavailable (no SERPAPI_API_KEY set)."

    day = "today"
    tl = query.lower()
    if "tomorrow" in tl: day = "tomorrow"
    elif any(dn.lower() in tl for dn in calendar.day_name):
        # optional: could parse a specific weekday; for now we stick to today/tomorrow
        pass

    cleaned = re.sub(r"\b(sunrise|sunset|in|tomorrow|today|what|time|is|the|for)\b", "", query, flags=re.I).strip()
    if not cleaned:
        return "Please specify a location, e.g., 'sunrise tomorrow in New York City'."

    # 1) geocode via SerpAPI Google Maps
    geo_url = "https://serpapi.com/search.json"
    params = {"engine": "google_maps", "q": cleaned, "api_key": SERPAPI_API_KEY}
    try:
        r = requests.get(geo_url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return f"Geocode error: {e}"

    loc = None
    if "place_results" in data and "gps_coordinates" in data["place_results"]:
        coords = data["place_results"]["gps_coordinates"]
        loc = (coords.get("latitude"), coords.get("longitude"))
    elif "local_results" in data and data["local_results"]:
        coords = data["local_results"][0].get("gps_coordinates", {})
        loc = (coords.get("latitude"), coords.get("longitude"))

    if not loc or None in loc:
        return "Could not determine location coordinates."

    lat, lng = loc

    # 2) Sunrise-Sunset API
    ss_url = "https://api.sunrise-sunset.org/json"
    ss_params = {"lat": lat, "lng": lng, "formatted": 0}
    if day == "tomorrow":
        ss_params["date"] = (datetime.utcnow() + timedelta(days=1)).date().isoformat()
    else:
        ss_params["date"] = datetime.utcnow().date().isoformat()

    try:
        r2 = requests.get(ss_url, params=ss_params, timeout=15)
        r2.raise_for_status()
        ss_data = r2.json()
    except Exception as e:
        return f"Sunrise API error: {e}"

    if ss_data.get("status") != "OK":
        return "Could not get sunrise/sunset times."

    results = ss_data.get("results", {})
    sunrise_utc = results.get("sunrise")
    sunset_utc = results.get("sunset")
    return f"Sunrise: {sunrise_utc} UTC | Sunset: {sunset_utc} UTC for {cleaned.title()} ({day})"

# ---------- small extractors ----------
def _extract_day(text: str) -> Optional[str]:
    t = text.lower()
    if "today" in t: return "today"
    if "tomorrow" in t: return "tomorrow"
    for name in calendar.day_name:
        if name.lower() in t or re.search(rf"\b{name[:3].lower()}\b", t):
            return name  # "Monday"
    return None

def _extract_city(text: str) -> Optional[str]:
    m = re.search(r"\bin\s+([A-Z][\w'’\-]*(?:\s+[A-Z][\w'’\-]*){0,4})", text)
    return m.group(1).strip() if m else None

def _extract_quoted_name(text: str) -> Optional[str]:
    m = re.search(r"[\"“”'’]([^\"“”'’]{2,})[\"“”'’]", text)
    return m.group(1).strip() if m else None

def _clean_topic(text: str) -> str:
    return re.sub(r"\b(latest|current|news|headlines|on|about|the)\b", " ", text, flags=re.I).strip()

def _extract_source(text: str) -> Optional[str]:
    m = re.search(r"\b(cnn|bbc|reuters|ap news|associated press|nytimes|new york times|fox news|wsj|washington post)\b", text, re.I)
    return m.group(1).lower() if m else None

def _extract_units(text: str):
    m = re.search(r"\bconvert\s+([\d\.]+)\s*([a-zA-Z]+)\s+to\s+([a-zA-Z]+)\b", text, re.I)
    if m: return float(m.group(1)), m.group(2).lower(), m.group(3).lower()
    return None

# ---------- intent results ----------
@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]

# ---------- individual detectors ----------
def detect_hours_intent(text: str) -> Optional[IntentResult]:
    t = text.strip()
    day = _extract_day(t)
    city = _extract_city(t)
    patterns = [
        r"what\s+time\s+does\s+(.+?)\s+(open|close)",
        r"when\s+is\s+(.+?)\s+open",
        r"hours\s+for\s+(.+)$",
        r"(.+?)\s+hours\b",
        r"\bcalled\s+([A-Z][\w&'’\-]*(?:\s+[A-Z][\w&'’\-]*)*)",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.I)
        if m:
            biz = (m.group(1) if m.lastindex else None) or _extract_quoted_name(t)
            if not biz:
                continue
            return IntentResult("hours", {"biz": biz.strip(), "city": city, "day": day})
    if ("open" in t.lower() or "hours" in t.lower()):
        q = _extract_quoted_name(t)
        if q:
            return IntentResult("hours", {"biz": q, "city": city, "day": day})
    return None

def detect_news_intent(text: str) -> Optional[IntentResult]:
    if re.search(r"(latest|current)\s+news", text, re.I) or "headlines" in text.lower():
        source = _extract_source(text)
        topic = _clean_topic(text)
        return IntentResult("news", {"topic": topic, "source": source})
    if re.search(r"\blatest\s+.+", text, re.I) and not re.search(r"\bweather|sunrise|sunset\b", text, re.I):
        source = _extract_source(text)
        topic = _clean_topic(text)
        return IntentResult("news", {"topic": topic, "source": source})
    return None

def detect_sun_intent(text: str) -> Optional[IntentResult]:
    t = text.lower()
    if "sunrise" in t or "sunset" in t:
        city = _extract_city(text)
        day = _extract_day(text) or "today"
        kind = "sunrise" if "sunrise" in t and "sunset" not in t else ("sunset" if "sunset" in t and "sunrise" not in t else "both")
        return IntentResult("sun", {"city": city, "day": day, "kind": kind})
    return None

def detect_weather_intent(text: str) -> Optional[IntentResult]:
    if re.search(r"\b(weather|temp|temperature|forecast)\b", text, re.I):
        city = _extract_city(text)
        day = _extract_day(text) or "today"
        return IntentResult("weather", {"city": city, "day": day})
    return None

def detect_contact_intent(text: str) -> Optional[IntentResult]:
    tl = text.lower()
    if any(k in tl for k in ["address for", "phone for", "website for", "email for", "contact for"]):
        m = re.search(r"\b(?:address|phone|website|email|contact)\s+for\s+(.+)$", text, re.I)
        target = m.group(1).strip() if m else text
        if "address" in tl: kind = "address"
        elif "phone" in tl: kind = "phone"
        elif "website" in tl: kind = "website"
        elif "email" in tl or "contact" in tl: kind = "contact"
        else: kind = "contact"
        return IntentResult("contact", {"kind": kind, "target": target})
    return None

def detect_time_in_city_intent(text: str) -> Optional[IntentResult]:
    if re.search(r"\btime\b", text, re.I) and _extract_city(text):
        return IntentResult("time_city", {"city": _extract_city(text)})
    return None

def detect_stock_intent(text: str) -> Optional[IntentResult]:
    m = re.search(r"\b(stock\s+price|price\s+of)\s+([A-Z]{1,5})\b", text, re.I)
    if m:
        return IntentResult("stock", {"ticker": m.group(2).upper()})
    m2 = re.search(r"\b([A-Z]{1,5})\s+stock\s+price\b", text)
    if m2:
        return IntentResult("stock", {"ticker": m2.group(1).upper()})
    return None

def detect_sports_intent(text: str) -> Optional[IntentResult]:
    if re.search(r"\b(score|scores|schedule|game|games|result|results)\b", text, re.I):
        m = re.search(r"\bfor\s+([A-Z][A-Za-z0-9\s\.\-]{2,})$", text)
        target = m.group(1).strip() if m else None
        return IntentResult("sports", {"target": target})
    return None

def detect_convert_intent(text: str) -> Optional[IntentResult]:
    conv = _extract_units(text)
    if conv:
        value, from_u, to_u = conv
        return IntentResult("convert", {"value": value, "from": from_u, "to": to_u})
    return None

def detect_math_intent(text: str) -> Optional[IntentResult]:
    if re.search(r"\b(calc|calculate|what\s+is)\s+[-+/*\d\.\s\(\)]+", text, re.I):
        expr = re.search(r"([-+/*\d\.\s\(\)]+)", text)
        return IntentResult("math", {"expr": expr.group(1).strip() if expr else None})
    return None

def detect_define_intent(text: str) -> Optional[IntentResult]:
    m = re.search(r"\b(define|definition\s+of)\s+([A-Za-z\-]+)\b", text, re.I)
    if m:
        return IntentResult("define", {"term": m.group(2)})
    return None

def detect_translate_intent(text: str) -> Optional[IntentResult]:
    m = re.search(r"\btranslate\s+(.+?)\s+to\s+([A-Za-z]+)\b", text, re.I)
    if m:
        return IntentResult("translate", {"text": m.group(1).strip(), "lang": m.group(2).lower()})
    return None

DET_ORDER = [
    detect_hours_intent,
    detect_news_intent,
    detect_sun_intent,
    detect_weather_intent,
    detect_contact_intent,
    detect_time_in_city_intent,
    detect_stock_intent,
    detect_sports_intent,
    detect_convert_intent,
    detect_math_intent,
    detect_define_intent,
    detect_translate_intent,
]

def detect_intent(text: str) -> Optional[IntentResult]:
    for fn in DET_ORDER:
        res = fn(text)
        if res:
            return res
    return None

# === GPT Chat ===
def ask_gpt(phone, user_msg):
    history = load_history(phone, limit=10)
    messages = [{"role": "system", "content": "You are a concise SMS assistant. Reply in 1–2 short sentences, plain language."}]
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

# === Routes ===
@app.route("/sms", methods=["POST"])
def sms_webhook():
    sender = request.form.get("from")
    body = (request.form.get("body") or "").strip()
    if not sender or not body:
        return "Missing fields", 400

    # STOP unsubscribe
    if body.upper() == "STOP":
        if remove_from_whitelist(sender):
            if sender in WHITELIST:
                WHITELIST.remove(sender)
            send_sms(sender, "You have been unsubscribed and will no longer receive messages.")
        return "OK", 200

    # Auto-add new number + welcome
    is_new = add_to_whitelist(sender)
    if is_new:
        WHITELIST.add(sender)
        send_sms(sender, WELCOME_MSG)

    if sender not in WHITELIST:
        return "Number not authorized", 403
    if not can_send(sender):
        return "Monthly message limit reached (200). Try again next month.", 403

    save_message(sender, "user", body)

    # --- Unified intent routing ---
    intent = detect_intent(body)
    if intent:
        t = intent.type
        e = intent.entities

        if t == "hours":
            parts = [e["biz"]]
            if e.get("city"): parts.append(e["city"])
            parts.append("hours")
            if e.get("day") == "tomorrow": parts.append("tomorrow")
            elif e.get("day") == "today": parts.append("today")
            reply = web_search(" ".join(parts)) or "No results."
            reply = reply[:300]
            save_message(sender, "assistant", reply); send_sms(sender, reply); return "OK", 200

        if t == "news":
            q = e["topic"] or body
            if e.get("source"): q = f"{e['source']} {q} headlines"
            else: q = f"{q} news"
            reply = web_search(q) or "No news found."
            reply = reply[:300]
            save_message(sender, "assistant", reply); send_sms(sender, reply); return "OK", 200
