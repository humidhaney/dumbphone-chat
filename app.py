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
import csv
import io

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

# Version tracking
APP_VERSION = "2.4"
CHANGELOG = {
    "2.4": "Fixed content filter false positives for philosophical questions, improved spam detection accuracy",
    "2.3": "Added ClickSend contact list sync, enhanced broadcasting capabilities, and contact management features",
    "2.2": "Added comprehensive monthly SMS usage tracking with 300 message quota per 30-day period, quota management system, and usage analytics",
    "2.1": "Major upgrade: Enhanced cultural query detection, improved restaurant intent filtering, enhanced SMS debugging with ClickSend status monitoring",
    "1.4": "Fixed search capability claims - Claude now properly routes searches instead of denying search ability",
    "1.3": "Updated welcome message with personality and clear examples, ready for testing",
    "1.2": "Fixed search follow-ups, enhanced context awareness, prevented search promise loops",
    "1.1": "Enhanced fact-checking, fixed intent detection order, improved Claude context isolation",
    "1.0": "Initial release with SMS assistant functionality"
}

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
MONTHLY_USAGE_FILE = "monthly_usage.json"
USAGE_LIMIT = 200
MONTHLY_LIMIT = 300
RESET_DAYS = 30
DB_PATH = os.getenv("DB_PATH", "chat.db")

# WELCOME MESSAGE
WELCOME_MSG = (
    "Hey there! 🌟 I'm Alex - think of me as your personal research assistant who lives in your texts. "
    "I'm great at finding: ✓ Weather & forecasts ✓ Restaurant info & hours ✓ Local business details "
    "✓ Current news & headlines No apps, no browsing - just text me your question and I'll handle the rest! "
    "Try asking \"weather today\" to get started."
)

# QUOTA WARNING MESSAGES
QUOTA_WARNING_MSG = (
    "⚠️ Hey! You've used {count} of your 300 monthly messages. "
    "You have {remaining} messages left this month. Your count resets every 30 days."
)

QUOTA_EXCEEDED_MSG = (
    "🚫 You've reached your monthly limit of 300 messages. "
    "Your quota will reset in {days_remaining} days. "
    "Thanks for using Hey Alex! We'll be here when your quota refreshes."
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
        
        c.execute("""
        CREATE TABLE IF NOT EXISTS fact_check_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            query TEXT NOT NULL,
            response TEXT NOT NULL,
            incident_type TEXT DEFAULT 'potential_hallucination',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        c.execute("""
        CREATE TABLE IF NOT EXISTS conversation_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            context_key TEXT NOT NULL,
            context_value TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(phone, context_key)
        );
        """)
        
        c.execute("""
        CREATE TABLE IF NOT EXISTS sms_delivery_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            message_content TEXT NOT NULL,
            clicksend_response TEXT,
            delivery_status TEXT,
            message_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        c.execute("""
        CREATE TABLE IF NOT EXISTS monthly_sms_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            message_count INTEGER DEFAULT 1,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            last_message_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            quota_warnings_sent INTEGER DEFAULT 0,
            quota_exceeded BOOLEAN DEFAULT FALSE,
            UNIQUE(phone, period_start)
        );
        """)
        
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_monthly_usage_phone_period 
        ON monthly_sms_usage(phone, period_start DESC);
        """)
        
        c.execute("""
        CREATE TABLE IF NOT EXISTS clicksend_sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER,
            list_name TEXT,
            contacts_synced INTEGER,
            sync_status TEXT,
            sync_details TEXT,
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
    
    if role == "assistant" and content:
        content_lower = content.lower()
        key_topics = {
            'smartphone': ['smartphone', 'phone', 'mobile device'],
            'technology': ['technology', 'tech', 'digital'],
            'health': ['health', 'mental health', 'physical health'],
            'social media': ['social media', 'facebook', 'instagram', 'twitter'],
            'privacy': ['privacy', 'data', 'personal information'],
            'addiction': ['addiction', 'addictive', 'dependency']
        }
        
        for topic, keywords in key_topics.items():
            if any(keyword in content_lower for keyword in keywords):
                set_conversation_context(phone, "last_searched_entity", topic)
                logger.info(f"Stored conversation context: {topic}")
                break

def load_history(phone, limit=4):
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

def log_fact_check_incident(phone, query, response, incident_type="potential_hallucination"):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO fact_check_incidents (phone, query, response, incident_type)
            VALUES (?, ?, ?, ?)
        """, (phone, query, response, incident_type))
        conn.commit()

def log_sms_delivery(phone, message_content, clicksend_response, delivery_status, message_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO sms_delivery_log (phone, message_content, clicksend_response, delivery_status, message_id)
            VALUES (?, ?, ?, ?, ?)
        """, (phone, message_content, json.dumps(clicksend_response), delivery_status, message_id))
        conn.commit()

