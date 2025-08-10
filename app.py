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
APP_VERSION = "2.3"
CHANGELOG = {
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
                pattern = r'\b' + re.escape(keyword) + r'\b'
                if re.search(pattern, text_lower):
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
        return jsonify({"error": "Missing 'from' field", "available_fields": list(request.form.keys())}), 400
        
    if not body:
        logger.error(f"❌ VALIDATION FAILED: Missing 'body' field")
        logger.error(f"📋 Raw body value: {repr(request.form.get('body'))}")
        logger.error(f"📋 Available form fields: {list(request.form.keys())}")
        return jsonify({"error": "Missing 'body' field", "raw_body": repr(request.form.get('body')), "available_fields": list(request.form.keys())}), 400

    logger.info(f"🔍 CONTENT FILTER CHECK: Testing message: {repr(body)}")
    is_valid, filter_reason = content_filter.is_valid_query(body)
    logger.info(f"📋 Content filter result: valid={is_valid}, reason='{filter_reason}'")
    
    if not is_valid:
        logger.error(f"❌ CONTENT FILTER FAILED: {filter_reason}")
        logger.error(f"📋 Message length: {len(body)}")
        logger.error(f"📋 Message content: {repr(body)}")
        return jsonify({
            "status": "filtered", 
            "reason": filter_reason,
            "message_length": len(body),
            "message_content": repr(body)
        }), 400

    clear_conversation_context(sender)

    if body.upper() in ["STOP", "UNSUBSCRIBE", "QUIT"]:
        if remove_from_whitelist(sender):
            WHITELIST.discard(sender)
            send_sms(sender, "You have been unsubscribed. Text START to reactivate.", bypass_quota=True)
        return "OK", 200

    if body.upper() == "START":
        if add_to_whitelist(sender):
            WHITELIST.add(sender)
        send_sms(sender, "Welcome back to Hey Alex!", bypass_quota=True)
        return "OK", 200

    is_new = add_to_whitelist(sender)
    if is_new:
        WHITELIST.add(sender)
        send_sms(sender, WELCOME_MSG, bypass_quota=True)
        logger.info(f"✨ New user {sender} added to whitelist with welcome message")

    if sender not in WHITELIST:
        logger.error(f"❌ AUTHORIZATION FAILED: {sender} not in whitelist")
        return jsonify({"error": "Number not authorized", "phone": sender}), 403

    logger.info(f"🔍 QUOTA CHECK: Checking monthly usage for {sender}")
    can_send_monthly, usage_info, quota_warning = track_monthly_sms_usage(sender, is_outgoing=False)
    logger.info(f"📋 Quota check result: can_send={can_send_monthly}, usage={usage_info.get('current_count', 'unknown')}/{MONTHLY_LIMIT}")
    
    if not can_send_monthly:
        logger.warning(f"⚠️ QUOTA EXCEEDED: {sender} has exceeded monthly limit")
        if quota_warning:
            send_sms(sender, quota_warning, bypass_quota=True)
        return jsonify({"status": "quota_exceeded", "usage": usage_info}), 429

    logger.info(f"🔍 RATE LIMIT CHECK: Checking legacy rate limits for {sender}")
    can_send_result, limit_reason = can_send(sender)
    logger.info(f"📋 Rate limit result: can_send={can_send_result}, reason='{limit_reason}'")
    
    if not can_send_result:
        logger.warning(f"⚠️ RATE LIMITED: {sender} - {limit_reason}")
        send_sms(sender, f"Rate limit exceeded: {limit_reason}", bypass_quota=True)
        return jsonify({"status": "rate_limited", "reason": limit_reason}), 429

    logger.info(f"✅ ALL VALIDATIONS PASSED: Processing message from {sender}")
    
    save_message(sender, "user", body)

    intent = detect_intent(body, sender)
    reply = ""
    intent_type = "general"
    
    try:
        if intent:
            intent_type = intent.type
            e = intent.entities
            logger.info(f"🎯 INTENT DETECTED: {intent_type} with entities: {e}")

            if intent_type == "cultural_query":
                search_query = e.get("query", body)
                reply = web_search(search_query, search_type="general")
                logger.info(f"Cultural query routed to general search: {search_query}")

            elif intent_type == "recipe":
                search_query = e.get("query", body)
                reply = web_search(search_query, search_type="general")
                logger.info(f"Recipe intent routed to search: {search_query}")
                
                if e.get("food_item"):
                    set_conversation_context(sender, "last_searched_entity", e["food_item"])

            elif intent_type == "fact_check":
                search_query = e.get("query", body)
                reply = web_search(search_query, search_type="general")
                logger.info(f"Fact-check intent routed to search: {search_query}")
                
                if e.get("entity"):
                    set_conversation_context(sender, "last_searched_entity", e["entity"])

            elif intent_type == "follow_up":
                entity = e.get("entity")
                if entity:
                    base_query = body
                    context_entity = entity
                    
                    if "do they" in body.lower():
                        search_query = f"do {context_entity} {body.lower().replace('do they', '').strip()}"
                    elif "can they" in body.lower():
                        search_query = f"can {context_entity} {body.lower().replace('can they', '').strip()}"
                    elif "are they" in body.lower():
                        search_query = f"are {context_entity} {body.lower().replace('are they', '').strip()}"
                    else:
                        search_query = f"{body} {context_entity}"
                    
                    search_query = re.sub(r'\s+', ' ', search_query).strip()
                    
                    reply = web_search(search_query, search_type="general")
                    logger.info(f"Follow-up intent routed to search: '{body}' + '{context_entity}' → '{search_query}'")
                else:
                    reply = "What would you like to know more about? Please be specific."

            elif intent_type == "restaurant":
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
                biz_name = e["biz"]
                city = e.get("city")
                
                if biz_name:
                    search_attempts = []
                    
                    if city:
                        search_attempts.append(f'"{biz_name}" in {city} hours')
                        search_attempts.append(f'"{biz_name} in {city}" hours')
                        search_attempts.append(f'{biz_name} {city} hours')
                        search_attempts.append(f'{biz_name} restaurant {city}')
                    else:
                        search_attempts.append(f'"{biz_name}" hours')
                        search_attempts.append(f'{biz_name} restaurant hours')
                    
                    reply = "No results found"
                    for i, search_query in enumerate(search_attempts):
                        logger.info(f"Hours search attempt {i+1}: {search_query}")
                        reply = web_search(search_query, search_type="local")
                        
                        if "No results found" not in reply:
                            logger.info(f"Success with search attempt {i+1}")
                            break
                        else:
                            logger.info(f"Search attempt {i+1} failed, trying next...")
                    
                    if "No results found" in reply and city:
                        logger.info("All local searches failed, trying regular Google search")
                        web_searches = [
                            f'{biz_name} {city} hours phone',
                            f'{biz_name} restaurant {city} hours',
                            f'{biz_name} {city} mississippi hours'
                        ]
                        
                        for i, web_query in enumerate(web_searches):
                            logger.info(f"Web search attempt {i+1}: {web_query}")
                            reply = web_search(web_query, search_type="general")
                            
                            if "No results found" not in reply:
                                logger.info(f"Success with web search attempt {i+1}")
                                break
                    
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
                search_query = body
                
                if "?" in search_query:
                    question_patterns = [
                        (r'\bdoes\s+(.+?)\s+freeze\?', r'\1 freezing point temperature'),
                        (r'\bwhat\s+is\s+(.+?)\?', r'\1 definition explanation'),
                        (r'\bhow\s+to\s+(.+?)\?', r'\1 guide tutorial'),
                        (r'\bwhy\s+does\s+(.+?)\?', r'\1 explanation reason'),
                        (r'\bwhen\s+does\s+(.+?)\?', r'\1 timing schedule'),
                        (r'\bwhere\s+is\s+(.+?)\?', r'\1 location')
                    ]
                    
                    for pattern, replacement in question_patterns:
                        match = re.search(pattern, search_query, re.I)
                        if match:
                            search_query = re.sub(pattern, replacement, search_query, flags=re.I)
                            logger.info(f"Enhanced search query: '{body}' → '{search_query}'")
                            break
                
                reply = web_search(search_query)

        else:
            logger.info(f"🤖 NO SPECIFIC INTENT: Checking if Claude can handle this directly")
            
            simple_question_patterns = [
                r'\bdoes\s+\w+\s+freeze\?',
                r'\bwhat\s+is\s+\w+\?',
                r'\bhow\s+much\s+does\s+\w+\s+cost\?',
                r'\bwhen\s+did\s+\w+\s+happen\?'
            ]
            
            is_simple_question = any(re.search(pattern, body, re.I) for pattern in simple_question_patterns)
            
            if is_simple_question and len(body.split()) <= 5:
                logger.info(f"🎯 SIMPLE QUESTION: Trying Claude first")
                reply = ask_claude(sender, body)
                intent_type = "claude_chat"
                
                generic_responses = [
                    "let me search", "i'd recommend searching", "search for", 
                    "i don't have", "i'm not sure", "i don't know"
                ]
                
                if any(phrase in reply.lower() for phrase in generic_responses):
                    logger.info(f"🔄 CLAUDE FALLBACK: Claude couldn't answer, trying search")
                    search_query = body
                    if "?" in search_query:
                        question_patterns = [
                            (r'\bdoes\s+(.+?)\s+freeze\?', r'\1 freezing point temperature'),
                            (r'\bwhat\s+is\s+(.+?)\?', r'\1 definition explanation'),
                            (r'\bhow\s+to\s+(.+?)\?', r'\1 guide tutorial'),
                            (r'\bwhy\s+does\s+(.+?)\?', r'\1 explanation reason'),
                        ]
                        
                        for pattern, replacement in question_patterns:
                            match = re.search(pattern, search_query, re.I)
                            if match:
                                search_query = re.sub(pattern, replacement, search_query, flags=re.I)
                                logger.info(f"Enhanced search query: '{body}' → '{search_query}'")
                                break
                    
                    reply = web_search(search_query, search_type="general")
                    intent_type = "enhanced_search"
            else:
                logger.info(f"🔍 COMPLEX QUERY: Routing to search")
                reply = ask_claude(sender, body)
                intent_type = "claude_chat"

        if len(reply) > 500:
            reply = reply[:497] + "..."

        response_time = int((time.time() - start_time) * 1000)
        save_message(sender, "assistant", reply, intent_type, response_time)
        log_usage_analytics(sender, intent_type, True, response_time)
        
        logger.info(f"📤 SENDING MAIN RESPONSE: {reply[:50]}...")
        sms_result = send_sms(sender, reply)
        
        if quota_warning:
            logger.info(f"📤 SENDING QUOTA WARNING: {quota_warning[:50]}...")
            send_sms(sender, quota_warning, bypass_quota=True)
        
        if "error" in sms_result:
            logger.error(f"Failed to send SMS to {sender}: {sms_result}")
            return jsonify({"error": "SMS send failed", "details": sms_result}), 500
        
        logger.info(f"✅ SUCCESS: Processed {intent_type} query for {sender} in {response_time}ms")
        return jsonify({"status": "success", "intent": intent_type, "response_time_ms": response_time}), 200

    except Exception as e:
        logger.error(f"💥 PROCESSING ERROR for {sender}: {e}", exc_info=True)
        error_msg = "Sorry, I'm experiencing technical difficulties."
        send_sms(sender, error_msg, bypass_quota=True)
        return jsonify({"error": "Processing failed", "details": str(e)}), 500

@app.route("/broadcast", methods=["POST"])
@handle_errors
def broadcast_message():
    api_key = request.headers.get("X-API-Key") or request.json.get("api_key")
    expected_key = os.getenv("BROADCAST_API_KEY", "your-secret-key-here")
    
    if api_key != expected_key:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.get_json()
    if not data or not data.get("message"):
        return jsonify({"error": "Message required"}), 400
    
    message = data["message"]
    bypass_quota = data.get("bypass_quota", True)
    
    whitelist = load_whitelist()
    
    if not whitelist:
        return jsonify({"error": "No numbers in whitelist"}), 400
    
    results = []
    success_count = 0
    error_count = 0
    
    logger.info(f"📢 BROADCAST: Sending to {len(whitelist)} numbers: {message[:50]}...")
    
    for phone in whitelist:
        try:
            result = send_sms(phone, message, bypass_quota=bypass_quota)
            
            if "error" in result:
                logger.error(f"❌ Broadcast failed for {phone}: {result}")
                results.append({"phone": phone, "status": "error", "details": result})
                error_count += 1
            else:
                logger.info(f"✅ Broadcast sent to {phone}")
                results.append({"phone": phone, "status": "success"})
                success_count += 1
                
        except Exception as e:
            logger.error(f"💥 Broadcast exception for {phone}: {e}")
            results.append({"phone": phone, "status": "error", "details": str(e)})
            error_count += 1
    
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO messages (phone, role, content, intent_type, response_time_ms) 
                VALUES (?, ?, ?, ?, ?)
            """, ("BROADCAST", "assistant", f"Broadcast to {len(whitelist)} numbers: {message}", "broadcast", 0))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to log broadcast: {e}")
    
    logger.info(f"📊 BROADCAST COMPLETE: {success_count} success, {error_count} errors")
    
    return jsonify({
        "status": "completed",
        "message": message,
        "total_recipients": len(whitelist),
        "successful": success_count,
        "failed": error_count,
        "results": results
    }), 200

@app.route("/broadcast/preview", methods=["GET"])
@handle_errors  
def broadcast_preview():
    api_key = request.headers.get("X-API-Key") or request.args.get("api_key")
    expected_key = os.getenv("BROADCAST_API_KEY", "your-secret-key-here")
    
    if api_key != expected_key:
        return jsonify({"error": "Unauthorized"}), 401
    
    whitelist = load_whitelist()
    
    recipient_stats = []
    for phone in whitelist:
        stats = get_monthly_usage_stats(phone)
        recipient_stats.append({
            "phone": phone,
            "current_usage": stats["current_count"],
            "remaining": stats["remaining"],
            "quota_exceeded": stats["quota_exceeded"]
        })
    
    return jsonify({
        "total_recipients": len(whitelist),
        "recipients": recipient_stats,
        "active_users": len([r for r in recipient_stats if r["current_usage"] > 0]),
        "over_quota": len([r for r in recipient_stats if r["quota_exceeded"]])
    }), 200

@app.route("/clicksend/sync", methods=["POST"])
@handle_errors
def sync_to_clicksend():
    api_key = request.headers.get("X-API-Key") or request.json.get("api_key", "")
    expected_key = os.getenv("BROADCAST_API_KEY", "your-secret-key-here")
    
    if api_key != expected_key:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.get_json() or {}
    list_name = data.get("list_name", "Hey Alex Subscribers")
    list_id = data.get("list_id")
    
    result = sync_whitelist_to_clicksend(list_id=list_id, list_name=list_name)
    
    if "error" in result:
        return jsonify(result), 400
    
    return jsonify(result), 200

@app.route("/clicksend/lists", methods=["GET"])
@handle_errors
def get_clicksend_lists():
    api_key = request.headers.get("X-API-Key") or request.args.get("api_key", "")
    expected_key = os.getenv("BROADCAST_API_KEY", "your-secret-key-here")
    
    if api_key != expected_key:
        return jsonify({"error": "Unauthorized"}), 401
    
    result = get_clicksend_contact_lists()
    
    if "error" in result:
        return jsonify(result), 400
    
    return jsonify(result), 200

@app.route("/clicksend/broadcast", methods=["POST"])
@handle_errors
def clicksend_broadcast():
    api_key = request.headers.get("X-API-Key") or request.json.get("api_key", "")
    expected_key = os.getenv("BROADCAST_API_KEY", "your-secret-key-here")
    
    if api_key != expected_key:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.get_json()
    if not data or not data.get("message"):
        return jsonify({"error": "Message required"}), 400
    
    list_id = data.get("list_id")
    if not list_id:
        return jsonify({"error": "list_id required"}), 400
    
    message = data["message"]
    
    result = broadcast_via_clicksend_list(list_id, message)
    
    if "error" in result:
        return jsonify(result), 400
    
    return jsonify(result), 200

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Hey Alex SMS Assistant",
        "description": "SMS assistant powered by Claude AI for staying connected without staying online",
        "status": "running",
        "version": APP_VERSION,
        "changelog": CHANGELOG,
        "monthly_limit": MONTHLY_LIMIT
    }), 200

@app.route("/health", methods=["GET"])
def health_check():
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM messages")
            message_count = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM fact_check_incidents")
            incident_count = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM conversation_context WHERE timestamp > datetime('now', '-1 hour')")
            active_contexts = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM sms_delivery_log WHERE timestamp > datetime('now', '-24 hours')")
            sms_attempts_24h = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM monthly_sms_usage WHERE period_start >= date('now', '-30 days')")
            active_monthly_users = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM clicksend_sync_log")
            sync_attempts = c.fetchone()[0]
        
        return jsonify({
            "status": "healthy",
            "version": APP_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database": {"connected": True, "message_count": message_count},
            "whitelist_count": len(load_whitelist()),
            "fact_check_incidents": incident_count,
            "active_conversation_contexts": active_contexts,
            "sms_attempts_24h": sms_attempts_24h,
            "active_monthly_users": active_monthly_users,
            "clicksend_sync_attempts": sync_attempts,
            "monthly_limit": MONTHLY_LIMIT
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "unhealthy", 
            "error": str(e),
            "version": APP_VERSION
        }), 500

@app.route("/usage/<phone>", methods=["GET"])
def get_user_usage(phone):
    usage_stats = get_monthly_usage_stats(phone)
    return jsonify(usage_stats), 200

@app.route("/clicksend-status", methods=["GET"])
def clicksend_status():
    account_info = check_clicksend_account()
    
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT phone, delivery_status, message_id, timestamp
                FROM sms_delivery_log 
                WHERE timestamp > datetime('now', '-24 hours')
                ORDER BY timestamp DESC
                LIMIT 20
            """)
            recent_deliveries = [
                {"phone": row[0], "status": row[1], "message_id": row[2], "timestamp": row[3]}
                for row in c.fetchall()
            ]
            
            c.execute("""
                SELECT list_id, list_name, contacts_synced, sync_status, timestamp
                FROM clicksend_sync_log 
                ORDER BY timestamp DESC
                LIMIT 10
            """)
            recent_syncs = [
                {
                    "list_id": row[0], 
                    "list_name": row[1], 
                    "contacts_synced": row[2], 
                    "sync_status": row[3], 
                    "timestamp": row[4]
                }
                for row in c.fetchall()
            ]
            
    except Exception as e:
        recent_deliveries = {"error": str(e)}
        recent_syncs = {"error": str(e)}
    
    return jsonify({
        "account_info": account_info,
        "recent_deliveries": recent_deliveries,
        "recent_syncs": recent_syncs,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }), 200

