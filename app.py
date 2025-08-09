from flask import Flask, request
import requests
import openai
import os
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
import re
import calendar
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import datetime as dt
from dotenv import load_dotenv
import urllib.parse

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
    "You can ask me to search the web, check business hours, get news, find sunrise/sunset times, "
    "get directions, check movie showtimes, find restaurants, check flight status, and much more. "
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
        # Add spam detection table
        c.execute("""
        CREATE TABLE IF NOT EXISTS spam_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL UNIQUE,
            is_spam BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

# === Enhanced Content Filtering ===
class ContentFilter:
    def __init__(self):
        self.spam_keywords = {
            'promotional': ['free', 'win', 'winner', 'prize', 'congratulations', 'click here', 
                           'limited time', 'act now', 'offer expires', 'cash prize'],
            'suspicious': ['bitcoin', 'crypto', 'investment opportunity', 'make money fast',
                          'work from home', 'guaranteed income', 'no experience needed'],
            'inappropriate': ['adult', 'dating', 'hookup', 'sexy', 'nude', '18+'],
            'phishing': ['verify account', 'suspended', 'click link', 'update payment',
                        'security alert', 'urgent action required']
        }
        
        self.offensive_patterns = [
            r'\b(f[*@#$%]?ck|sh[*@#$%]?t|damn|hell)\b',
            r'\b(stupid|idiot|moron|dumb[a@]ss)\b',
            # Add more patterns as needed
        ]
    
    def is_spam(self, text: str) -> tuple[bool, str]:
        """Check if text contains spam indicators"""
        text_lower = text.lower()
        
        for category, keywords in self.spam_keywords.items():
            for keyword in keywords:
                if keyword in text_lower:
                    return True, f"Spam detected: {category}"
        
        # Check for excessive caps
        if len(text) > 10 and sum(c.isupper() for c in text) / len(text) > 0.7:
            return True, "Excessive capitalization"
        
        # Check for excessive punctuation
        if text.count('!') > 3 or text.count('?') > 3:
            return True, "Excessive punctuation"
        
        return False, ""
    
    def is_offensive(self, text: str) -> tuple[bool, str]:
        """Check for offensive language"""
        text_lower = text.lower()
        
        for pattern in self.offensive_patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return True, "Offensive language detected"
        
        return False, ""
    
    def is_valid_query(self, text: str) -> tuple[bool, str]:
        """Check if query is valid and appropriate"""
        # Check minimum length
        if len(text.strip()) < 3:
            return False, "Query too short"
        
        # Check maximum length
        if len(text) > 500:
            return False, "Query too long"
        
        # Check for spam
        is_spam, spam_reason = self.is_spam(text)
        if is_spam:
            return False, spam_reason
        
        # Check for offensive content
        is_offensive, offensive_reason = self.is_offensive(text)
        if is_offensive:
            return False, offensive_reason
        
        return True, ""

content_filter = ContentFilter()

# === Rate Limiting Enhancement ===
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
    now = datetime.now(timezone.utc)
    
    # Get existing record or create new one
    record = usage.get(sender, {})
    
    # Ensure all required fields exist with defaults
    if "count" not in record:
        record["count"] = 0
    if "last_reset" not in record:
        record["last_reset"] = now.isoformat()
    if "hourly_count" not in record:
        record["hourly_count"] = 0
    if "last_hour" not in record:
        record["last_hour"] = now.replace(minute=0, second=0, microsecond=0).isoformat()
    
    try:
        last_reset = datetime.fromisoformat(record["last_reset"])
        last_hour = datetime.fromisoformat(record["last_hour"])
        # Ensure timezone awareness
        if last_reset.tzinfo is None:
            last_reset = last_reset.replace(tzinfo=timezone.utc)
        if last_hour.tzinfo is None:
            last_hour = last_hour.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        # Reset corrupted timestamps
        record["last_reset"] = now.isoformat()
        record["last_hour"] = now.replace(minute=0, second=0, microsecond=0).isoformat()
        last_reset = now
        last_hour = now.replace(minute=0, second=0, microsecond=0)
    
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    
    # Reset monthly count
    if now - last_reset > timedelta(days=RESET_DAYS):
        record["count"] = 0
        record["last_reset"] = now.isoformat()
    
    # Reset hourly count
    if current_hour > last_hour:
        record["hourly_count"] = 0
        record["last_hour"] = current_hour.isoformat()
    
    # Check limits
    if record["count"] >= USAGE_LIMIT:
        return False, "Monthly limit reached"
    
    if record["hourly_count"] >= 10:  # Max 10 messages per hour
        return False, "Hourly limit reached"
    
    record["count"] += 1
    record["hourly_count"] += 1
    usage[sender] = record
    save_usage(usage)
    return True, ""

# === Whitelist functions (unchanged) ===
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

# === Enhanced Search Function ===
def web_search(q, num=3, search_type="general"):
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
    
    # Customize search based on type
    if search_type == "news":
        params["tbm"] = "nws"
    elif search_type == "images":
        params["tbm"] = "isch"
    elif search_type == "local":
        params["engine"] = "google_maps"
    
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return f"Search error ({r.status_code})"
        data = r.json()
    except Exception as e:
        return f"Search error: {e}"

    if search_type == "news" and "news_results" in data:
        news = data["news_results"]
        if news:
            top = news[0]
            return f"{top.get('title', '')} — {top.get('snippet', '')} ({top.get('link', '')})".strip()[:320]
    
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

# === Enhanced Extractors ===
def _extract_day(text: str) -> Optional[str]:
    t = text.lower()
    if "today" in t: return "today"
    if "tomorrow" in t: return "tomorrow"
    if "yesterday" in t: return "yesterday"
    for name in calendar.day_name:
        if name.lower() in t or re.search(rf"\b{name[:3].lower()}\b", t):
            return name
    return None

def _extract_city(text: str) -> Optional[str]:
    # Enhanced city extraction
    patterns = [
        r"\bin\s+([A-Z][\w''\-]*(?:\s+[A-Z][\w''\-]*){0,4})",
        r"\bnear\s+([A-Z][\w''\-]*(?:\s+[A-Z][\w''\-]*){0,4})",
        r"\bat\s+([A-Z][\w''\-]*(?:\s+[A-Z][\w''\-]*){0,4})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return None

def _extract_time(text: str) -> Optional[str]:
    """Extract time from text (e.g., '2pm', '14:30', 'noon')"""
    time_patterns = [
        r'\b(\d{1,2}):(\d{2})\s*(am|pm)?\b',
        r'\b(\d{1,2})\s*(am|pm)\b',
        r'\b(noon|midnight)\b',
    ]
    for pattern in time_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None

def _extract_date(text: str) -> Optional[str]:
    """Extract date from text"""
    date_patterns = [
        r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b',
        r'\b(\d{1,2})-(\d{1,2})-(\d{2,4})\b',
        r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b',
    ]
    for pattern in date_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None

def _extract_price_range(text: str) -> Optional[tuple]:
    """Extract price range (e.g., '$10-20', 'under $50')"""
    m = re.search(r'\$(\d+)-(\d+)', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    
    m = re.search(r'under\s+\$(\d+)', text, re.IGNORECASE)
    if m:
        return 0, int(m.group(1))
    
    m = re.search(r'over\s+\$(\d+)', text, re.IGNORECASE)
    if m:
        return int(m.group(1)), 999999
    
    return None

# === Intent Results ===
@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]
    confidence: float = 1.0

# === Enhanced Intent Detectors ===
def detect_restaurant_intent(text: str) -> Optional[IntentResult]:
    """Detect restaurant/food related queries"""
    food_keywords = ['restaurant', 'food', 'eat', 'dining', 'menu', 'cuisine', 'pizza', 
                    'burger', 'coffee', 'lunch', 'dinner', 'breakfast']
    
    if any(keyword in text.lower() for keyword in food_keywords):
        city = _extract_city(text)
        price_range = _extract_price_range(text)
        
        # Extract cuisine type
        cuisine_types = ['italian', 'chinese', 'mexican', 'indian', 'thai', 'japanese', 
                        'french', 'american', 'mediterranean', 'vietnamese']
        cuisine = None
        for c_type in cuisine_types:
            if c_type in text.lower():
                cuisine = c_type
                break
        
        return IntentResult("restaurant", {
            "city": city,
            "cuisine": cuisine,
            "price_range": price_range,
            "query": text
        })
    return None

def detect_directions_intent(text: str) -> Optional[IntentResult]:
    """Detect directions/navigation queries"""
    direction_keywords = ['directions', 'how to get', 'navigate', 'route', 'drive to', 'walk to']
    
    if any(keyword in text.lower() for keyword in direction_keywords):
        # Extract from and to locations
        m = re.search(r'from\s+([^to]+)\s+to\s+(.+)', text, re.IGNORECASE)
        if m:
            return IntentResult("directions", {
                "from": m.group(1).strip(),
                "to": m.group(2).strip()
            })
        
        # Just destination
        m = re.search(r'(?:directions to|navigate to|route to)\s+(.+)', text, re.IGNORECASE)
        if m:
            return IntentResult("directions", {
                "to": m.group(1).strip(),
                "from": None
            })
    
    return None

def detect_movie_intent(text: str) -> Optional[IntentResult]:
    """Detect movie/entertainment queries"""
    movie_keywords = ['movie', 'film', 'cinema', 'theater', 'showtime', 'tickets']
    
    if any(keyword in text.lower() for keyword in movie_keywords):
        city = _extract_city(text)
        date = _extract_date(text) or _extract_day(text)
        
        # Extract movie title if quoted
        movie_title = None
        m = re.search(r'[\"""'']([^\"""'']+)[\"""'']', text)
        if m:
            movie_title = m.group(1)
        
        return IntentResult("movie", {
            "city": city,
            "date": date,
            "title": movie_title,
            "query": text
        })
    return None

def detect_flight_intent(text: str) -> Optional[IntentResult]:
    """Detect flight status queries"""
    flight_keywords = ['flight', 'airline', 'departure', 'arrival', 'gate', 'delay']
    
    if any(keyword in text.lower() for keyword in flight_keywords):
        # Extract flight number
        m = re.search(r'\b([A-Z]{2}\d{1,4})\b', text)
        flight_number = m.group(1) if m else None
        
        # Extract airline
        airlines = ['american', 'delta', 'united', 'southwest', 'jetblue', 'alaska']
        airline = None
        for a in airlines:
            if a in text.lower():
                airline = a
                break
        
        return IntentResult("flight", {
            "flight_number": flight_number,
            "airline": airline,
            "query": text
        })
    return None

def detect_event_intent(text: str) -> Optional[IntentResult]:
    """Detect event/activity queries"""
    event_keywords = ['event', 'concert', 'show', 'festival', 'party', 'meeting', 'conference']
    
    if any(keyword in text.lower() for keyword in event_keywords):
        city = _extract_city(text)
        date = _extract_date(text) or _extract_day(text)
        
        return IntentResult("event", {
            "city": city,
            "date": date,
            "query": text
        })
    return None

def detect_shopping_intent(text: str) -> Optional[IntentResult]:
    """Detect shopping queries"""
    shopping_keywords = ['buy', 'shop', 'store', 'purchase', 'price', 'sale', 'deal']
    
    if any(keyword in text.lower() for keyword in shopping_keywords):
        city = _extract_city(text)
        price_range = _extract_price_range(text)
        
        return IntentResult("shopping", {
            "city": city,
            "price_range": price_range,
            "query": text
        })
    return None

def detect_medical_intent(text: str) -> Optional[IntentResult]:
    """Detect medical/health queries (handle carefully)"""
    medical_keywords = ['doctor', 'hospital', 'pharmacy', 'clinic', 'emergency', 'urgent care']
    
    if any(keyword in text.lower() for keyword in medical_keywords):
        city = _extract_city(text)
        
        # Check for emergency
        is_emergency = any(word in text.lower() for word in ['emergency', 'urgent', '911', 'help'])
        
        return IntentResult("medical", {
            "city": city,
            "is_emergency": is_emergency,
            "query": text
        })
    return None

# === Keep existing detectors ===
def detect_hours_intent(text: str) -> Optional[IntentResult]:
    t = text.strip()
    day = _extract_day(t)
    city = _extract_city(t)
    patterns = [
        r"what\s+time\s+does\s+(.+?)\s+(open|close)",
        r"when\s+is\s+(.+?)\s+open",
        r"hours\s+for\s+(.+)$",
        r"(.+?)\s+hours\b",
        r"\bcalled\s+([A-Z][\w&''\-]*(?:\s+[A-Z][\w&''\-]*)*)",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.I)
        if m:
            biz = (m.group(1) if m.lastindex else None)
            if not biz:
                continue
            return IntentResult("hours", {"biz": biz.strip(), "city": city, "day": day})
    return None

def detect_news_intent(text: str) -> Optional[IntentResult]:
    if re.search(r"(latest|current)\s+news", text, re.I) or "headlines" in text.lower():
        topic = re.sub(r"\b(latest|current|news|headlines|on|about|the)\b", "", text, flags=re.I).strip()
        return IntentResult("news", {"topic": topic})
    return None

def detect_weather_intent(text: str) -> Optional[IntentResult]:
    if re.search(r"\b(weather|temp|temperature|forecast)\b", text, re.I):
        city = _extract_city(text)
        day = _extract_day(text) or "today"
        return IntentResult("weather", {"city": city, "day": day})
    return None

# === Updated Detector Order ===
DET_ORDER = [
    detect_medical_intent,      # High priority for safety
    detect_directions_intent,
    detect_restaurant_intent,
    detect_movie_intent,
    detect_flight_intent,
    detect_event_intent,
    detect_shopping_intent,
    detect_hours_intent,
    detect_news_intent,
    detect_weather_intent,
]

def detect_intent(text: str) -> Optional[IntentResult]:
    for fn in DET_ORDER:
        res = fn(text)
        if res:
            return res
    return None

# === Enhanced GPT Chat ===
def ask_gpt(phone, user_msg):
    history = load_history(phone, limit=10)
    messages = [
        {"role": "system", "content": """You are a helpful SMS assistant. Keep responses under 160 characters when possible. 
        Be concise but friendly. For medical emergencies, always advise calling 911. Don't provide medical diagnoses."""}
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": user_msg})
    
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=messages,
            max_tokens=80,
            temperature=0.7
        )
        reply = resp.choices[0].message.content.strip()
        if len(reply) > 320:
            reply = reply[:317] + "..."
        return reply
    except Exception as e:
        return "Sorry, I'm having trouble processing that right now."