def set_conversation_context(phone, key, value):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO conversation_context (phone, context_key, context_value, timestamp)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (phone, key, value))
        conn.commit()

def get_conversation_context(phone, key):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT context_value FROM conversation_context
            WHERE phone = ? AND context_key = ?
            AND timestamp > datetime('now', '-10 minutes')
        """, (phone, key))
        result = c.fetchone()
        return result[0] if result else None

def clear_conversation_context(phone):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            DELETE FROM conversation_context
            WHERE phone = ? AND timestamp < datetime('now', '-10 minutes')
        """, (phone,))
        conn.commit()

def get_current_period_dates():
    now = datetime.now(timezone.utc)
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = period_start + timedelta(days=30)
    return period_start.date(), period_end.date()

def track_monthly_sms_usage(phone, is_outgoing=True):
    if not is_outgoing:
        return True, {}, None
    
    period_start, period_end = get_current_period_dates()
    
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        
        c.execute("""
            SELECT id, message_count, quota_warnings_sent, quota_exceeded
            FROM monthly_sms_usage
            WHERE phone = ? AND period_start = ?
        """, (phone, period_start))
        
        result = c.fetchone()
        
        if result:
            usage_id, current_count, warnings_sent, quota_exceeded = result
            new_count = current_count + 1
            
            c.execute("""
                UPDATE monthly_sms_usage 
                SET message_count = ?, last_message_date = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (new_count, usage_id))
        else:
            new_count = 1
            warnings_sent = 0
            quota_exceeded = False
            
            c.execute("""
                INSERT INTO monthly_sms_usage 
                (phone, message_count, period_start, period_end, quota_warnings_sent, quota_exceeded)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (phone, new_count, period_start, period_end, warnings_sent, quota_exceeded))
            
            usage_id = c.lastrowid
        
        conn.commit()
        
        usage_info = {
            "phone": phone,
            "current_count": new_count,
            "monthly_limit": MONTHLY_LIMIT,
            "remaining": max(0, MONTHLY_LIMIT - new_count),
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "days_remaining": (period_end - datetime.now(timezone.utc).date()).days
        }
        
        warning_message = None
        
        if new_count > MONTHLY_LIMIT:
            if not quota_exceeded:
                c.execute("""
                    UPDATE monthly_sms_usage 
                    SET quota_exceeded = TRUE
                    WHERE id = ?
                """, (usage_id,))
                conn.commit()
                
                warning_message = QUOTA_EXCEEDED_MSG.format(
                    days_remaining=usage_info["days_remaining"]
                )
                logger.warning(f"📊 QUOTA EXCEEDED: {phone} - {new_count}/{MONTHLY_LIMIT} messages")
            
            return False, usage_info, warning_message
        
        warning_thresholds = [250, 280, 295]
        
        for threshold in warning_thresholds:
            if new_count == threshold and warnings_sent < len([t for t in warning_thresholds if t <= threshold]):
                warning_message = QUOTA_WARNING_MSG.format(
                    count=new_count,
                    remaining=usage_info["remaining"]
                )
                
                c.execute("""
                    UPDATE monthly_sms_usage 
                    SET quota_warnings_sent = quota_warnings_sent + 1
                    WHERE id = ?
                """, (usage_id,))
                conn.commit()
                
                logger.info(f"📊 QUOTA WARNING: {phone} - {new_count}/{MONTHLY_LIMIT} messages (threshold: {threshold})")
                break
        
        logger.info(f"📊 Monthly usage: {phone} - {new_count}/{MONTHLY_LIMIT} messages")
        return True, usage_info, warning_message