@app.route("/analytics", methods=["GET"])
def analytics_dashboard():
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            c.execute("""
                SELECT intent_type, COUNT(*) as count 
                FROM usage_analytics 
                WHERE timestamp > datetime('now', '-7 days')
                GROUP BY intent_type
                ORDER BY count DESC
            """)
            intent_stats = {row[0]: row[1] for row in c.fetchall()}
            
            c.execute("""
                SELECT COUNT(*) as incidents
                FROM fact_check_incidents 
                WHERE timestamp > datetime('now', '-24 hours')
            """)
            recent_incidents = c.fetchone()[0]
            
            c.execute("""
                SELECT COUNT(*) as follow_ups
                FROM usage_analytics 
                WHERE intent_type = 'follow_up' AND timestamp > datetime('now', '-7 days')
            """)
            follow_up_count = c.fetchone()[0]
            
            c.execute("""
                SELECT COUNT(*) as cultural_queries
                FROM usage_analytics 
                WHERE intent_type = 'cultural_query' AND timestamp > datetime('now', '-7 days')
            """)
            cultural_count = c.fetchone()[0]
            
            c.execute("""
                SELECT COUNT(*) as recipes
                FROM usage_analytics 
                WHERE intent_type = 'recipe' AND timestamp > datetime('now', '-7 days')
            """)
            recipe_count = c.fetchone()[0]
            
            c.execute("""
                SELECT COUNT(*) as broadcasts
                FROM usage_analytics 
                WHERE intent_type = 'broadcast' AND timestamp > datetime('now', '-7 days')
            """)
            broadcast_count = c.fetchone()[0]
            
            c.execute("""
                SELECT 
                    COUNT(*) as total_attempts,
                    SUM(CASE WHEN delivery_status = 'SUCCESS' THEN 1 ELSE 0 END) as successful,
                    COUNT(DISTINCT phone) as unique_recipients
                FROM sms_delivery_log 
                WHERE timestamp > datetime('now', '-7 days')
            """)
            sms_stats = c.fetchone()
            sms_delivery_rate = {
                "total_attempts": sms_stats[0],
                "successful": sms_stats[1],
                "success_rate": round((sms_stats[1] / sms_stats[0] * 100) if sms_stats[0] > 0 else 0, 2),
                "unique_recipients": sms_stats[2]
            }
            
            c.execute("""
                SELECT 
                    COUNT(*) as active_users,
                    SUM(message_count) as total_messages,
                    AVG(message_count) as avg_messages_per_user,
                    SUM(CASE WHEN quota_exceeded = 1 THEN 1 ELSE 0 END) as users_over_quota,
                    SUM(quota_warnings_sent) as total_warnings_sent
                FROM monthly_sms_usage 
                WHERE period_start >= date('now', '-30 days')
            """)
            monthly_stats = c.fetchone()
            monthly_usage = {
                "active_users": monthly_stats[0],
                "total_messages": monthly_stats[1],
                "avg_messages_per_user": round(monthly_stats[2], 1) if monthly_stats[2] else 0,
                "users_over_quota": monthly_stats[3],
                "total_warnings_sent": monthly_stats[4],
                "monthly_limit": MONTHLY_LIMIT
            }
            
            c.execute("""
                SELECT phone, message_count, quota_exceeded, quota_warnings_sent
                FROM monthly_sms_usage 
                WHERE period_start >= date('now', '-30 days')
                ORDER BY message_count DESC
                LIMIT 10
            """)
            top_users = [
                {
                    "phone": row[0],
                    "message_count": row[1], 
                    "quota_exceeded": bool(row[2]),
                    "warnings_sent": row[3]
                }
                for row in c.fetchall()
            ]
            
            c.execute("""
                SELECT COUNT(DISTINCT phone) as new_users
                FROM messages 
                WHERE content LIKE '%think of me as your personal research assistant%'
                AND timestamp > datetime('now', '-7 days')
            """)
            new_users = c.fetchone()[0]
            
            c.execute("""
                SELECT 
                    COUNT(*) as total_syncs,
                    SUM(contacts_synced) as total_contacts_synced,
                    COUNT(CASE WHEN sync_status = 'success' THEN 1 END) as successful_syncs
                FROM clicksend_sync_log 
                WHERE timestamp > datetime('now', '-7 days')
            """)
            sync_stats = c.fetchone()
            clicksend_sync = {
                "total_syncs": sync_stats[0],
                "total_contacts_synced": sync_stats[1] or 0,
                "successful_syncs": sync_stats[2],
                "success_rate": round((sync_stats[2] / sync_stats[0] * 100) if sync_stats[0] > 0 else 0, 2)
            }
            
            return jsonify({
                "version": APP_VERSION,
                "intent_breakdown_7d": intent_stats,
                "fact_check_incidents_24h": recent_incidents,
                "follow_up_queries_7d": follow_up_count,
                "cultural_queries_7d": cultural_count,
                "recipe_queries_7d": recipe_count,
                "broadcast_count_7d": broadcast_count,
                "sms_delivery_stats_7d": sms_delivery_rate,
                "monthly_usage_stats": monthly_usage,
                "top_users_current_month": top_users,
                "new_users_7d": new_users,
                "clicksend_sync_stats_7d": clicksend_sync,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }), 200
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/monthly-usage-report", methods=["GET"])
def monthly_usage_report():
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            c.execute("""
                SELECT 
                    phone,
                    message_count,
                    period_start,
                    period_end,
                    quota_warnings_sent,
                    quota_exceeded,
                    last_message_date
                FROM monthly_sms_usage 
                WHERE period_start >= date('now', '-30 days')
                ORDER BY message_count DESC
            """)
            
            users = []
            for row in c.fetchall():
                phone, count, start, end, warnings, exceeded, last_msg = row
                users.append({
                    "phone": phone,
                    "message_count": count,
                    "remaining": max(0, MONTHLY_LIMIT - count),
                    "usage_percentage": round((count / MONTHLY_LIMIT) * 100, 1),
                    "period_start": start,
                    "period_end": end,
                    "quota_warnings_sent": warnings,
                    "quota_exceeded": bool(exceeded),
                    "last_message_date": last_msg,
                    "days_remaining": (datetime.strptime(end, '%Y-%m-%d').date() - datetime.now(timezone.utc).date()).days
                })
            
            c.execute("""
                SELECT 
                    CASE 
                        WHEN message_count <= 50 THEN '0-50'
                        WHEN message_count <= 100 THEN '51-100'
                        WHEN message_count <= 150 THEN '101-150'
                        WHEN message_count <= 200 THEN '151-200'
                        WHEN message_count <= 250 THEN '201-250'
                        WHEN message_count <= 300 THEN '251-300'
                        ELSE '300+'
                    END as usage_bracket,
                    COUNT(*) as user_count
                FROM monthly_sms_usage 
                WHERE period_start >= date('now', '-30 days')
                GROUP BY usage_bracket
                ORDER BY usage_bracket
            """)
            
            usage_distribution = {row[0]: row[1] for row in c.fetchall()}
            
            return jsonify({
                "report_date": datetime.now(timezone.utc).isoformat(),
                "monthly_limit": MONTHLY_LIMIT,
                "total_users": len(users),
                "users": users,
                "usage_distribution": usage_distribution,
                "summary": {
                    "users_over_quota": sum(1 for u in users if u["quota_exceeded"]),
                    "users_near_quota": sum(1 for u in users if u["message_count"] >= 250 and not u["quota_exceeded"]),
                    "total_messages_sent": sum(u["message_count"] for u in users),
                    "avg_usage_percentage": round(sum(u["usage_percentage"] for u in users) / len(users), 1) if users else 0
                }
            }), 200
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