# === Routes ===
@app.route("/sms", methods=["POST"])
def sms_webhook():
    sender = request.form.get("from")
    body = (request.form.get("body") or "").strip()
    
    if not sender or not body:
        return "Missing fields", 400

    # Content filtering
    is_valid, filter_reason = content_filter.is_valid_query(body)
    if not is_valid:
        return f"Message filtered: {filter_reason}", 400

    # STOP unsubscribe
    if body.upper() == "STOP":
        if remove_from_whitelist(sender):
            if sender in WHITELIST:
                WHITELIST.remove(sender)
            send_sms(sender, "You have been unsubscribed.")
        return "OK", 200

    # Auto-add new number + welcome
    is_new = add_to_whitelist(sender)
    if is_new:
        WHITELIST.add(sender)
        send_sms(sender, WELCOME_MSG)

    if sender not in WHITELIST:
        return "Number not authorized", 403

    # Enhanced rate limiting
    can_send_result, limit_reason = can_send(sender)
    if not can_send_result:
        send_sms(sender, f"Rate limit exceeded: {limit_reason}")
        return "Rate limited", 429

    save_message(sender, "user", body)

    # --- Enhanced intent routing ---
    intent = detect_intent(body)
    if intent:
        t = intent.type
        e = intent.entities

        if t == "medical":
            if e.get("is_emergency"):
                reply = "⚠️ For medical emergencies, call 911 immediately. For non-emergency medical help:"
            else:
                reply = "For medical assistance:"
            
            search_query = e["query"]
            if e.get("city"):
                search_query += f" in {e['city']}"
            
            search_result = web_search(search_query, search_type="local")
            reply += f" {search_result}"
            
        elif t == "restaurant":
            search_parts = ["restaurant"]
            if e.get("cuisine"): search_parts.append(e["cuisine"])
            if e.get("city"): search_parts.append(f"in {e['city']}")
            if e.get("price_range"):
                min_p, max_p = e["price_range"]
                search_parts.append(f"${min_p}-{max_p}")
            
            reply = web_search(" ".join(search_parts), search_type="local")

        elif t == "directions":
            if e.get("from") and e.get("to"):
                query = f"directions from {e['from']} to {e['to']}"
            else:
                query = f"directions to {e['to']}"
            reply = web_search(query, search_type="local")

        elif t == "movie":
            search_parts = ["movie showtimes"]
            if e.get("title"): search_parts.append(e["title"])
            if e.get("city"): search_parts.append(f"in {e['city']}")
            if e.get("date"): search_parts.append(e["date"])
            
            reply = web_search(" ".join(search_parts))

        elif t == "flight":
            if e.get("flight_number"):
                query = f"flight status {e['flight_number']}"
            else:
                query = f"{e['airline']} flight status" if e.get("airline") else "flight status"
            reply = web_search(query)

        elif t == "hours":
            parts = [e["biz"]]
            if e.get("city"): parts.append(e["city"])
            parts.append("hours")
            if e.get("day"): parts.append(e["day"])
            reply = web_search(" ".join(parts), search_type="local")

        elif t == "news":
            query = e["topic"] or "news headlines"
            reply = web_search(query, search_type="news")

        elif t == "weather":
            query = "weather"
            if e.get("city"): query += f" in {e['city']}"
            if e.get("day") != "today": query += f" {e['day']}"
            reply = web_search(query)

        else:
            # Fallback to general search
            reply = web_search(body)

        # Ensure reply fits SMS limits
        if len(reply) > 300:
            reply = reply[:297] + "..."

        save_message(sender, "assistant", reply)
        send_sms(sender, reply)
        return "OK", 200

    # Fallback to GPT
    try:
        reply = ask_gpt(sender, body)
        save_message(sender, "assistant", reply)
        send_sms(sender, reply)
        return "OK", 200
    except Exception as e:
        error_msg = "Sorry, I'm experiencing technical difficulties."
        save_message(sender, "assistant", error_msg)
        send_sms(sender, error_msg)
        return "OK", 200