def get_monthly_usage_stats(phone):
    period_start, period_end = get_current_period_dates()
    
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT message_count, quota_warnings_sent, quota_exceeded, last_message_date
            FROM monthly_sms_usage
            WHERE phone = ? AND period_start = ?
        """, (phone, period_start))
        
        result = c.fetchone()
        
        if result:
            count, warnings_sent, quota_exceeded, last_message = result
            return {
                "phone": phone,
                "current_count": count,
                "monthly_limit": MONTHLY_LIMIT,
                "remaining": max(0, MONTHLY_LIMIT - count),
                "quota_exceeded": bool(quota_exceeded),
                "warnings_sent": warnings_sent,
                "last_message_date": last_message,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "days_remaining": (period_end - datetime.now(timezone.utc).date()).days
            }
        else:
            return {
                "phone": phone,
                "current_count": 0,
                "monthly_limit": MONTHLY_LIMIT,
                "remaining": MONTHLY_LIMIT,
                "quota_exceeded": False,
                "warnings_sent": 0,
                "last_message_date": None,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "days_remaining": (period_end - datetime.now(timezone.utc).date()).days
            }

init_db()

class ContentFilter:
    def __init__(self):
        # Actual promotional/spam keywords - much more specific
        self.spam_keywords = {
            'promotional': [
                'free money', 'win cash', 'winner selected', 'claim prize', 
                'congratulations you won', 'act now', 'limited time offer',
                'click here to claim', 'urgent response required'
            ],
            'suspicious': [
                'send bitcoin', 'crypto investment', 'guaranteed returns',
                'double your money', 'wire transfer', 'western union'
            ],
            'inappropriate': [
                'adult content', 'dating site', 'hookup tonight',
                'xxx', 'porn', 'sexy singles'
            ],
            'phishing': [
                'verify your account now', 'account suspended click',
                'confirm identity', 'update payment info',
                'account will be closed'
            ]
        }
        
        # Whitelist for legitimate questions that might trigger false positives
        self.question_patterns = [
            r'\b(what|who|when|where|why|how|do|does|is|are|can|will|would|should)\b.*\?',
            r'\b(free will|philosophy|philosophical|ethics|moral|meaning)\b',
            r'\b(illusion|reality|consciousness|existence|purpose)\b'
        ]
    
    def is_spam(self, text: str) -> tuple[bool, str]:
        text_lower = text.lower().strip()
        
        # First check if it's a legitimate question
        for pattern in self.question_patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                logger.info(f"Legitimate question pattern detected: {pattern}")
                return False, ""
        
        # Check for actual spam - require exact phrase matches or clear promotional language
        for category, keywords in self.spam_keywords.items():
            for keyword in keywords:
                # Use word boundaries and require more context for single words
                if len(keyword.split()) == 1:
                    # Single words need to be part of clearly promotional context
                    pattern = r'\b' + re.escape(keyword) + r'\b.*\b(now|today|click|call|text)\b'
                    if re.search(pattern, text_lower):
                        return True, f"Spam detected: {category}"
                else:
                    # Multi-word phrases can be direct matches
                    if keyword in text_lower:
                        return True, f"Spam detected: {category}"
        
        # Additional check for obvious promotional patterns
        promotional_patterns = [
            r'\b(free|win|winner)\b.*\b(money|cash|prize|gift)\b.*\b(now|today|claim)\b',
            r'\bcongratulations\b.*\b(won|selected|winner)\b.*\b(claim|call|text)\b',
            r'\b(urgent|immediate)\b.*\b(action|response)\b.*\b(required|needed)\b'
        ]
        
        for pattern in promotional_patterns:
            if re.search(pattern, text_lower):
                return True, "Spam detected: promotional pattern"
        
        return False, ""
    
    def is_valid_query(self, text: str) -> tuple[bool, str]:
        text = text.strip()
        if len(text) < 2:
            return False, "Query too short"
        if len(text) > 500:
            return False, "Query too long"
        
        # Allow common short messages
        short_allowed = ['hi', 'hey', 'hello', 'help', 'yes', 'no', 'ok', 'thanks', 'stop', 'start']
        if text.lower() in short_allowed:
            return True, ""
        
        # Check for spam
        is_spam, spam_reason = self.is_spam(text)
        if is_spam:
            return False, spam_reason
        
        return True, ""

content_filter = ContentFilter()

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
    
    record = usage.get(sender, {})
    
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
    
    if record["hourly_count"] >= 15:
        return False, "Hourly limit reached (15 messages/hour)"
    
    record["count"] += 1
    record["hourly_count"] += 1
    usage[sender] = record
    save_usage(usage)
    return True, ""

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

def send_sms(to_number, message, bypass_quota=False):
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
        logger.info(f"📤 Sending SMS to {to_number}: {message[:50]}...")
        
        resp = requests.post(
            url,
            auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
            headers=headers,
            json=payload,
            timeout=15
        )
        
        result = resp.json()
        
        logger.info(f"📋 ClickSend Response Status: {resp.status_code}")
        logger.info(f"📋 ClickSend Response Body: {json.dumps(result, indent=2)}")
        
        if resp.status_code == 200:
            if "data" in result and "messages" in result["data"]:
                messages = result["data"]["messages"]
                if messages:
                    msg_status = messages[0].get("status")
                    msg_id = messages[0].get("message_id")
                    msg_price = messages[0].get("message_price")
                    
                    logger.info(f"✅ SMS queued successfully to {to_number}")
                    logger.info(f"📊 Message ID: {msg_id}, Status: {msg_status}, Price: {msg_price}")
                    
                    log_sms_delivery(to_number, message, result, msg_status, msg_id)
                    
                    if not bypass_quota:
                        track_monthly_sms_usage(to_number, is_outgoing=True)
                    
                    if msg_status != "SUCCESS":
                        logger.warning(f"⚠️  SMS Status Warning: {msg_status} for {to_number}")
                else:
                    logger.warning(f"⚠️  No message data in ClickSend response for {to_number}")
                    log_sms_delivery(to_number, message, result, "NO_MESSAGE_DATA", None)
            
            return result
        else:
            logger.error(f"❌ ClickSend API Error {resp.status_code}: {result}")
            log_sms_delivery(to_number, message, result, f"API_ERROR_{resp.status_code}", None)
            return {"error": f"ClickSend API error: {resp.status_code}"}
            
    except Exception as e:
        logger.error(f"💥 SMS Exception for {to_number}: {e}")
        log_sms_delivery(to_number, message, {"error": str(e)}, "EXCEPTION", None)
        return {"error": f"SMS send failed: {str(e)}"}

def check_clicksend_account():
    if not CLICKSEND_USERNAME or not CLICKSEND_API_KEY:
        return {"error": "ClickSend credentials not configured"}
    
    url = "https://rest.clicksend.com/v3/account"
    
    try:
        resp = requests.get(
            url,
            auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
            timeout=10
        )
        
        if resp.status_code == 200:
            account_info = resp.json()
            logger.info(f"💰 ClickSend Account Info: {json.dumps(account_info, indent=2)}")
            return account_info
        else:
            logger.error(f"❌ ClickSend Account Check Failed: {resp.status_code}")
            return {"error": f"Account check failed: {resp.status_code}"}
            
    except Exception as e:
        logger.error(f"💥 ClickSend Account Check Exception: {e}")
        return {"error": str(e)}

def create_clicksend_contact_list(list_name="Hey Alex Subscribers"):
    if not CLICKSEND_USERNAME or not CLICKSEND_API_KEY:
        return {"error": "ClickSend credentials not configured"}
    
    url = "https://rest.clicksend.com/v3/lists"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "list_name": list_name,
        "list_email_address": os.getenv("ADMIN_EMAIL", "admin@example.com")
    }
    
    try:
        response = requests.post(
            url,
            auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
            headers=headers,
            json=payload,
            timeout=15
        )
        
        result = response.json()
        logger.info(f"📋 ClickSend Contact List Creation: {response.status_code}")
        logger.info(f"📋 Response: {json.dumps(result, indent=2)}")
        
        if response.status_code == 200:
            list_id = result["data"]["list_id"]
            logger.info(f"✅ Created ClickSend contact list: {list_name} (ID: {list_id})")
            return {"success": True, "list_id": list_id, "list_name": list_name}
        else:
            logger.error(f"❌ Failed to create contact list: {result}")
            return {"error": f"ClickSend API error: {response.status_code}", "details": result}
            
    except Exception as e:
        logger.error(f"💥 ClickSend contact list creation error: {e}")
        return {"error": str(e)}

def get_clicksend_contact_lists():
    if not CLICKSEND_USERNAME or not CLICKSEND_API_KEY:
        return {"error": "ClickSend credentials not configured"}
    
    url = "https://rest.clicksend.com/v3/lists"
    
    try:
        response = requests.get(
            url,
            auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
            timeout=10
        )
        
        if response.status_code == 200:
            result = response.json()
            lists = result.get("data", {}).get("data", [])
            logger.info(f"📋 Found {len(lists)} ClickSend contact lists")
            return {"success": True, "lists": lists}
        else:
            logger.error(f"❌ Failed to get contact lists: {response.status_code}")
            return {"error": f"ClickSend API error: {response.status_code}"}
            
    except Exception as e:
        logger.error(f"💥 ClickSend contact lists error: {e}")
        return {"error": str(e)}

def sync_whitelist_to_clicksend(list_id=None, list_name="Hey Alex Subscribers"):
    if not list_id:
        logger.info("🔍 Looking for existing ClickSend contact list...")
        lists_result = get_clicksend_contact_lists()
        
        if "error" in lists_result:
            return lists_result
        
        existing_list = None
        for contact_list in lists_result["lists"]:
            if contact_list["list_name"] == list_name:
                existing_list = contact_list
                list_id = contact_list["list_id"]
                break
        
        if not existing_list:
            logger.info(f"📝 Creating new ClickSend contact list: {list_name}")
            create_result = create_clicksend_contact_list(list_name)
            if "error" in create_result:
                return create_result
            list_id = create_result["list_id"]
    
    whitelist = load_whitelist()
    if not whitelist:
        return {"error": "No numbers in whitelist"}
    
    logger.info(f"📤 Syncing {len(whitelist)} contacts to ClickSend list ID: {list_id}")
    
    contacts = []
    for i, phone in enumerate(whitelist):
        stats = get_monthly_usage_stats(phone)
        
        contact = {
            "phone_number": phone,
            "first_name": f"User{i+1}",
            "last_name": "",
            "email": "",
            "custom_1": str(stats["current_count"]),
            "custom_2": "active" if not stats["quota_exceeded"] else "over_quota",
            "custom_3": stats["last_message_date"] or "",
            "custom_4": f"remaining_{stats['remaining']}"
        }
        contacts.append(contact)
    
    url = f"https://rest.clicksend.com/v3/lists/{list_id}/contacts"
    headers = {"Content-Type": "application/json"}
    
    batch_size = 1000
    results = []
    
    for i in range(0, len(contacts), batch_size):
        batch = contacts[i:i + batch_size]
        logger.info(f"📤 Uploading batch {i//batch_size + 1}: {len(batch)} contacts")
        
        try:
            response = requests.post(
                url,
                auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
                headers=headers,
                json=batch,
                timeout=30
            )
            
            result = response.json()
            
            if response.status_code == 200:
                logger.info(f"✅ Batch {i//batch_size + 1} uploaded successfully")
                results.append({"batch": i//batch_size + 1, "status": "success", "count": len(batch)})
            else:
                logger.error(f"❌ Batch {i//batch_size + 1} failed: {result}")
                results.append({"batch": i//batch_size + 1, "status": "error", "details": result})
                
        except Exception as e:
            logger.error(f"💥 Batch {i//batch_size + 1} exception: {e}")
            results.append({"batch": i//batch_size + 1, "status": "error", "details": str(e)})
    
    successful_batches = len([r for r in results if r["status"] == "success"])
    
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO clicksend_sync_log (list_id, list_name, contacts_synced, sync_status, sync_details)
                VALUES (?, ?, ?, ?, ?)
            """, (
                list_id, 
                list_name, 
                len(contacts), 
                "success" if successful_batches == len(results) else "partial",
                json.dumps(results)
            ))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to log sync: {e}")
    
    logger.info(f"📊 Sync complete: {successful_batches}/{len(results)} batches successful")
    
    return {
        "success": True,
        "list_id": list_id,
        "list_name": list_name,
        "total_contacts": len(contacts),
        "batches": results,
        "successful_batches": successful_batches
    }

