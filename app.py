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
        # Direct import and initialization to avoid parameter conflicts
        import anthropic as anthropic_lib
        
        # Try the newest API format first
        try:
            anthropic_client = anthropic_lib.Anthropic(
                api_key=ANTHROPIC_API_KEY,
                # Don't pass any other parameters that might conflict
            )
            logger.info("Anthropic client initialized successfully (new format)")
        except Exception as e1:
            logger.warning(f"New format failed: {e1}")
            # Try older format
            try:
                anthropic_client = anthropic_lib.Client(
                    api_key=ANTHROPIC_API_KEY
                )
                logger.info("Anthropic client initialized successfully (legacy format)")
            except Exception as e2:
                logger.warning(f"Legacy format failed: {e2}")
                # Try the most basic initialization
                try:
                    # Set the API key as a module-level variable
                    anthropic_lib.api_key = ANTHROPIC_API_KEY
                    anthropic_client = anthropic_lib
                    logger.info("Anthropic client initialized successfully (module-level)")
                except Exception as e3:
                    logger.error(f"All initialization methods failed: {e3}")
                    anthropic_client = None
                    
    except ImportError as e:
        logger.error(f"Failed to import anthropic: {e}")
        anthropic_client = None
    except Exception as e:
        logger.error(f"Unexpected error initializing Anthropic: {e}")
        anthropic_client = None
else:
    logger.warning("ANTHROPIC_API_KEY not found")

WHITELIST_FILE = "whitelist.txt"
USAGE_FILE = "usage.json"
USAGE_LIMIT = 200
RESET_DAYS = 30
DB_PATH = os.getenv("DB_PATH", "chat.db")

WELCOME_MSG = (
    "Welcome to the Dirty Coast chatbot powered by Claude AI. "
    "You can ask me to search the web, check business hours, get news, find sunrise/sunset times, "
    "get directions, check movie showtimes, find restaurants, check flight status, and much more. "
    "If at anytime you wish to unsubscribe, reply with STOP."
)