port = int(os.getenv("PORT", 5000))

if __name__ == "__main__":
    logger.info("🔥 STARTING HEY ALEX SMS ASSISTANT v{} 🔥".format(APP_VERSION))
    logger.info("📱 Helping people stay connected without staying online")
    logger.info(f"Whitelist: {len(WHITELIST)} numbers")
    logger.info(f"Version {APP_VERSION}: {CHANGELOG[APP_VERSION]}")
    logger.info(f"📊 Monthly SMS Limit: {MONTHLY_LIMIT} messages per 30 days")
    logger.info(f"🔍 Enhanced Intent Detection: Cultural queries, Restaurant filtering, SMS debugging")
    logger.info(f"📈 Enhanced Analytics: Delivery tracking, Cultural query monitoring, Monthly usage tracking")
    logger.info(f"📋 NEW: ClickSend contact list sync and broadcasting capabilities")
    
    if os.getenv("RENDER") or os.getenv("PRODUCTION"):
        logger.info("🚀 PRODUCTION MODE DETECTED 🚀")
        logger.info("Use: gunicorn -c gunicorn_config.py app:app")
        logger.info("This message appears because you're running app.py directly.")
        logger.info("In production, Render should use the gunicorn command instead.")
        app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
    else:
        logger.info("🔧 DEVELOPMENT MODE")
        app.run(debug=True, host="0.0.0.0", port=port)