def broadcast_via_clicksend_list(list_id, message):
    if not CLICKSEND_USERNAME or not CLICKSEND_API_KEY:
        return {"error": "ClickSend credentials not configured"}
    
    url = "https://rest.clicksend.com/v3/sms/send"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "list_id": list_id,
        "body": message,
        "source": "python_broadcast"
    }
    
    try:
        logger.info(f"📢 Sending broadcast via ClickSend list {list_id}: {message[:50]}...")
        
        response = requests.post(
            url,
            auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
            headers=headers,
            json=payload,
            timeout=30
        )
        
        result = response.json()
        
        if response.status_code == 200:
            logger.info(f"✅ ClickSend broadcast sent successfully")
            return {"success": True, "result": result}
        else:
            logger.error(f"❌ ClickSend broadcast failed: {result}")
            return {"error": f"ClickSend API error: {response.status_code}", "details": result}
            
    except Exception as e:
        logger.error(f"💥 ClickSend broadcast exception: {e}")
        return {"error": str(e)}

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

    if search_type == "news" and "news_results" in data:
        news = data["news_results"]
        if news:
            top = news[0]
            title = top.get('title', '')
            snippet = top.get('snippet', '')
            result = f"{title}"
            if snippet:
                result += f" — {snippet}"
            return result[:500]
    
    if search_type == "local":
        local_results = []
        if "local_results" in data:
            local_results = data["local_results"]
        elif "places_results" in data:
            local_results = data["places_results"]
        elif "places" in data:
            local_results = data["places"]
        
        logger.info(f"Found {len(local_results)} local results")
        
        if local_results:
            result_place = local_results[0]
            logger.info(f"First result data: {result_place}")
            
            name = result_place.get('title', '') or result_place.get('name', '')
            address = result_place.get('address', '') or result_place.get('vicinity', '')
            rating = result_place.get('rating', '')
            phone = result_place.get('phone', '') or result_place.get('formatted_phone_number', '')
            
            hours_info = ""
            hours_fields = ['hours', 'opening_hours', 'current_opening_hours', 'regular_opening_hours']
            
            for field in hours_fields:
                if field in result_place:
                    hours_data = result_place[field]
                    if isinstance(hours_data, list) and hours_data:
                        hours_info = f" — Hours: {hours_data[0]}"
                        break
                    elif isinstance(hours_data, str):
                        hours_info = f" — Hours: {hours_data}"
                        break
                    elif isinstance(hours_data, dict):
                        if 'weekday_text' in hours_data and hours_data['weekday_text']:
                            hours_info = f" — Hours: {hours_data['weekday_text'][0]}"
                            break
                        elif 'periods' in hours_data:
                            hours_info = " — Hours available"
                            break
            
            snippet = result_place.get('snippet', '') or result_place.get('description', '')
            if not hours_info and snippet:
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
            return result[:500]
        
        elif "organic_results" in data and data["organic_results"]:
            logger.info("No local results, checking organic results")
            org = data["organic_results"][0]
            title = org.get("title", "")
            snippet = org.get("snippet", "")
            result = f"{title}"
            if snippet:
                result += f" — {snippet}"
            return result[:500]
        
        else:
            logger.warning(f"No local results found. Available keys: {list(data.keys())}")
    
    org = data.get("organic_results", [])
    if org:
        for result in org[:3]:
            title = result.get("title", "")
            snippet = result.get("snippet", "")
            source = result.get("source", "")
            
            low_quality_indicators = [
                'reddit.com/r/', 'yahoo.answers', 'quora.com', 
                'answers.com', 'ask.com', '/forums/', 
                'discussion', 'forum', 'thread'
            ]
            
            if any(indicator in source.lower() or indicator in title.lower() 
                   for indicator in low_quality_indicators):
                logger.info(f"Skipping low-quality source: {source}")
                continue
            
            forum_indicators = [
                'r/', 'subreddit', 'posted by', 'forum', 'discussion',
                'thread', 'reply', 'comment', 'user:', 'member since'
            ]
            
            if any(indicator in snippet.lower() for indicator in forum_indicators):
                logger.info(f"Skipping forum-like content: {snippet[:50]}...")
                continue
            
            result_text = f"{title}"
            if snippet:
                result_text += f" — {snippet}"
            
            logger.info(f"Selected quality result from: {source}")
            return result_text[:500]
        
        top = org[0]
        title = top.get("title", "")
        snippet = top.get("snippet", "")
        
        result = f"{title}"
        if snippet:
            result += f" — {snippet}"
        return result[:500]
    
    return f"No results found for '{q}'."

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