# === Enhanced Error Handling Decorator ===
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
        
        # Add indexes for better performance
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_phone_ts 
        ON messages(phone, ts DESC);
        """)
        
        # Enhanced spam detection table
        c.execute("""
        CREATE TABLE IF NOT EXISTS spam_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL UNIQUE,
            is_spam BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            hit_count INTEGER DEFAULT 0
        );
        """)
        
        # Usage analytics table
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

# === Enhanced Content Filtering ===
class ContentFilter:
    def __init__(self):
        self.spam_keywords = {
            'promotional': ['free', 'win', 'winner', 'prize', 'congratulations', 'click here', 
                           'limited time', 'act now', 'offer expires', 'cash prize', 'lottery'],
            'suspicious': ['bitcoin', 'crypto', 'investment opportunity', 'make money fast',
                          'work from home', 'guaranteed income', 'no experience needed', 'mlm'],
            'inappropriate': ['adult', 'dating', 'hookup', 'sexy', 'nude', '18+', 'escort'],
            'phishing': ['verify account', 'suspended', 'click link', 'update payment',
                        'security alert', 'urgent action required', 'account locked']
        }
        
        # More sophisticated offensive pattern detection
        self.offensive_patterns = [
            r'\b(f[*@#$%u]?[*@#$%u]?ck|sh[*@#$%]?t|damn|hell)\b',
            r'\b(stupid|idiot|moron|dumb[a@]ss|retard)\b',
            # Add more patterns as needed but be careful with false positives
        ]
        
        # Known spam phone numbers or patterns
        self.spam_numbers = set()
    
    def is_spam(self, text: str) -> tuple[bool, str]:
        """Enhanced spam detection with scoring"""
        text_lower = text.lower()
        spam_score = 0
        
        # Check keywords with weighted scoring
        for category, keywords in self.spam_keywords.items():
            for keyword in keywords:
                if keyword in text_lower:
                    spam_score += 2 if category in ['phishing', 'suspicious'] else 1
        
        # Check for excessive caps (but allow some)
        if len(text) > 20:
            caps_ratio = sum(c.isupper() for c in text) / len(text)
            if caps_ratio > 0.7:
                spam_score += 2
            elif caps_ratio > 0.5:
                spam_score += 1
        
        # Check for excessive punctuation
        punct_count = text.count('!') + text.count('?') + text.count('.')
        if punct_count > 5:
            spam_score += 1
        
        # Check for repeated characters (like "hellooooo")
        if re.search(r'(.)\1{4,}', text_lower):
            spam_score += 1
        
        # Check for URLs in unsolicited messages
        if re.search(r'http[s]?://|www\.|\w+\.(com|org|net)', text_lower):
            spam_score += 2
        
        return spam_score >= 3, f"Spam score: {spam_score}"
    
    def is_offensive(self, text: str) -> tuple[bool, str]:
        """Enhanced offensive content detection"""
        text_lower = text.lower()
        
        for pattern in self.offensive_patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return True, "Offensive language detected"
        
        # Check for hate speech patterns (be very careful with false positives)
        hate_patterns = [
            r'\b(kill yourself|kys)\b',
            # Add more carefully vetted patterns
        ]
        
        for pattern in hate_patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return True, "Hate speech detected"
        
        return False, ""
    
    def is_valid_query(self, text: str) -> tuple[bool, str]:
        """Enhanced query validation"""
        text = text.strip()
        
        # Check minimum length (but allow common short queries)
        if len(text) < 2:
            return False, "Query too short"
        
        # Check maximum length
        if len(text) > 500:
            return False, "Query too long"
        
        # Allow common short queries
        short_allowed = ['hi', 'hey', 'hello', 'help', 'yes', 'no', 'ok', 'thanks', 'stop']
        if text.lower() in short_allowed:
            return True, ""
        
        # Check for spam
        is_spam, spam_reason = self.is_spam(text)
        if is_spam:
            return False, spam_reason
        
        # Check for offensive content
        is_offensive, offensive_reason = self.is_offensive(text)
        if is_offensive:
            return False, offensive_reason
        
        # Check for bot-like patterns
        if re.match(r'^[a-zA-Z]\s*$', text) or text == text[0] * len(text):
            return False, "Invalid pattern detected"
        
        return True, ""

content_filter = ContentFilter()

# === Enhanced Rate Limiting ===
def load_usage():
    try:
        with open(USAGE_FILE, "r") as f:
            data = json.load(f)
            # Validate data structure
            for phone, record in data.items():
                if not isinstance(record, dict):
                    logger.warning(f"Invalid usage record for {phone}, resetting")
                    data[phone] = {}
            return data
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.info(f"Creating new usage file: {e}")
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
    
    # Get existing record or create new one
    record = usage.get(sender, {})
    
    # Initialize with proper defaults
    defaults = {
        "count": 0,
        "last_reset": now.isoformat(),
        "hourly_count": 0,
        "last_hour": now.replace(minute=0, second=0, microsecond=0).isoformat(),
        "daily_count": 0,
        "last_day": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    }
    
    for key, default_value in defaults.items():
        if key not in record:
            record[key] = default_value
    
    try:
        last_reset = datetime.fromisoformat(record["last_reset"]).replace(tzinfo=timezone.utc)
        last_hour = datetime.fromisoformat(record["last_hour"]).replace(tzinfo=timezone.utc)
        last_day = datetime.fromisoformat(record["last_day"]).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as e:
        logger.warning(f"Corrupted timestamps for {sender}, resetting: {e}")
        record.update(defaults)
        last_reset = now
        last_hour = now.replace(minute=0, second=0, microsecond=0)
        last_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    current_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Reset counters as needed
    if now - last_reset > timedelta(days=RESET_DAYS):
        record["count"] = 0
        record["last_reset"] = now.isoformat()
    
    if current_hour > last_hour:
        record["hourly_count"] = 0
        record["last_hour"] = current_hour.isoformat()
    
    if current_day > last_day:
        record["daily_count"] = 0
        record["last_day"] = current_day.isoformat()
    
    # Check limits with progressive restrictions
    if record["count"] >= USAGE_LIMIT:
        return False, "Monthly limit reached (200 messages)"
    
    if record["daily_count"] >= 50:  # Daily limit
        return False, "Daily limit reached (50 messages)"
    
    if record["hourly_count"] >= 10:  # Hourly limit
        return False, "Hourly limit reached (10 messages)"
    
    # Update counters
    record["count"] += 1
    record["hourly_count"] += 1
    record["daily_count"] += 1
    usage[sender] = record
    save_usage(usage)
    return True, ""

# === Whitelist functions (enhanced) ===
def load_whitelist():
    try:
        with open(WHITELIST_FILE, "r") as f:
            numbers = set()
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):  # Allow comments
                    numbers.add(line)
            return numbers
    except FileNotFoundError:
        logger.info("Creating new whitelist file")
        return set()

def add_to_whitelist(phone):
    wl = load_whitelist()
    if phone not in wl:
        try:
            with open(WHITELIST_FILE, "a") as f:
                f.write(phone + "\n")
            logger.info(f"Added {phone} to whitelist")
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
            logger.info(f"Removed {phone} from whitelist")
            return True
        except Exception as e:
            logger.error(f"Failed to remove {phone} from whitelist: {e}")
    return False

WHITELIST = load_whitelist()

# === Enhanced ClickSend SMS ===
def send_sms(to_number, message):
    """Enhanced SMS sending with retry logic and better error handling"""
    if not CLICKSEND_USERNAME or not CLICKSEND_API_KEY:
        logger.error("ClickSend credentials not configured")
        return {"error": "SMS service not configured"}
    
    url = "https://rest.clicksend.com/v3/sms/send"
    headers = {"Content-Type": "application/json"}
    
    # Ensure message fits SMS limits
    if len(message) > 1600:
        message = message[:1597] + "..."
    
    payload = {"messages": [{
        "source": "python",
        "body": message,
        "to": to_number,
        "custom_string": "gpt_reply"
    }]}
    
    max_retries = 3
    for attempt in range(max_retries):
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
                logger.warning(f"SMS send failed (attempt {attempt + 1}): {result}")
                
        except requests.exceptions.Timeout:
            logger.warning(f"SMS timeout (attempt {attempt + 1})")
        except Exception as e:
            logger.error(f"SMS error (attempt {attempt + 1}): {e}")
        
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)  # Exponential backoff
    
    return {"error": "Failed to send SMS after retries"}

# === Enhanced Search Function ===
def web_search(q, num=3, search_type="general"):
    """Enhanced web search with better error handling and caching"""
    if not SERPAPI_API_KEY:
        logger.warning("SERPAPI_API_KEY not configured")
        return "Search unavailable - service not configured."
    
    # Clean and validate query
    q = q.strip()
    if len(q) < 2:
        return "Search query too short."
    
    url = "https://serpapi.com/search.json"
    base_params = {
        "engine": "google",
        "q": q,
        "num": min(num, 5),  # Limit to prevent excessive results
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
        "gl": "us",
    }
    
    # Customize search based on type
    params = base_params.copy()
    if search_type == "news":
        params["tbm"] = "nws"
    elif search_type == "images":
        params["tbm"] = "isch"
    elif search_type == "local":
        params["engine"] = "google_maps"
        # For local searches, be more specific with the query
        if "restaurant" in q.lower() or "cafe" in q.lower() or "bar" in q.lower():
            params["type"] = "search"
    
    try:
        logger.info(f"Searching: {q} (type: {search_type})")
        r = requests.get(url, params=params, timeout=15)
        
        if r.status_code == 429:
            return "Search temporarily unavailable (rate limited)."
        elif r.status_code != 200:
            logger.error(f"Search API error: {r.status_code}")
            return f"Search error (status {r.status_code})"
            
        data = r.json()
        
    except requests.exceptions.Timeout:
        logger.warning("Search request timed out")
        return "Search timed out. Please try again."
    except Exception as e:
        logger.error(f"Search error: {e}")
        return "Search service temporarily unavailable."

    # Process results based on search type
    if search_type == "news" and "news_results" in data:
        news = data["news_results"]
        if news:
            top = news[0]
            title = top.get('title', '')
            snippet = top.get('snippet', '')
            source = top.get('source', '')
            result = f"{title}"
            if snippet:
                result += f" — {snippet}"
            if source:
                result += f" ({source})"
            return result[:320]
    
    # Handle local/maps results with better matching
    if search_type == "local" and "local_results" in data:
        local = data["local_results"]
        if local:
            # Try to find exact or close matches first
            query_lower = q.lower()
            
            # Extract the restaurant name from the query
            restaurant_name = ""
            for word in ["restaurant", "cafe", "bar", "grill", "kitchen", "bistro"]:
                if word in query_lower:
                    # Get everything before the restaurant type word
                    parts = query_lower.split(word)
                    if parts[0].strip():
                        restaurant_name = parts[0].strip()
                        break
            
            # If we couldn't extract a name, use the whole query minus location words
            if not restaurant_name:
                location_words = ["in", "near", "at", "restaurant", "cafe", "bar"]
                words = query_lower.split()
                restaurant_words = [w for w in words if w not in location_words]
                restaurant_name = " ".join(restaurant_words)
            
            # Look for exact or partial matches
            best_match = None
            for place in local:
                place_name = place.get('title', '').lower()
                if restaurant_name and restaurant_name in place_name:
                    best_match = place
                    break
                # Also check for partial word matches
                if restaurant_name:
                    name_words = restaurant_name.split()
                    if any(word in place_name for word in name_words if len(word) > 3):
                        best_match = place
                        break
            
            # Use best match or first result
            result_place = best_match or local[0]
            
            name = result_place.get('title', '')
            address = result_place.get('address', '')
            rating = result_place.get('rating', '')
            phone = result_place.get('phone', '')
            
            result = name
            if rating:
                result += f" (★{rating})"
            if address:
                result += f" — {address}"
            if phone:
                result += f" — {phone}"
                
            # If no exact match found, mention it
            if not best_match and restaurant_name:
                result = f"Couldn't find '{restaurant_name}' specifically. Nearest: {result}"
                
            return result[:320]
    
    # Handle regular search results with better restaurant-specific parsing
    org = data.get("organic_results", [])
    if not org:
        # Try knowledge graph
        kg = data.get("knowledge_graph", {})
        if kg:
            title = kg.get("title", "")
            description = kg.get("description", "")
            address = kg.get("address", "")
            phone = kg.get("phone", "")
            
            result = title
            if description:
                result += f" — {description}"
            if address:
                result += f" — {address}"
            if phone:
                result += f" — {phone}"
            
            if result.strip():
                return result[:320]
        
        return f"No results found for '{q}'. Try being more specific or check spelling."

    # For restaurant searches in regular results, look for restaurant-specific info
    best_result = None
    query_lower = q.lower()
    
    if "restaurant" in query_lower or "cafe" in query_lower:
        for result in org[:3]:  # Check first 3 results
            title = result.get("title", "").lower()
            snippet = result.get("snippet", "").lower()
            
            # Look for restaurant indicators
            restaurant_indicators = ["menu", "hours", "restaurant", "cafe", "dining", "food", "yelp", "tripadvisor"]
            if any(indicator in title + " " + snippet for indicator in restaurant_indicators):
                best_result = result
                break
    
    # Use best result or first result
    final_result = best_result or org[0]
    title = final_result.get("title", "")
    snippet = final_result.get("snippet", "")
    
    if not title and not snippet:
        return f"No relevant results found for '{q}'. Try a different search term."
    
    result = f"{title}"
    if snippet:
        result += f" — {snippet}"
    
    return result[:320] if result else f"No results found for '{q}'."

# === Keep existing extractors and intent detectors (unchanged) ===
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

@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]
    confidence: float = 1.0

# === Keep all existing intent detectors (unchanged for brevity) ===
# [All the detect_*_intent functions remain the same]

def detect_restaurant_intent(text: str) -> Optional[IntentResult]:
    food_keywords = ['restaurant', 'food', 'eat', 'dining', 'menu', 'cuisine', 'pizza', 
                    'burger', 'coffee', 'lunch', 'dinner', 'breakfast']
    
    if any(keyword in text.lower() for keyword in food_keywords):
        city = _extract_city(text)
        price_range = _extract_price_range(text)
        
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

# Add other intent detectors as needed...

DET_ORDER = [
    detect_hours_intent,
    detect_news_intent,
    detect_weather_intent,
    detect_restaurant_intent,
    # Add other detectors...
]

def detect_intent(text: str) -> Optional[IntentResult]:
    for fn in DET_ORDER:
        res = fn(text)
        if res:
            return res
    return None

# === Enhanced Claude Chat ===
def ask_claude(phone, user_msg):
    """Enhanced Claude integration with better error handling"""
    start_time = time.time()
    
    if not anthropic_client:
        logger.error("Anthropic client not initialized")
        return "Sorry, the AI service is currently unavailable."
    
    try:
        history = load_history(phone, limit=6)
        
        system_context = """You are a helpful SMS assistant for Dirty Coast, a New Orleans-based lifestyle brand. Keep responses under 160 characters when possible for SMS. Be concise but friendly and conversational. For medical emergencies, always advise calling 911. Don't provide medical diagnoses. If asked about Dirty Coast, mention it's a beloved New Orleans lifestyle brand known for local pride and unique designs. Use local New Orleans knowledge when relevant. Be helpful with local recommendations. Keep the tone casual and friendly, like a local friend helping out."""
        
        # Build a simple prompt for module-level API
        conversation_parts = [system_context]
        
        # Add conversation history
        for msg in history[-4:]:
            role_prefix = "Human: " if msg["role"] == "user" else "Assistant: "
            conversation_parts.append(f"{role_prefix}{msg['content']}")
        
        # Add current user message
        conversation_parts.append(f"Human: {user_msg}")
        conversation_parts.append("Assistant: ")
        
        prompt = "\n\n".join(conversation_parts)
        
        # Make a direct HTTP request to Anthropic API using Messages API
        try:
            import requests
            
            headers = {
                "Content-Type": "application/json",
                "X-API-Key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            }
            
            # Convert history to messages format
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
                "model": "claude-3-haiku-20240307",  # Use Claude 3 Haiku (fast and cost-effective)
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
                logger.info("Used direct HTTP Messages API call")
            else:
                logger.error(f"HTTP API call failed: {response.status_code} - {response.text}")
                raise Exception(f"API call failed with status {response.status_code}")
                
        except Exception as api_error:
            logger.error(f"Direct HTTP API call failed: {api_error}")
            # Fallback to a simple response
            return "I'm here to help! Ask me about New Orleans restaurants, weather, directions, or anything else."
        
        # Clean up the response
        if not reply or len(reply.strip()) == 0:
            return "I'm here to help! Ask me about New Orleans restaurants, weather, directions, or anything else."
        
        # Ensure SMS length compliance
        if len(reply) > 320:
            reply = reply[:317] + "..."
            
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(phone, "claude_chat", True, response_time)
        
        logger.info(f"Claude response for {phone} in {response_time}ms: {reply[:50]}...")
        return reply
        
    except Exception as e:
        logger.error(f"Claude error for {phone}: {e}")
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(phone, "claude_chat", False, response_time)
        
        # Return a helpful fallback message
        return "I'm having trouble with the AI service right now, but I can still help you search for things! Try asking about restaurants, weather, or directions."

# === Main SMS Route ===
@app.route("/sms", methods=["POST"])
@handle_errors
def sms_webhook():
    start_time = time.time()
    
    sender = request.form.get("from")
    body = (request.form.get("body") or "").strip()
    
    logger.info(f"SMS received from {sender}: {body[:50]}...")
    
    if not sender or not body:
        logger.warning("Missing sender or body in SMS")
        return "Missing fields", 400

    # Enhanced content filtering
    is_valid, filter_reason = content_filter.is_valid_query(body)
    if not is_valid:
        logger.info(f"Message filtered from {sender}: {filter_reason}")
        return jsonify({"status": "filtered", "reason": filter_reason}), 400

    # Handle STOP unsubscribe
    if body.upper() in ["STOP", "UNSUBSCRIBE", "QUIT"]:
        if remove_from_whitelist(sender):
            WHITELIST.discard(sender)
            send_sms(sender, "You have been unsubscribed. Text START to reactivate.")
        logger.info(f"User {sender} unsubscribed")
        return "OK", 200

    # Handle START resubscribe
    if body.upper() == "START":
        if add_to_whitelist(sender):
            WHITELIST.add(sender)
        send_sms(sender, "Welcome back! You're now resubscribed to Dirty Coast chatbot.")
        return "OK", 200

    # Auto-add new number + welcome
    is_new = add_to_whitelist(sender)
    if is_new:
        WHITELIST.add(sender)
        send_sms(sender, WELCOME_MSG)
        logger.info(f"New user {sender} added and welcomed")

    if sender not in WHITELIST:
        logger.warning(f"Unauthorized number: {sender}")
        return "Number not authorized", 403

    # Enhanced rate limiting
    can_send_result, limit_reason = can_send(sender)
    if not can_send_result:
        logger.info(f"Rate limited {sender}: {limit_reason}")
        send_sms(sender, f"Rate limit exceeded: {limit_reason}. Please try again later.")
        return "Rate limited", 429

    # Save user message
    save_message(sender, "user", body)

    # --- Enhanced intent routing ---
    intent = detect_intent(body)
    reply = ""
    intent_type = "general"
    
    try:
        if intent:
            intent_type = intent.type
            e = intent.entities

            if intent_type == "medical":
                if e.get("is_emergency"):
                    reply = "⚠️ For medical emergencies, call 911 immediately. For non-emergency medical help:"
                else:
                    reply = "For medical assistance:"
                
                search_query = e["query"]
                if e.get("city"):
                    search_query += f" in {e['city']}"
                
                search_result = web_search(search_query, search_type="local")
                reply += f" {search_result}"
                
            elif intent_type == "restaurant":
                # Build more specific search query
                search_parts = []
                
                # Debug logging
                logger.info(f"Restaurant entities: {e}")
                
                # If we have a specific restaurant name, prioritize that
                if e.get("restaurant_name"):
                    search_parts.append(f'"{e["restaurant_name"]}"')
                    if not any(word in e["restaurant_name"].lower() for word in ["restaurant", "cafe", "bar", "grill"]):
                        search_parts.append("restaurant")
                    logger.info(f"Found restaurant name: {e['restaurant_name']}")
                else:
                    search_parts.append("restaurant")
                    logger.info("No specific restaurant name found")
                
                if e.get("cuisine"): 
                    search_parts.append(e["cuisine"])
                    
                if e.get("city"): 
                    search_parts.append(f"in {e['city']}")
                else:
                    # Try to infer location from context or use default
                    search_parts.append("in New Orleans")
                    
                if e.get("price_range"):
                    min_p, max_p = e["price_range"]
                    search_parts.append(f"${min_p}-{max_p}")
                
                search_query = " ".join(search_parts)
                logger.info(f"Restaurant search query: {search_query}")
                reply = web_search(search_query, search_type="local")

            elif intent_type == "directions":
                if e.get("from") and e.get("to"):
                    query = f"directions from {e['from']} to {e['to']}"
                else:
                    query = f"directions to {e['to']}"
                reply = web_search(query, search_type="local")

            elif intent_type == "movie":
                search_parts = ["movie showtimes"]
                if e.get("title"): 
                    search_parts.append(e["title"])
                if e.get("city"): 
                    search_parts.append(f"in {e['city']}")
                else:
                    search_parts.append("in New Orleans")
                if e.get("date"): 
                    search_parts.append(e["date"])
                
                reply = web_search(" ".join(search_parts))

            elif intent_type == "flight":
                if e.get("flight_number"):
                    query = f"flight status {e['flight_number']}"
                else:
                    query = f"{e['airline']} flight status" if e.get("airline") else "flight status"
                reply = web_search(query)

            elif intent_type == "hours":
                parts = [e["biz"]]
                if e.get("city"): 
                    parts.append(e["city"])
                else:
                    parts.append("New Orleans")  # Default
                parts.append("hours")
                if e.get("day"): 
                    parts.append(e["day"])
                reply = web_search(" ".join(parts), search_type="local")

            elif intent_type == "news":
                query = e["topic"] or "New Orleans news headlines"
                reply = web_search(query, search_type="news")

            elif intent_type == "weather":
                query = "weather"
                if e.get("city"): 
                    query += f" in {e['city']}"
                else:
                    query += " in New Orleans"  # Default
                if e.get("day") != "today": 
                    query += f" {e['day']}"
                reply = web_search(query)

            elif intent_type == "event":
                search_parts = ["events"]
                if e.get("city"): 
                    search_parts.append(f"in {e['city']}")
                else:
                    search_parts.append("in New Orleans")
                if e.get("date"): 
                    search_parts.append(e["date"])
                reply = web_search(" ".join(search_parts))

            elif intent_type == "shopping":
                search_parts = ["shopping"]
                if e.get("city"): 
                    search_parts.append(f"in {e['city']}")
                else:
                    search_parts.append("in New Orleans")
                if e.get("price_range"):
                    min_p, max_p = e["price_range"]
                    search_parts.append(f"${min_p}-{max_p}")
                reply = web_search(" ".join(search_parts), search_type="local")

            else:
                # Fallback to general search
                reply = web_search(body)

        else:
            # No intent detected, use Claude
            reply = ask_claude(sender, body)
            intent_type = "claude_chat"

        # Ensure reply fits SMS limits
        if len(reply) > 300:
            reply = reply[:297] + "..."

        # Calculate response time
        response_time = int((time.time() - start_time) * 1000)
        
        # Save assistant message
        save_message(sender, "assistant", reply, intent_type, response_time)
        
        # Log analytics
        log_usage_analytics(sender, intent_type, True, response_time)
        
        # Send SMS
        sms_result = send_sms(sender, reply)
        
        if "error" in sms_result:
            logger.error(f"Failed to send SMS to {sender}: {sms_result}")
            return "SMS send failed", 500
        
        logger.info(f"Successfully processed {intent_type} query for {sender} in {response_time}ms")
        return "OK", 200

    except Exception as e:
        logger.error(f"Error processing message from {sender}: {e}", exc_info=True)
        
        # Calculate response time even for errors
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(sender, intent_type, False, response_time)
        
        error_msg = "Sorry, I'm experiencing technical difficulties. Please try again later."
        save_message(sender, "assistant", error_msg)
        send_sms(sender, error_msg)
        return "OK", 200  # Return 200 to prevent webhook retries

# === Enhanced Routes ===
@app.route("/", methods=["GET"])
def index():
    """Root endpoint with enhanced service information"""
    return jsonify({
        "service": "Dirty Coast SMS Chatbot",
        "status": "running",
        "version": "2.1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "features": [
            "Intent-based routing",
            "Web search integration", 
            "Content filtering",
            "Rate limiting",
            "Message history",
            "Analytics tracking"
        ],
        "endpoints": {
            "sms_webhook": "/sms (POST)",
            "health_check": "/health (GET)",
            "analytics": "/analytics (GET)",
            "whitelist_stats": "/whitelist (GET)"
        }
    }), 200

@app.route("/health", methods=["GET"])
@handle_errors
def health_check():
    """Comprehensive health check endpoint"""
    try:
        # Test database connection
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM messages")
            message_count = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM usage_analytics")
            analytics_count = c.fetchone()[0]
        
        # Check required environment variables
        env_status = {
            "clicksend_configured": bool(CLICKSEND_USERNAME and CLICKSEND_API_KEY),
            "anthropic_configured": bool(ANTHROPIC_API_KEY),
            "serpapi_configured": bool(SERPAPI_API_KEY)
        }
        
        # Check file system
        files_status = {
            "whitelist_exists": os.path.exists(WHITELIST_FILE),
            "usage_file_exists": os.path.exists(USAGE_FILE),
            "db_exists": os.path.exists(DB_PATH)
        }
        
        # Get recent activity
        try:
            with closing(sqlite3.connect(DB_PATH)) as conn:
                c = conn.cursor()
                c.execute("""
                    SELECT COUNT(*) FROM messages 
                    WHERE ts > datetime('now', '-1 hour')
                """)
                recent_messages = c.fetchone()[0]
        except:
            recent_messages = 0
        
        health_score = sum([
            env_status["clicksend_configured"],
            env_status["anthropic_configured"], 
            env_status["serpapi_configured"],
            files_status["db_exists"]
        ])
        
        status = "healthy" if health_score >= 3 else "degraded" if health_score >= 2 else "unhealthy"
        
        return jsonify({
            "status": status,
            "health_score": f"{health_score}/4",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database": {
                "connected": True,
                "message_count": message_count,
                "analytics_count": analytics_count,
                "recent_activity": recent_messages
            },
            "environment": env_status,
            "files": files_status,
            "whitelist_count": len(load_whitelist()),
            "uptime_check": True
        }), 200
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 500

@app.route("/analytics", methods=["GET"])
@handle_errors
def analytics():
    """Analytics endpoint for monitoring usage patterns"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            # Get usage by intent type
            c.execute("""
                SELECT intent_type, COUNT(*) as count, 
                       AVG(response_time_ms) as avg_response_time,
                       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count
                FROM usage_analytics 
                WHERE timestamp > datetime('now', '-7 days')
                GROUP BY intent_type
                ORDER BY count DESC
            """)
            intent_stats = [
                {
                    "intent": row[0] or "unknown",
                    "count": row[1],
                    "avg_response_time_ms": round(row[2] or 0, 2),
                    "success_rate": round((row[3] / row[1]) * 100, 2) if row[1] > 0 else 0
                }
                for row in c.fetchall()
            ]
            
            # Get hourly activity for last 24 hours
            c.execute("""
                SELECT strftime('%H', timestamp) as hour, COUNT(*) as count
                FROM usage_analytics 
                WHERE timestamp > datetime('now', '-1 day')
                GROUP BY hour
                ORDER BY hour
            """)
            hourly_activity = {str(row[0]).zfill(2): row[1] for row in c.fetchall()}
            
            # Get total stats
            c.execute("""
                SELECT COUNT(*) as total_messages,
                       COUNT(DISTINCT phone) as unique_users,
                       AVG(response_time_ms) as avg_response_time
                FROM usage_analytics 
                WHERE timestamp > datetime('now', '-7 days')
            """)
            total_stats = c.fetchone()
            
        return jsonify({
            "period": "last_7_days",
            "summary": {
                "total_messages": total_stats[0],
                "unique_users": total_stats[1],
                "avg_response_time_ms": round(total_stats[2] or 0, 2)
            },
            "intent_breakdown": intent_stats,
            "hourly_activity": hourly_activity,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 200
        
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        return jsonify({"error": "Analytics unavailable"}), 500

@app.route("/whitelist", methods=["GET"])
@handle_errors
def whitelist_stats():
    """Whitelist management endpoint"""
    try:
        whitelist = load_whitelist()
        usage_data = load_usage()
        
        # Get stats for whitelisted numbers
        stats = []
        for phone in whitelist:
            user_usage = usage_data.get(phone, {})
            stats.append({
                "phone": phone[-4:],  # Only show last 4 digits for privacy
                "monthly_usage": user_usage.get("count", 0),
                "daily_usage": user_usage.get("daily_count", 0),
                "hourly_usage": user_usage.get("hourly_count", 0)
            })
        
        return jsonify({
            "total_whitelisted": len(whitelist),
            "usage_stats": sorted(stats, key=lambda x: x["monthly_usage"], reverse=True),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 200
        
    except Exception as e:
        logger.error(f"Whitelist stats error: {e}")
        return jsonify({"error": "Whitelist stats unavailable"}), 500

# === Error Handlers ===
@app.errorhandler(404)
def not_found(error):
    """Enhanced 404 handler"""
    return jsonify({
        "error": "Not Found",
        "message": "The requested endpoint does not exist",
        "available_endpoints": {
            "GET /": "Service information",
            "POST /sms": "SMS webhook", 
            "GET /health": "Health check",
            "GET /analytics": "Usage analytics",
            "GET /whitelist": "Whitelist stats"
        }
    }), 404

@app.errorhandler(500)
def internal_error(error):
    """Enhanced 500 handler with logging"""
    logger.error(f"Internal server error: {error}")
    return jsonify({
        "error": "Internal Server Error",
        "message": "An unexpected error occurred",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "support": "Check logs for details"
    }), 500

@app.errorhandler(429)
def rate_limit_error(error):
    """Rate limiting error handler"""
    return jsonify({
        "error": "Rate Limited", 
        "message": "Too many requests, please try again later",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }), 429

# === Startup Configuration ===
def configure_app():
    """Configure app settings based on environment"""
    if os.getenv("FLASK_ENV") == "development":
        app.config['DEBUG'] = True
        logger.setLevel(logging.DEBUG)
    else:
        app.config['DEBUG'] = False
        
    # Security headers for production
    @app.after_request
    def after_request(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        return response

# Get port from environment variable (Render sets this automatically)
port = int(os.getenv("PORT", 5000))

if __name__ == "__main__":
    configure_app()
    logger.info("Starting Dirty Coast SMS Chatbot...")
    logger.info(f"Database: {DB_PATH}")
    logger.info(f"Whitelist: {len(WHITELIST)} numbers")
    
    # Validate configuration
    missing_configs = []
    if not CLICKSEND_USERNAME: missing_configs.append("CLICKSEND_USERNAME")
    if not CLICKSEND_API_KEY: missing_configs.append("CLICKSEND_API_KEY")
    if not ANTHROPIC_API_KEY: missing_configs.append("ANTHROPIC_API_KEY")
    if not SERPAPI_API_KEY: missing_configs.append("SERPAPI_API_KEY")
    
    if missing_configs:
        logger.warning(f"Missing configurations: {', '.join(missing_configs)}")
    else:
        logger.info("All configurations validated ✓")
    
    # Check if running in Render (production)
    if os.getenv("RENDER"):
        logger.info("Running in Render environment")
        # Start with production settings
        app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
    else:
        logger.info(f"Starting development server on port {port}")
        app.run(debug=True, host="0.0.0.0", port=port)
