from flask import Flask, request, jsonify
import requests
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
import logging
from functools import wraps
import time
import anthropic

# Load env vars
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('chatbot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# === Config & API Keys ===
CLICKSEND_USERNAME = os.getenv("CLICKSEND_USERNAME")
CLICKSEND_API_KEY = os.getenv("CLICKSEND_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

# Initialize Anthropic client
anthropic_client = None
if ANTHROPIC_API_KEY:
    try:
        import anthropic as anthropic_lib
        anthropic_lib.api_key = ANTHROPIC_API_KEY
        anthropic_client = anthropic_lib
        logger.info("Anthropic client initialized successfully (module-level)")
    except Exception as e:
        logger.error(f"Failed to initialize Anthropic: {e}")
        anthropic_client = None
else:
    logger.warning("ANTHROPIC_API_KEY not found")

WHITELIST_FILE = "whitelist.txt"
USAGE_FILE = "usage.json"
USAGE_LIMIT = 200
RESET_DAYS = 30
DB_PATH = os.getenv("DB_PATH", "chat.db")

WELCOME_MSG = (
    "Hey there! This is Alex, your SMS assistant powered by Claude AI. "
    "I help you stay connected to the info you need without spending time online. "
    "Ask me about weather, restaurants, directions, news, business hours, and more. "
    "Reply STOP anytime to unsubscribe from these messages."
)

# === Error Handling Decorator ===
def handle_errors(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {f.__name__}: {str(e)}", exc_info=True)
            return {"error": "Internal server error"}, 500
    return decorated_function

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
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            intent_type TEXT,
            response_time_ms INTEGER
        );
        """)
        
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_phone_ts 
        ON messages(phone, ts DESC);
        """)
        
        c.execute("""
        CREATE TABLE IF NOT EXISTS usage_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            intent_type TEXT,
            success BOOLEAN,
            response_time_ms INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        conn.commit()

def save_message(phone, role, content, intent_type=None, response_time_ms=None):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO messages (phone, role, content, intent_type, response_time_ms) 
            VALUES (?, ?, ?, ?, ?)
        """, (phone, role, content, intent_type, response_time_ms))
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

def log_usage_analytics(phone, intent_type, success, response_time_ms):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO usage_analytics (phone, intent_type, success, response_time_ms)
            VALUES (?, ?, ?, ?)
        """, (phone, intent_type, success, response_time_ms))
        conn.commit()

init_db()

# === Content Filtering ===
class ContentFilter:
    def __init__(self):
        self.spam_keywords = {
            'promotional': ['free', 'win', 'winner', 'prize', 'congratulations'],
            'suspicious': ['bitcoin', 'crypto', 'investment opportunity'],
            'inappropriate': ['adult', 'dating', 'hookup'],
            'phishing': ['verify account', 'suspended', 'click link']
        }
    
    def is_spam(self, text: str) -> tuple[bool, str]:
        text_lower = text.lower()
        for category, keywords in self.spam_keywords.items():
            for keyword in keywords:
                if keyword in text_lower:
                    return True, f"Spam detected: {category}"
        return False, ""
    
    def is_valid_query(self, text: str) -> tuple[bool, str]:
        text = text.strip()
        if len(text) < 2:
            return False, "Query too short"
        if len(text) > 500:
            return False, "Query too long"
        
        short_allowed = ['hi', 'hey', 'hello', 'help', 'yes', 'no', 'ok', 'thanks', 'stop']
        if text.lower() in short_allowed:
            return True, ""
        
        is_spam, spam_reason = self.is_spam(text)
        if is_spam:
            return False, spam_reason
        
        return True, ""

content_filter = ContentFilter()

# === Rate Limiting ===
def load_usage():
    try:
        with open(USAGE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_usage(data):
    try:
        with open(USAGE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save usage data: {e}")

def can_send(sender):
    usage = load_usage()
    now = datetime.now(timezone.utc)
    
    # Get existing record or create new one with all required fields
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
        last_reset = datetime.fromisoformat(record["last_reset"]).replace(tzinfo=timezone.utc)
        last_hour = datetime.fromisoformat(record["last_hour"]).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        record["last_reset"] = now.isoformat()
        record["last_hour"] = now.replace(minute=0, second=0, microsecond=0).isoformat()
        last_reset = now
        last_hour = now.replace(minute=0, second=0, microsecond=0)
    
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    
    if now - last_reset > timedelta(days=RESET_DAYS):
        record["count"] = 0
        record["last_reset"] = now.isoformat()
    
    if current_hour > last_hour:
        record["hourly_count"] = 0
        record["last_hour"] = current_hour.isoformat()
    
    if record["count"] >= USAGE_LIMIT:
        return False, "Monthly limit reached"
    
    if record["hourly_count"] >= 10:
        return False, "Hourly limit reached"
    
    record["count"] += 1
    record["hourly_count"] += 1
    usage[sender] = record
    save_usage(usage)
    return True, ""

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
        try:
            with open(WHITELIST_FILE, "a") as f:
                f.write(phone + "\n")
            return True
        except Exception as e:
            logger.error(f"Failed to add {phone} to whitelist: {e}")
    return False

def remove_from_whitelist(phone):
    wl = load_whitelist()
    if phone in wl:
        try:
            wl.remove(phone)
            with open(WHITELIST_FILE, "w") as f:
                for num in wl:
                    f.write(num + "\n")
            return True
        except Exception as e:
            logger.error(f"Failed to remove {phone} from whitelist: {e}")
    return False

WHITELIST = load_whitelist()

# === SMS Functions ===
def send_sms(to_number, message):
    if not CLICKSEND_USERNAME or not CLICKSEND_API_KEY:
        logger.error("ClickSend credentials not configured")
        return {"error": "SMS service not configured"}
    
    url = "https://rest.clicksend.com/v3/sms/send"
    headers = {"Content-Type": "application/json"}
    
    if len(message) > 1600:
        message = message[:1597] + "..."
    
    payload = {"messages": [{
        "source": "python",
        "body": message,
        "to": to_number,
        "custom_string": "alex_reply"
    }]}
    
    try:
        resp = requests.post(
            url,
            auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
            headers=headers,
            json=payload,
            timeout=15
        )
        
        result = resp.json()
        if resp.status_code == 200:
            logger.info(f"SMS sent successfully to {to_number}")
            return result
        else:
            logger.warning(f"SMS send failed: {result}")
            
    except Exception as e:
        logger.error(f"SMS error: {e}")
    
    return {"error": "Failed to send SMS"}

# === Search Function ===
def web_search(q, num=3, search_type="general"):
    if not SERPAPI_API_KEY:
        return "Search unavailable - service not configured."
    
    q = q.strip()
    if len(q) < 2:
        return "Search query too short."
    
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google",
        "q": q,
        "num": min(num, 5),
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
        "gl": "us",
    }
    
    if search_type == "news":
        params["tbm"] = "nws"
    elif search_type == "local":
        params["engine"] = "google_maps"
    
    try:
        logger.info(f"Searching: {q} (type: {search_type})")
        r = requests.get(url, params=params, timeout=15)
        
        if r.status_code != 200:
            return f"Search error (status {r.status_code})"
            
        data = r.json()
        logger.info(f"Search response keys: {list(data.keys())}")
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        return "Search service temporarily unavailable."

    # Handle news results
    if search_type == "news" and "news_results" in data:
        news = data["news_results"]
        if news:
            top = news[0]
            title = top.get('title', '')
            snippet = top.get('snippet', '')
            result = f"{title}"
            if snippet:
                result += f" — {snippet}"
            return result[:320]
    
    # Handle local/maps results - IMPROVED PARSING
    if search_type == "local":
        # Check multiple possible result fields
        local_results = []
        if "local_results" in data:
            local_results = data["local_results"]
        elif "places_results" in data:
            local_results = data["places_results"]
        elif "places" in data:
            local_results = data["places"]
        
        logger.info(f"Found {len(local_results)} local results")
        
        if local_results:
            # Use the first result
            result_place = local_results[0]
            logger.info(f"First result data: {result_place}")
            
            # Extract available information
            name = result_place.get('title', '') or result_place.get('name', '')
            address = result_place.get('address', '') or result_place.get('vicinity', '')
            rating = result_place.get('rating', '')
            phone = result_place.get('phone', '') or result_place.get('formatted_phone_number', '')
            
            # Look for hours in multiple possible fields
            hours_info = ""
            hours_fields = ['hours', 'opening_hours', 'current_opening_hours', 'regular_opening_hours']
            
            for field in hours_fields:
                if field in result_place:
                    hours_data = result_place[field]
                    if isinstance(hours_data, list) and hours_data:
                        # Take first hours entry if it's a list
                        hours_info = f" — Hours: {hours_data[0]}"
                        break
                    elif isinstance(hours_data, str):
                        hours_info = f" — Hours: {hours_data}"
                        break
                    elif isinstance(hours_data, dict):
                        # Look for current day or general info
                        if 'weekday_text' in hours_data and hours_data['weekday_text']:
                            hours_info = f" — Hours: {hours_data['weekday_text'][0]}"
                            break
                        elif 'periods' in hours_data:
                            hours_info = " — Hours available"
                            break
            
            # Check if there's hours info in the snippet/description
            snippet = result_place.get('snippet', '') or result_place.get('description', '')
            if not hours_info and snippet:
                # Look for hours patterns in snippet
                import re
                hours_patterns = [
                    r'(open|opens?)\s+(\d{1,2}:\d{2}\s*[ap]m|\d{1,2}\s*[ap]m)',
                    r'(\d{1,2}:\d{2}\s*[ap]m|\d{1,2}\s*[ap]m)\s*-\s*(\d{1,2}:\d{2}\s*[ap]m|\d{1,2}\s*[ap]m)',
                    r'hours?:?\s*([^.]+)',
                ]
                for pattern in hours_patterns:
                    match = re.search(pattern, snippet.lower())
                    if match:
                        hours_info = f" — {match.group(0).title()}"
                        break
            
            # Build result string
            result = name if name else "Business found"
            if rating:
                result += f" (★{rating})"
            if hours_info:
                result += hours_info
            elif address:
                result += f" — {address}"
            if phone and not hours_info:
                result += f" — {phone}"
            
            logger.info(f"Formatted result: {result}")
            return result[:320]
        
        # If no local results, check if there are regular organic results
        elif "organic_results" in data and data["organic_results"]:
            logger.info("No local results, checking organic results")
            org = data["organic_results"][0]
            title = org.get("title", "")
            snippet = org.get("snippet", "")
            result = f"{title}"
            if snippet:
                result += f" — {snippet}"
            return result[:320]
        
        else:
            logger.warning(f"No local results found. Available keys: {list(data.keys())}")
    
    # Handle regular search results
    org = data.get("organic_results", [])
    if org:
        top = org[0]
        title = top.get("title", "")
        snippet = top.get("snippet", "")
        
        result = f"{title}"
        if snippet:
            result += f" — {snippet}"
        return result[:320]
    
    return f"No results found for '{q}'."

# === Extractors ===
def _extract_city(text: str) -> Optional[str]:
    patterns = [
        r"\bin\s+([A-Z][\w''\-]*(?:\s+[A-Z][\w''\-]*){0,4})",
        r"\bnear\s+([A-Z][\w''\-]*(?:\s+[A-Z][\w''\-]*){0,4})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return None

def _extract_day(text: str) -> Optional[str]:
    t = text.lower()
    if "today" in t: return "today"
    if "tomorrow" in t: return "tomorrow"
    for name in calendar.day_name:
        if name.lower() in t:
            return name
    return None

# === Intent Results ===
@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]
    confidence: float = 1.0

# === Intent Detectors ===
def detect_hours_intent(text: str) -> Optional[IntentResult]:
    t = text.strip()
    day = _extract_day(t)
    city = _extract_city(t)
    
    patterns = [
        r"what\s+time\s+does\s+(.+?)\s+(open|close)",
        r"hours\s+for\s+(.+)$",
        r"(.+?)\s+hours\b",
    ]
    
    for p in patterns:
        m = re.search(p, t, flags=re.I)
        if m:
            biz = m.group(1).strip()
            if not biz:
                continue
                
            # Clean up business name
            if biz.lower().startswith('the '):
                biz = biz[4:]
            
            # Remove city from business name if it appears at the end
            if city:
                pattern = r'\s+(?:in\s+)?' + re.escape(city) + r'$'
                biz = re.sub(pattern, '', biz, flags=re.I)
            
            logger.info(f"Extracted business: '{biz}', city: '{city}', day: '{day}'")
            
            return IntentResult("hours", {"biz": biz.strip(), "city": city, "day": day})
    return None

def detect_weather_intent(text: str) -> Optional[IntentResult]:
    # Use word boundaries to match weather keywords precisely
    weather_patterns = [
        r'\bweather\b',
        r'\btemperature\b',
        r'\btemp\b',
        r'\bforecast\b',
        r'\brain\b',
        r'\bsnow\b',
        r'\bcloudy\b',
        r'\bsunny\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in weather_patterns):
        city = _extract_city(text)
        day = _extract_day(text) or "today"
        return IntentResult("weather", {"city": city, "day": day})
    return None

def detect_restaurant_intent(text: str) -> Optional[IntentResult]:
    # Use word boundaries to avoid partial matches like "eat" in "weather"
    food_keywords = ['restaurant', 'food', 'eat', 'dining', 'menu']
    
    # Create pattern with word boundaries to match whole words only
    pattern = r'\b(?:' + '|'.join(re.escape(kw) for kw in food_keywords) + r')\b'
    
    if re.search(pattern, text.lower()):
        city = _extract_city(text)
        
        # Extract restaurant name
        restaurant_name = None
        match = re.search(r'^(.+?)\s+restaurant', text, re.IGNORECASE)
        if match:
            restaurant_name = match.group(1).strip()
        
        return IntentResult("restaurant", {
            "city": city,
            "restaurant_name": restaurant_name,
            "query": text
        })
    return None

def detect_news_intent(text: str) -> Optional[IntentResult]:
    news_patterns = [
        r'\b(latest|current)\s+news\b',
        r'\bheadlines\b',
        r'\bnews\s+(about|on)\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in news_patterns):
        topic = re.sub(r"\b(latest|current|news|headlines|on|about|the)\b", "", text, flags=re.I).strip()
        return IntentResult("news", {"topic": topic})
    return None

# Detector order - FIXED: Weather before Restaurant to prevent "eat" in "weather" matches
DET_ORDER = [
    detect_hours_intent,
    detect_weather_intent,    # Moved up to check weather first
    detect_restaurant_intent, # Moved down - now uses word boundaries
    detect_news_intent,
]

def detect_intent(text: str) -> Optional[IntentResult]:
    for fn in DET_ORDER:
        res = fn(text)
        if res:
            logger.info(f"Detected intent: {res.type} with entities: {res.entities}")
            return res
    return None

# === Claude Chat ===
def ask_claude(phone, user_msg):
    start_time = time.time()
    
    if not anthropic_client:
        return "Hi! I'm Alex, your SMS assistant. AI responses are unavailable right now, but I can help you search for info!"
    
    try:
        history = load_history(phone, limit=6)
        
        system_context = """You are Alex, a helpful SMS assistant that helps people stay connected to information without spending time online. Keep responses under 160 characters when possible for SMS. Be friendly and helpful."""
        
        # Make direct HTTP request to Anthropic API
        try:
            headers = {
                "Content-Type": "application/json",
                "X-API-Key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            }
            
            messages = []
            for msg in history[-4:]:
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
            messages.append({
                "role": "user",
                "content": user_msg
            })
            
            data = {
                "model": "claude-3-haiku-20240307",
                "max_tokens": 150,
                "temperature": 0.7,
                "system": system_context,
                "messages": messages
            }
            
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=data,
                timeout=15
            )
            
            if response.status_code == 200:
                result = response.json()
                reply = result.get("content", [{}])[0].get("text", "").strip()
            else:
                raise Exception(f"API call failed with status {response.status_code}")
                
        except Exception:
            return "Hi! I'm Alex. I'm having trouble with AI responses, but I can help you search for info!"
        
        if not reply:
            return "Hi! I'm Alex. I'm having trouble with AI responses, but I can help you search for info!"
        
        if len(reply) > 320:
            reply = reply[:317] + "..."
            
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(phone, "claude_chat", True, response_time)
        
        return reply
        
    except Exception as e:
        logger.error(f"Claude error for {phone}: {e}")
        return "Hi! I'm Alex. I'm having trouble with AI responses, but I can help you search for info!"

# === Routes ===
@app.route("/sms", methods=["POST"])
@handle_errors
def sms_webhook():
    start_time = time.time()
    
    sender = request.form.get("from")
    body = (request.form.get("body") or "").strip()
    
    logger.info(f"📱 SMS received from {sender}: {body[:50]}...")
    
    if not sender or not body:
        return "Missing fields", 400

    is_valid, filter_reason = content_filter.is_valid_query(body)
    if not is_valid:
        return jsonify({"status": "filtered", "reason": filter_reason}), 400

    # Handle STOP
    if body.upper() in ["STOP", "UNSUBSCRIBE", "QUIT"]:
        if remove_from_whitelist(sender):
            WHITELIST.discard(sender)
            send_sms(sender, "You have been unsubscribed. Text START to reactivate.")
        return "OK", 200

    # Handle START
    if body.upper() == "START":
        if add_to_whitelist(sender):
            WHITELIST.add(sender)
        send_sms(sender, "Welcome back to Hey Alex!")
        return "OK", 200

    # Auto-add new number
    is_new = add_to_whitelist(sender)
    if is_new:
        WHITELIST.add(sender)
        send_sms(sender, WELCOME_MSG)

    if sender not in WHITELIST:
        return "Number not authorized", 403

    # Rate limiting
    can_send_result, limit_reason = can_send(sender)
    if not can_send_result:
        send_sms(sender, f"Rate limit exceeded: {limit_reason}")
        return "Rate limited", 429

    save_message(sender, "user", body)

    # Intent routing
    intent = detect_intent(body)
    reply = ""
    intent_type = "general"
    
    try:
        if intent:
            intent_type = intent.type
            e = intent.entities

            if intent_type == "restaurant":
                search_parts = []
                if e.get("restaurant_name"):
                    search_parts.append(f'"{e["restaurant_name"]}"')
                    search_parts.append("restaurant")
                else:
                    search_parts.append("restaurant")
                
                if e.get("city"):
                    search_parts.append(f"in {e['city']}")
                
                search_query = " ".join(search_parts)
                reply = web_search(search_query, search_type="local")

            elif intent_type == "hours":
                search_parts = []
                biz_name = e["biz"]
                city = e.get("city")
                
                if biz_name:
                    # Try multiple search variations with local search first
                    search_attempts = []
                    
                    # Attempt 1: Exact quoted search
                    if city:
                        search_attempts.append(f'"{biz_name}" in {city} hours')
                        search_attempts.append(f'"{biz_name} in {city}" hours')  # Try business name WITH location
                        search_attempts.append(f'{biz_name} {city} hours')  # No quotes
                        search_attempts.append(f'{biz_name} restaurant {city}')  # Add restaurant context
                    else:
                        search_attempts.append(f'"{biz_name}" hours')
                        search_attempts.append(f'{biz_name} restaurant hours')
                    
                    reply = "No results found"
                    for i, search_query in enumerate(search_attempts):
                        logger.info(f"Hours search attempt {i+1}: {search_query}")
                        reply = web_search(search_query, search_type="local")
                        
                        # If we found results, stop trying
                        if "No results found" not in reply:
                            logger.info(f"Success with search attempt {i+1}")
                            break
                        else:
                            logger.info(f"Search attempt {i+1} failed, trying next...")
                    
                    # If local search failed, try regular Google search
                    if "No results found" in reply and city:
                        logger.info("All local searches failed, trying regular Google search")
                        web_searches = [
                            f'{biz_name} {city} hours phone',
                            f'{biz_name} restaurant {city} hours',
                            f'{biz_name} {city} mississippi hours'  # Add state for specificity
                        ]
                        
                        for i, web_query in enumerate(web_searches):
                            logger.info(f"Web search attempt {i+1}: {web_query}")
                            reply = web_search(web_query, search_type="general")  # Regular Google search
                            
                            if "No results found" not in reply:
                                logger.info(f"Success with web search attempt {i+1}")
                                break
                    
                    # Final fallback message if nothing found
                    if "No results found" in reply:
                        reply = f"Sorry, I couldn't find hours for {biz_name} in {city}. You might try calling them directly or checking their website/social media."
                        
                else:
                    reply = "Please specify a business name for hours information."

            elif intent_type == "weather":
                query = "weather"
                if e.get("city"):
                    query += f" in {e['city']}"
                if e.get("day") and e["day"] != "today":
                    query += f" {e['day']}"
                reply = web_search(query)

            elif intent_type == "news":
                query = e["topic"] or "news headlines"
                reply = web_search(query, search_type="news")

            else:
                reply = web_search(body)

        else:
            reply = ask_claude(sender, body)
            intent_type = "claude_chat"

        if len(reply) > 300:
            reply = reply[:297] + "..."

        response_time = int((time.time() - start_time) * 1000)
        save_message(sender, "assistant", reply, intent_type, response_time)
        log_usage_analytics(sender, intent_type, True, response_time)
        
        sms_result = send_sms(sender, reply)
        
        if "error" in sms_result:
            logger.error(f"Failed to send SMS to {sender}: {sms_result}")
            return "SMS send failed", 500
        
        logger.info(f"Successfully processed {intent_type} query for {sender} in {response_time}ms")
        return "OK", 200

    except Exception as e:
        logger.error(f"Error processing message from {sender}: {e}", exc_info=True)
        error_msg = "Sorry, I'm experiencing technical difficulties."
        send_sms(sender, error_msg)
        return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Hey Alex SMS Assistant",
        "description": "SMS assistant powered by Claude AI for staying connected without staying online",
        "status": "running",
        "version": "1.0"
    }), 200

@app.route("/health", methods=["GET"])
def health_check():
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM messages")
            message_count = c.fetchone()[0]
        
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database": {"connected": True, "message_count": message_count},
            "whitelist_count": len(load_whitelist())
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "unhealthy", 
            "error": str(e)
        }), 500

# Get port from environment
port = int(os.getenv("PORT", 5000))

if __name__ == "__main__":
    logger.info("🔥 STARTING HEY ALEX SMS ASSISTANT 🔥")
    logger.info("📱 Helping people stay connected without staying online")
    logger.info(f"Whitelist: {len(WHITELIST)} numbers")
    
    if os.getenv("RENDER"):
        logger.info("🚀 RUNNING HEY ALEX IN PRODUCTION 🚀")
        app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
    else:
        app.run(debug=True, host="0.0.0.0", port=port)