@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]
    confidence: float = 1.0

def detect_hours_intent(text: str) -> Optional[IntentResult]:
    t = text.strip()
    day = _extract_day(t)
    city = _extract_city(t)
    
    patterns = [
        r"what\s+time\s+does\s+(.+?)\s+(open|close)",
        r"hours\s+for\s+(.+)$",
        r"when\s+(?:does|is)\s+(.+?)\s+(?:open|close)",
        r"(.+?)\s+hours\b"
    ]
    
    if re.search(r'\b(experience|describe|compare|cultural|impression)\b', t, re.I):
        return None
    
    for p in patterns:
        m = re.search(p, t, flags=re.I)
        if m:
            biz = m.group(1).strip()
            if not biz:
                continue
                
            if biz.lower().startswith('the '):
                biz = biz[4:]
            
            if city:
                pattern = r'\s+(?:in\s+)?' + re.escape(city) + r'$'
                biz = re.sub(pattern, '', biz, flags=re.I)
            
            logger.info(f"Extracted business: '{biz}', city: '{city}', day: '{day}'")
            
            return IntentResult("hours", {"biz": biz.strip(), "city": city, "day": day})
    return None

def detect_weather_intent(text: str) -> Optional[IntentResult]:
    weather_patterns = [
        r'\bweather\b',
        r'\btemperature\b',
        r'\btemp\b',
        r'\bforecast\b',
        r'\brain\b',
        r'\bsnow\b',
        r'\bcloudy\b',
        r'\bsunny\b',
        r'\bstorm\b',
        r'\bhot\b',
        r'\bcold\b'
    ]
    
    if re.search(r'\b(experience|describe|compare|cultural|impression)\b', text, re.I):
        return None
    
    if any(re.search(pattern, text, re.I) for pattern in weather_patterns):
        city = _extract_city(text)
        day = _extract_day(text) or "today"
        return IntentResult("weather", {"city": city, "day": day})
    return None