@app.route("/", methods=["GET"])
def index():
    """Root endpoint for health checks and service verification"""
    return {
        "service": "Dirty Coast SMS Chatbot",
        "status": "running",
        "version": "2.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoints": {
            "sms_webhook": "/sms (POST)",
            "health_check": "/health (GET)"
        }
    }, 200

@app.route("/health", methods=["GET"])
def health_check():
    """Detailed health check endpoint"""
    try:
        # Test database connection
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM messages")
            message_count = c.fetchone()[0]
        
        # Check required environment variables
        env_status = {
            "clicksend_configured": bool(CLICKSEND_USERNAME and CLICKSEND_API_KEY),
            "openai_configured": bool(OPENAI_API_KEY),
            "serpapi_configured": bool(SERPAPI_API_KEY)
        }
        
        return {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database": {
                "connected": True,
                "message_count": message_count
            },
            "environment": env_status,
            "whitelist_count": len(load_whitelist())
        }, 200
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }, 500

@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors gracefully"""
    return {
        "error": "Not Found",
        "message": "The requested endpoint does not exist",
        "available_endpoints": ["/", "/sms", "/health"]
    }, 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors gracefully"""
    return {
        "error": "Internal Server Error",
        "message": "An unexpected error occurred",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }, 500

# Get port from environment variable (Render sets this automatically)
port = int(os.getenv("PORT", 5000))

if __name__ == "__main__":
    # Use gunicorn in production, Flask dev server locally
    if os.getenv("RENDER"):
        # This should not run in Render since gunicorn starts the app
        pass
    else:
        app.run(debug=True, host="0.0.0.0", port=port)