def detect_cultural_query_intent(text: str) -> Optional[IntentResult]:
    cultural_patterns = [
        r'\bdescribe\s+how\b',
        r'\bexperience\s+of\b',
        r'\bhow.*(?:differ|compare|feel|experience)\b',
        r'\bwhat.*(?:like|experience)\s+(?:to|for)\b',
        r'\b(?:cultural|social|unspoken)\s+(?:norms|cues|customs)\b',
        r'\b(?:first-time|lifelong|visitor|local|tourist)\b.*(?:compared?\s+to|versus|vs)\b',
        r'\bsensory\s+impression\b',
        r'\bduring\s+(?:the\s+)?(?:rainy\s+season|winter|summer|monsoon)\b',
        r'\bhow\s+(?:does|do|would|might|could)\b.*\b(?:differ|compare|feel|experience)\b',
        r'\bwhat\s+(?:is|are)\s+(?:the\s+)?(?:difference|differences)\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in cultural_patterns):
        logger.info(f"Cultural query detected: {text[:50]}...")
        return IntentResult("cultural_query", {
            "query": text,
            "requires_search": True
        })
    return None

def detect_restaurant_intent(text: str) -> Optional[IntentResult]:
    exclusion_patterns = [
        r'\b(experience|describe|compare|differ|cultural|impression|norm|cue)\b',
        r'\b(first-time|lifelong|visitor|local|tourist)\b',
        r'\b(season|weather|atmosphere|environment)\b',
        r'\bhow.*(?:experience|feel|differ|compare)\b',
        r'\bwhat.*(?:like|experience|feel)\b.*(?:eating|food)\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in exclusion_patterns):
        logger.info(f"Excluding cultural food query from restaurant intent: {text[:50]}...")
        return None
    
    specific_restaurant_patterns = [
        r'\b(find|search|locate)\b.*\brestaurant\b',
        r'\brestaurant\s+(near|in|around)\b',
        r'\b(best|good|top)\s+restaurant\b',
        r'\b(pizza|burger|sushi|italian|mexican)\s+(?:restaurant|place|near)\b',
        r'\bwhere\s+(?:can|to)\s+eat\b',
        r'\bfood\s+(?:near|in|around)\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in specific_restaurant_patterns):
        city = _extract_city(text)
        
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

def detect_fact_check_intent(text: str) -> Optional[IntentResult]:
    fact_check_patterns = [
        r'\bwho\s+is\b',
        r'\bwho\s+(founded|created|started)\b',
        r'\bfounder\s+of\b',
        r'\bCEO\s+of\b',
        r'\bco-founded?\s+by\b',
        r'\bstarted\s+by\b',
        r'\bowns?\b.*\bcompany\b',
        r'\bwhen\s+was\b.*\b(founded|started|created)\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in fact_check_patterns):
        entity = None
        
        patterns = [
            r'who\s+is\s+([A-Za-z\s]+?)(?:\?|$)',
            r'founder\s+of\s+([A-Za-z\s]+?)(?:\?|$)',
            r'CEO\s+of\s+([A-Za-z\s]+?)(?:\?|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                entity = match.group(1).strip()
                break
        
        return IntentResult("fact_check", {
            "query": text,
            "entity": entity,
            "requires_search": True
        })
    return None

def detect_follow_up_intent(text: str, phone: str) -> Optional[IntentResult]:
    follow_up_patterns = [
        r'\bmore\s+(info|information|details)\b',
        r'\btell\s+me\s+more\b',
        r'\bhow\s+many\b',
        r'\bwhat\s+else\b',
        r'\bother\s+(details|info)\b',
        r'\bcontinue\b',
        r'\bgo\s+on\b',
        r'\band\?\s*$',
        r'\bsteps?\b',
        r'\bfull\b.*\b(recipe|instructions)\b',
        r'\bdo\s+they\b',
        r'\bcan\s+they\b',
        r'\bwill\s+they\b',
        r'\bare\s+they\b',
        r'\bwhy\s+do\s+they\b',
        r'\bhow\s+do\s+they\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in follow_up_patterns):
        last_entity = get_conversation_context(phone, "last_searched_entity")
        if last_entity:
            logger.info(f"Follow-up detected for entity: {last_entity}")
            return IntentResult("follow_up", {
                "query": text,
                "entity": last_entity,
                "original_query": f"{text} {last_entity}",
                "requires_search": True
            })
        
        history = load_history(phone, limit=6)
        if history:
            recent_topics = []
            for msg in reversed(history[-4:]):
                if msg["role"] == "assistant":
                    content = msg["content"].lower()
                    topic_keywords = [
                        'smartphone', 'phone', 'device', 'technology', 'social media',
                        'screen time', 'addiction', 'mental health', 'privacy', 'security'
                    ]
                    
                    for keyword in topic_keywords:
                        if keyword in content:
                            recent_topics.append(keyword)
                            break
            
            if recent_topics:
                context_topic = recent_topics[0]
                logger.info(f"Context-based follow-up detected for topic: {context_topic}")
                return IntentResult("follow_up", {
                    "query": text,
                    "entity": context_topic,
                    "original_query": f"{text} {context_topic}",
                    "requires_search": True
                })
    
    return None

def detect_recipe_intent(text: str) -> Optional[IntentResult]:
    recipe_patterns = [
        r'\bhow\s+to\s+make\b',
        r'\bhow\s+do\s+you\s+make\b',
        r'\brecipe\s+for\b',
        r'\bmake\s+.+\s+(in|at|with)\b',
        r'\bhow\s+to\s+(cook|bake|prepare)\b',
        r'\bsteps\s+to\s+make\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in recipe_patterns):
        food_item = None
        patterns = [
            r'how\s+to\s+make\s+(.+?)(?:\s+in|\s+at|\s+with|\?|$)',
            r'recipe\s+for\s+(.+?)(?:\?|$)',
            r'make\s+(.+?)\s+(?:in|at|with)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                food_item = match.group(1).strip()
                break
        
        return IntentResult("recipe", {
            "query": text,
            "food_item": food_item,
            "requires_search": True
        })
    return None

DET_ORDER = [
    detect_hours_intent,
    detect_weather_intent,
    detect_cultural_query_intent,
    detect_recipe_intent,
    detect_follow_up_intent,
    detect_fact_check_intent,
    detect_restaurant_intent,
    detect_news_intent,
]

def detect_intent(text: str, phone: str = None) -> Optional[IntentResult]:
    for fn in DET_ORDER:
        if fn.__name__ == "detect_follow_up_intent" and phone:
            res = fn(text, phone)
        else:
            res = fn(text)
        if res:
            logger.info(f"Detected intent: {res.type} with entities: {res.entities}")
            return res
    return None

def ask_claude(phone, user_msg):
    start_time = time.time()
    
    if not anthropic_client:
        return "Hi! I'm Alex, your SMS assistant. AI responses are unavailable right now, but I can help you search for info!"
    
    try:
        history = load_history(phone, limit=4)
        
        system_context = """You are Alex, a helpful SMS assistant that helps people stay connected to information without spending time online. 

IMPORTANT GUIDELINES:
- Keep responses under 500 characters when possible for SMS (expanded from 160)
- Be friendly and helpful
- You DO have access to web search capabilities through your routing system
- For specific information requests (recipes, current info, business details), suggest that you can search for that information
- If someone asks for detailed information that would benefit from a search, respond with "Let me search for [specific topic]" 
- Never make up detailed information - always offer to search for accurate, current details
- Be honest about your capabilities - you can search for current information

You are a helpful assistant with search capabilities. Be conversational and helpful."""
        
        try:
            headers = {
                "Content-Type": "application/json",
                "X-API-Key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            }
            
            messages = []
            for msg in history[-3:]:
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
                "temperature": 0.3,
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
        
        search_suggestion_patterns = [
            r'let me search for (.+?)(?:\.|$)',
            r'i can search for (.+?)(?:\.|$)',
            r'search for (.+?)(?:\.|$)'
        ]
        
        for pattern in search_suggestion_patterns:
            match = re.search(pattern, reply, re.I)
            if match:
                search_term = match.group(1).strip()
                logger.info(f"Claude suggested search for: {search_term}, executing actual search")
                search_result = web_search(search_term, search_type="general")
                return search_result
        
        if len(reply) > 500:
            reply = reply[:497] + "..."
            
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(phone, "claude_chat", True, response_time)
        
        return reply
        
    except Exception as e:
        logger.error(f"Claude error for {phone}: {e}")
        return "Hi! I'm Alex. I'm having trouble with AI responses, but I can help you search for info!"

@app.route("/sms", methods=["POST"])
@handle_errors
def sms_webhook():
    start_time = time.time()
    
    logger.info(f"🔍 RAW REQUEST DATA:")
    logger.info(f"📋 Request Method: {request.method}")
    logger.info(f"📋 Request Headers: {dict(request.headers)}")
    logger.info(f"📋 Request Form Data: {dict(request.form)}")
    logger.info(f"📋 Request Args: {dict(request.args)}")
    logger.info(f"📋 Request JSON: {request.get_json(silent=True)}")
    
    sender = request.form.get("from")
    body = (request.form.get("body") or "").strip()
    
    logger.info(f"📱 SMS received from {sender}: {repr(body)}")
    
    if not sender:
        logger.error(f"❌ VALIDATION FAILED: Missing 'from' field")
        logger.error(f"📋 Available form fields: {list(request.form.keys())}")
        return jsonify({"error": "Missing 'from' field",
