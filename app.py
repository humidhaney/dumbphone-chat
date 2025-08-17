from flask import Flask, request, jsonify
import requests
import os
import json
import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager
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
import stripe
import hmac
import hashlib
from urllib.parse import urlparse

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
APP_VERSION = "3.3"
CHANGELOG = {
    "3.3": "Added 'longer' command: Users can text 'longer' for detailed 3-part responses (480 chars, counts as 3 messages)",
    "3.2": "COST OPTIMIZATION: Reduced to 200 messages/month with 160-char limit for sustainable pricing ($12/month cost vs $20 revenue)",
    "3.1": "Added comprehensive admin endpoints: remove-user, reset-user, and restore-user for complete user management",
    "3.0": "MAJOR: Migrated from SQLite to PostgreSQL for persistent data storage - no more data loss on redeploys!",
    "2.9": "Increased SMS response limit to 720 characters for longer, more detailed answers",
    "2.8": "Added comprehensive admin debug endpoints for SMS testing and troubleshooting",
    "2.7": "Added complete Stripe webhook integration for automatic subscription management and user lifecycle",
}

# === Config & API Keys ===
CLICKSEND_USERNAME = os.getenv("CLICKSEND_USERNAME")
CLICKSEND_API_KEY = os.getenv("CLICKSEND_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

# PostgreSQL Database Configuration
DATABASE_URL = os.getenv("DATABASE_URL")

# Debug API key availability
logger.info(f"🔑 API Keys Status:")
logger.info(f"  CLICKSEND_USERNAME: {'✅ Set' if CLICKSEND_USERNAME else '❌ Missing'}")
logger.info(f"  CLICKSEND_API_KEY: {'✅ Set' if CLICKSEND_API_KEY else '❌ Missing'}")
logger.info(f"  ANTHROPIC_API_KEY: {'✅ Set' if ANTHROPIC_API_KEY else '❌ Missing'}")
logger.info(f"  SERPAPI_API_KEY: {'✅ Set' if SERPAPI_API_KEY else '❌ Missing'}")
logger.info(f"  DATABASE_URL: {'✅ Set' if DATABASE_URL else '❌ Missing'}")

if not DATABASE_URL:
    logger.error("🚨 DATABASE_URL not found! PostgreSQL connection required.")
    raise Exception("DATABASE_URL environment variable must be set for PostgreSQL connection")

# Parse DATABASE_URL to show connection info (without password)
try:
    parsed = urlparse(DATABASE_URL)
    logger.info(f"🗄️ PostgreSQL: {parsed.hostname}:{parsed.port}/{parsed.path[1:]} (user: {parsed.username})")
except Exception as e:
    logger.error(f"Error parsing DATABASE_URL: {e}")

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    logger.info("✅ Stripe API initialized successfully")

# Initialize Anthropic client
anthropic_client = None
if ANTHROPIC_API_KEY:
    try:
        import anthropic as anthropic_lib
        anthropic_lib.api_key = ANTHROPIC_API_KEY
        anthropic_client = anthropic_lib
        logger.info("✅ Anthropic client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Anthropic: {e}")

WHITELIST_FILE = "whitelist.txt"
USAGE_FILE = "usage.json"
MONTHLY_LIMIT = 200
RESET_DAYS = 30

# SMS Response Limits
MAX_SMS_LENGTH = 160        # Standard response (1 SMS part)
LONGER_SMS_LENGTH = 480     # "Longer" response (3 SMS parts)
CLICKSEND_MAX_LENGTH = 1600

# WELCOME MESSAGE
WELCOME_MSG = (
    "Hey there! 🌟 I'm Alex - think of me as your personal research assistant who lives in your texts. "
    "I'm great at finding: ✓ Weather & forecasts ✓ Restaurant info & hours ✓ Local business details "
    "✓ Current news & headlines No apps, no browsing - just text me your question and I'll handle the rest! "
    "Try asking \"weather today\" to get started."
)

# ONBOARDING MESSAGES
ONBOARDING_NAME_MSG = (
    "🎉 Welcome to Hey Alex! I'm your personal SMS research assistant. "
    "Before we start, I need to get to know you better. What's your first name?"
)

ONBOARDING_LOCATION_MSG = (
    "Nice to meet you, {name}! 👋 Now, what's your city or zip code? "
    "This helps me give you local weather, restaurants, and business info."
)

ONBOARDING_COMPLETE_MSG = (
    "Perfect! You're all set up, {name}! 🌟 I can now help you with personalized local info. "
    "You get 200 messages per month. Try asking \"weather today\" to start!"
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

# === PostgreSQL Connection Manager ===
@contextmanager
def get_db_connection():
    """Context manager for PostgreSQL connections"""
    conn = None
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        yield conn
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Database connection error: {e}")
        raise
    finally:
        if conn:
            conn.close()

# === Helper Functions ===
def normalize_phone_number(phone):
    """Normalize phone number to consistent format"""
    if not phone:
        return None
    
    digits_only = re.sub(r'\D', '', phone)
    
    if len(digits_only) == 10:
        digits_only = '1' + digits_only
    
    if len(digits_only) == 11 and digits_only.startswith('1'):
        return '+' + digits_only
    
    if phone.startswith('+'):
        return phone
    
    return '+' + digits_only

def truncate_response(response_msg, max_length=MAX_SMS_LENGTH):
    """Intelligently truncate response to fit SMS limits"""
    if len(response_msg) <= max_length:
        return response_msg
    
    truncated = response_msg[:max_length - 3]
    
    sentence_ends = ['.', '!', '?']
    last_sentence_end = -1
    
    for end_char in sentence_ends:
        pos = truncated.rfind(end_char)
        if pos > last_sentence_end and pos > max_length * 0.7:
            last_sentence_end = pos
    
    if last_sentence_end > 0:
        return truncated[:last_sentence_end + 1]
    else:
        last_space = truncated.rfind(' ')
        if last_space > max_length * 0.8:
            return truncated[:last_space] + "..."
        else:
            return truncated + "..."

# === Database Initialization ===
def init_db():
    try:
        logger.info(f"🗄️ Initializing PostgreSQL database")
        
        with get_db_connection() as conn:
            with conn.cursor() as c:
                
                # Check existing tables
                c.execute("""
                    SELECT table_name FROM information_schema.tables 
                    WHERE table_schema = 'public'
                """)
                existing_tables = [row['table_name'] for row in c.fetchall()]
                logger.info(f"📊 Existing tables: {existing_tables}")
                
                # Messages table
                c.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    role VARCHAR(20) NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    intent_type VARCHAR(50),
                    response_time_ms INTEGER
                );
                """)
                
                c.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_phone_ts 
                ON messages(phone, ts DESC);
                """)
                
                # User profiles table
                c.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) UNIQUE NOT NULL,
                    first_name VARCHAR(100),
                    location VARCHAR(200),
                    onboarding_step INTEGER DEFAULT 0,
                    onboarding_completed BOOLEAN DEFAULT FALSE,
                    stripe_customer_id VARCHAR(100),
                    subscription_status VARCHAR(50),
                    subscription_id VARCHAR(100),
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                # Other tables
                c.execute("""
                CREATE TABLE IF NOT EXISTS onboarding_log (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    step INTEGER NOT NULL,
                    response TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                c.execute("""
                CREATE TABLE IF NOT EXISTS whitelist_events (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    action VARCHAR(20) NOT NULL CHECK(action IN ('added','removed')),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    source VARCHAR(50) DEFAULT 'manual'
                );
                """)
                
                c.execute("""
                CREATE TABLE IF NOT EXISTS sms_delivery_log (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    message_content TEXT NOT NULL,
                    clicksend_response TEXT,
                    delivery_status VARCHAR(50),
                    message_id VARCHAR(100),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                c.execute("""
                CREATE TABLE IF NOT EXISTS usage_analytics (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    intent_type VARCHAR(50),
                    success BOOLEAN,
                    response_time_ms INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                c.execute("""
                CREATE TABLE IF NOT EXISTS monthly_sms_usage (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    message_count INTEGER DEFAULT 1,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    last_message_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    quota_warnings_sent INTEGER DEFAULT 0,
                    quota_exceeded BOOLEAN DEFAULT FALSE,
                    UNIQUE(phone, period_start)
                );
                """)
                
                c.execute("""
                CREATE TABLE IF NOT EXISTS subscription_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(100) NOT NULL,
                    stripe_customer_id VARCHAR(100),
                    subscription_id VARCHAR(100),
                    phone VARCHAR(20),
                    status VARCHAR(50),
                    event_data TEXT,
                    processed BOOLEAN DEFAULT TRUE,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                conn.commit()
                logger.info(f"📊 All PostgreSQL tables created/verified")
                
                # Check for existing data
                c.execute("SELECT COUNT(*) as count FROM user_profiles")
                user_count = c.fetchone()['count']
                
                c.execute("SELECT COUNT(*) as count FROM messages")  
                message_count = c.fetchone()['count']
                
                logger.info(f"📊 PostgreSQL database initialized successfully")
                logger.info(f"📊 Found {user_count} user profiles and {message_count} messages")
                
    except Exception as e:
        logger.error(f"💥 PostgreSQL database initialization error: {e}")
        raise

# === User Profile Functions ===
def get_user_profile(phone):
    """Get user profile and onboarding status"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT first_name, location, onboarding_step, onboarding_completed, 
                           stripe_customer_id, subscription_status
                    FROM user_profiles
                    WHERE phone = %s
                """, (phone,))
                result = c.fetchone()
                
                if result:
                    return {
                        'first_name': result['first_name'],
                        'location': result['location'],
                        'onboarding_step': result['onboarding_step'],
                        'onboarding_completed': bool(result['onboarding_completed']),
                        'stripe_customer_id': result['stripe_customer_id'],
                        'subscription_status': result['subscription_status']
                    }
                else:
                    return None
    except Exception as e:
        logger.error(f"Error getting user profile for {phone}: {e}")
        return None

def create_user_profile(phone):
    """Create new user profile for onboarding"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO user_profiles (phone, onboarding_step, onboarding_completed)
                    VALUES (%s, 1, FALSE)
                    ON CONFLICT (phone) DO NOTHING
                """, (phone,))
                conn.commit()
                logger.info(f"📝 Created user profile for {phone}")
                return True
    except Exception as e:
        logger.error(f"Error creating user profile for {phone}: {e}")
        return False

def update_user_profile(phone, first_name=None, location=None, onboarding_step=None, 
                       onboarding_completed=None, stripe_customer_id=None, 
                       subscription_status=None, subscription_id=None):
    """Update user profile information"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                
                update_parts = []
                params = []
                
                if first_name is not None:
                    update_parts.append("first_name = %s")
                    params.append(first_name)
                
                if location is not None:
                    update_parts.append("location = %s")
                    params.append(location)
                
                if onboarding_step is not None:
                    update_parts.append("onboarding_step = %s")
                    params.append(onboarding_step)
                
                if onboarding_completed is not None:
                    update_parts.append("onboarding_completed = %s")
                    params.append(onboarding_completed)
                
                if stripe_customer_id is not None:
                    update_parts.append("stripe_customer_id = %s")
                    params.append(stripe_customer_id)
                
                if subscription_status is not None:
                    update_parts.append("subscription_status = %s")
                    params.append(subscription_status)
                
                if subscription_id is not None:
                    update_parts.append("subscription_id = %s")
                    params.append(subscription_id)
                
                update_parts.append("updated_date = CURRENT_TIMESTAMP")
                params.append(phone)
                
                query = f"""
                    UPDATE user_profiles 
                    SET {', '.join(update_parts)}
                    WHERE phone = %s
                """
                
                c.execute(query, params)
                conn.commit()
                logger.info(f"📝 Updated user profile for {phone}")
                return True
    except Exception as e:
        logger.error(f"Error updating user profile for {phone}: {e}")
        return False

def is_user_onboarded(phone):
    """Check if user has completed onboarding"""
    profile = get_user_profile(phone)
    return profile and profile['onboarding_completed']

def get_user_context_for_queries(phone):
    """Get user context to personalize responses"""
    profile = get_user_profile(phone)
    if profile and profile['onboarding_completed']:
        return {
            'first_name': profile['first_name'],
            'location': profile['location'],
            'personalized': True
        }
    return {'personalized': False}

def log_onboarding_step(phone, step, response):
    """Log onboarding step response"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO onboarding_log (phone, step, response)
                    VALUES (%s, %s, %s)
                """, (phone, step, response))
                conn.commit()
    except Exception as e:
        logger.error(f"Error logging onboarding step: {e}")

def handle_onboarding_response(phone, message):
    """Handle user responses during onboarding process"""
    profile = get_user_profile(phone)
    
    if not profile:
        logger.error(f"No profile found for {phone} during onboarding")
        return "Sorry, there was an error with your profile. Please contact support."
    
    current_step = profile['onboarding_step']
    
    if current_step == 1:
        # Collecting first name
        first_name = message.strip().title()
        
        if len(first_name) < 1 or len(first_name) > 50:
            return "Please enter a valid first name."
        
        clean_name = re.sub(r"[^a-zA-Z\s\-']", "", first_name)
        if not clean_name:
            return "Please enter a valid first name using only letters."
        
        update_user_profile(phone, first_name=clean_name, onboarding_step=2)
        log_onboarding_step(phone, 1, clean_name)
        
        response = ONBOARDING_LOCATION_MSG.format(name=clean_name)
        save_message(phone, "assistant", response, "onboarding_location", 0)
        
        logger.info(f"👤 Collected name '{clean_name}' for {phone}, asking for location")
        return response
        
    elif current_step == 2:
        # Collecting location
        location = message.strip().title()
        
        if len(location) < 2 or len(location) > 100:
            return "Please enter a valid city name or zip code."
        
        update_user_profile(phone, location=location, onboarding_step=3, onboarding_completed=True)
        log_onboarding_step(phone, 2, location)
        
        updated_profile = get_user_profile(phone)
        first_name = updated_profile['first_name'] if updated_profile else "there"
        
        response = ONBOARDING_COMPLETE_MSG.format(name=first_name)
        save_message(phone, "assistant", response, "onboarding_complete", 0)
        
        logger.info(f"🎉 Completed onboarding for {phone}: {first_name} in {location}")
        return response
    
    else:
        logger.warning(f"Unexpected onboarding step {current_step} for {phone}")
        return "There was an error with your setup. You can now ask me questions!"

# === Whitelist Management ===
def load_whitelist():
    try:
        with open(WHITELIST_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

def log_whitelist_event(phone, action, source='manual'):
    """Log whitelist addition/removal events"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO whitelist_events (phone, action, source)
                    VALUES (%s, %s, %s)
                """, (phone, action, source))
                conn.commit()
                logger.info(f"📋 Logged whitelist event: {action} for {phone} (source: {source})")
    except Exception as e:
        logger.error(f"Error logging whitelist event: {e}")

def add_to_whitelist(phone, send_welcome=True, source='manual'):
    """Enhanced whitelist addition with automatic welcome message and onboarding"""
    if not phone:
        return False
        
    phone = normalize_phone_number(phone)
    wl = load_whitelist()
    
    is_new_user = phone not in wl
    
    if is_new_user:
        try:
            with open(WHITELIST_FILE, "a") as f:
                f.write(phone + "\n")
            
            log_whitelist_event(phone, "added", source)
            logger.info(f"📱 Added new user {phone} to whitelist (source: {source})")
            
            create_user_profile(phone)
            
            if send_welcome:
                try:
                    result = send_sms(phone, ONBOARDING_NAME_MSG, bypass_quota=True)
                    if "error" not in result:
                        logger.info(f"🎉 Onboarding started for new user {phone}")
                        save_message(phone, "assistant", ONBOARDING_NAME_MSG, "onboarding_start", 0)
                    else:
                        logger.error(f"Failed to send onboarding SMS to {phone}: {result['error']}")
                except Exception as sms_error:
                    logger.error(f"Failed to send onboarding SMS to {phone}: {sms_error}")
            
            return True
        except Exception as e:
            logger.error(f"Failed to add {phone} to whitelist: {e}")
            return False
    else:
        logger.info(f"📱 {phone} already in whitelist")
        return True

def remove_from_whitelist(phone, send_goodbye=False):
    """Enhanced whitelist removal with optional goodbye message"""
    if not phone:
        return False
        
    phone = normalize_phone_number(phone)
    wl = load_whitelist()
    
    if phone in wl:
        try:
            wl.remove(phone)
            with open(WHITELIST_FILE, "w") as f:
                for num in wl:
                    f.write(num + "\n")
            
            log_whitelist_event(phone, "removed")
            logger.info(f"📱 Removed {phone} from whitelist")
            
            if send_goodbye:
                goodbye_msg = "Thanks for using Hey Alex! Your subscription has been cancelled. You can resubscribe anytime at heyalex.co"
                try:
                    send_sms(phone, goodbye_msg, bypass_quota=True)
                    logger.info(f"👋 Goodbye message sent to {phone}")
                except Exception as sms_error:
                    logger.error(f"Failed to send goodbye SMS to {phone}: {sms_error}")
            
            return True
        except Exception as e:
            logger.error(f"Failed to remove {phone} from whitelist: {e}")
            return False
    else:
        logger.info(f"📱 {phone} not in whitelist")
        return True

# === SMS Functions ===
def send_sms(to_number, message, bypass_quota=False):
    if not CLICKSEND_USERNAME or not CLICKSEND_API_KEY:
        logger.error("ClickSend credentials not configured")
        return {"error": "SMS service not configured"}
    
    url = "https://rest.clicksend.com/v3/sms/send"
    headers = {"Content-Type": "application/json"}
    
    if len(message) > CLICKSEND_MAX_LENGTH:
        message = message[:CLICKSEND_MAX_LENGTH - 3] + "..."
        logger.warning(f"📏 Message truncated to ClickSend limit: {CLICKSEND_MAX_LENGTH} chars")
    
    payload = {"messages": [{
        "source": "python",
        "body": message,
        "to": to_number,
        "custom_string": "alex_reply"
    }]}
    
    try:
        logger.info(f"📤 Sending SMS to {to_number}: {message[:50]}... (Length: {len(message)} chars)")
        
        resp = requests.post(
            url,
            auth=(CLICKSEND_USERNAME, CLICKSEND_API_KEY),
            headers=headers,
            json=payload,
            timeout=15
        )
        
        result = resp.json()
        
        if resp.status_code == 200:
            if "data" in result and "messages" in result["data"]:
                messages = result["data"]["messages"]
                if messages:
                    msg_status = messages[0].get("status")
                    msg_id = messages[0].get("message_id")
                    msg_parts = messages[0].get("message_parts", 1)
                    
                    logger.info(f"✅ SMS queued successfully to {to_number} ({msg_parts} parts)")
                    log_sms_delivery(to_number, message, result, msg_status, msg_id)
            
            return result
        else:
            logger.error(f"❌ ClickSend API Error {resp.status_code}: {result}")
            return {"error": f"ClickSend API error: {resp.status_code}"}
            
    except Exception as e:
        logger.error(f"💥 SMS Exception for {to_number}: {e}")
        return {"error": f"SMS send failed: {str(e)}"}

def log_sms_delivery(phone, message_content, clicksend_response, delivery_status, message_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO sms_delivery_log (phone, message_content, clicksend_response, delivery_status, message_id)
                    VALUES (%s, %s, %s, %s, %s)
                """, (phone, message_content, json.dumps(clicksend_response), delivery_status, message_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Error logging SMS delivery: {e}")

def save_message(phone, role, content, intent_type=None, response_time_ms=None):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO messages (phone, role, content, intent_type, response_time_ms) 
                    VALUES (%s, %s, %s, %s, %s)
                """, (phone, role, content, intent_type, response_time_ms))
                conn.commit()
    except Exception as e:
        logger.error(f"Error saving message: {e}")

def load_history(phone, limit=4):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT role, content
                    FROM messages
                    WHERE phone = %s
                    ORDER BY id DESC
                    LIMIT %s
                """, (phone, limit))
                rows = c.fetchall()
                return [{"role": row['role'], "content": row['content']} for row in reversed(rows)]
    except Exception as e:
        logger.error(f"Error loading history: {e}")
        return []

def log_usage_analytics(phone, intent_type, success, response_time_ms):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO usage_analytics (phone, intent_type, success, response_time_ms)
                    VALUES (%s, %s, %s, %s)
                """, (phone, intent_type, success, response_time_ms))
                conn.commit()
    except Exception as e:
        logger.error(f"Error logging usage analytics: {e}")

# === Content Filter ===
class ContentFilter:
    def __init__(self):
        self.spam_keywords = {
            'promotional': [
                'free money', 'win cash', 'winner selected', 'claim prize', 
                'congratulations you won', 'act now', 'limited time offer',
                'click here to claim', 'urgent response required'
            ]
        }
        
        self.question_patterns = [
            r'\b(what|who|when|where|why|how|do|does|is|are|can|will|would|should)\b.*\?',
            r'\b(free will|philosophy|philosophical|ethics|moral|meaning)\b',
            r'\b(illusion|reality|consciousness|existence|purpose)\b'
        ]
    
    def is_spam(self, text: str) -> tuple[bool, str]:
        text_lower = text.lower().strip()
        
        for pattern in self.question_patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return False, ""
        
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
        
        short_allowed = ['hi', 'hey', 'hello', 'help', 'yes', 'no', 'ok', 'thanks', 'stop', 'start']
        if text.lower() in short_allowed:
            return True, ""
        
        is_spam, spam_reason = self.is_spam(text)
        if is_spam:
            return False, spam_reason
        
        return True, ""

content_filter = ContentFilter()

# === Intent Detection ===
@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]
    confidence: float = 1.0

def detect_weather_intent(text: str) -> Optional[IntentResult]:
    weather_patterns = [
        r'\bweather\b',
        r'\btemperature\b',
        r'\bforecast\b',
        r'\brain\b',
        r'\bsnow\b',
        r'\bsunny\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in weather_patterns):
        return IntentResult("weather", {})
    return None

def detect_longer_request(text: str) -> bool:
    """Check if user is requesting a longer response"""
    longer_keywords = ['longer', 'more info', 'more details', 'expand', 'tell me more', 'full details']
    text_lower = text.lower().strip()
    return any(keyword in text_lower for keyword in longer_keywords)

def detect_intent(text: str, phone: str = None) -> Optional[IntentResult]:
    return detect_weather_intent(text)

# === Web Search ===
def web_search(q, num=3, search_type="general"):
    if not SERPAPI_API_KEY:
        logger.warning("❌ SERPAPI_API_KEY not configured - search unavailable")
        return "I'd love to search for that information, but my search service isn't configured right now. Please contact support."
    
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
    
    try:
        logger.info(f"🔍 Searching: {q}")
        r = requests.get(url, params=params, timeout=15)
        
        if r.status_code != 200:
            logger.error(f"❌ Search API error: {r.status_code}")
            return f"Search temporarily unavailable. Try again later."
            
        data = r.json()
        logger.info(f"✅ Search response received")
        
        if 'error' in data:
            logger.error(f"❌ SerpAPI error: {data['error']}")
            return "Search service error. Please try again later."
        
    except Exception as e:
        logger.error(f"💥 Search exception: {e}")
        return "Search service temporarily unavailable. Try again later."

    org = data.get("organic_results", [])
    if org:
        top = org[0]
        title = top.get("title", "")
        snippet = top.get("snippet", "")
        
        result = f"{title}"
        if snippet:
            result += f" — {snippet}"
        
        return truncate_response(result, MAX_SMS_LENGTH)
    
    return f"No results found for '{q}'."

# === Claude Integration ===
def ask_claude(phone, user_msg):
    start_time = time.time()
    
    if not anthropic_client:
        logger.warning("❌ ANTHROPIC_API_KEY not configured - Claude unavailable")
        return "I'd love to help with that question, but my AI service isn't configured right now. Let me try to search for that information instead."
    
    try:
        history = load_history(phone, limit=4)
        
        system_context = f"""You are Alex, a helpful SMS assistant that helps people stay connected to information without spending time online. 

IMPORTANT GUIDELINES:
- Keep responses under {MAX_SMS_LENGTH} characters (160 chars = 1 SMS part) unless specifically asked for longer
- If this is a "longer" request, provide a comprehensive response up to {LONGER_SMS_LENGTH} characters (3 SMS parts)
- Be concise but helpful - provide key information quickly
- Be friendly and conversational but brief
- You DO have access to web search capabilities
- For specific information requests, respond with "Let me search for [specific topic]" 
- Never make up detailed information - always offer to search for accurate, current details
- Prioritize the most important information first in short responses
- End standard responses with "Text 'longer' for more details" when relevant"""
        
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
                "max_tokens": 250,
                "temperature": 0.3,
                "system": system_context,
                "messages": messages
            }
            
            logger.info(f"🤖 Calling Claude API")
            
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=data,
                timeout=15
            )
            
            logger.info(f"📡 Claude API response status: {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                reply = result.get("content", [{}])[0].get("text", "").strip()
                logger.info(f"✅ Claude responded successfully (length: {len(reply)} chars)")
            else:
                logger.error(f"❌ Claude API error: {response.status_code}")
                raise Exception(f"API call failed with status {response.status_code}")
                
        except Exception as e:
            logger.error(f"💥 Claude API exception: {e}")
            return "I'm having trouble with my AI service right now. Let me try to search for that information instead."
        
        if not reply:
            logger.warning("⚠️ Claude returned empty response")
            return "I'm having trouble processing that question. Let me try to search for that information instead."
        
        # Check if Claude suggests a search
        search_suggestion_patterns = [
            r'let me search for (.+?)(?:\.|$)',
            r'i can search for (.+?)(?:\.|$)',
            r'search for (.+?)(?:\.|$)'
        ]
        
        for pattern in search_suggestion_patterns:
            match = re.search(pattern, reply, re.I)
            if match:
                search_term = match.group(1).strip()
                logger.info(f"🔍 Claude suggested search for: {search_term}")
                search_result = web_search(search_term, search_type="general")
                return search_result
        
        truncated_reply = truncate_response(reply, MAX_SMS_LENGTH)
        
        if len(truncated_reply) < len(reply):
            logger.info(f"📏 Claude response truncated from {len(reply)} to {len(truncated_reply)} chars")
            
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(phone, "claude_chat", True, response_time)
        
        return truncated_reply
        
    except Exception as e:
        logger.error(f"💥 Claude integration error for {phone}: {e}")
        return "I'm having trouble processing that question. Let me try to search for that information instead."

# === Stripe Functions ===
def log_stripe_event(event_type, customer_id, subscription_id, phone, status, additional_data=None):
    """Log Stripe webhook events for debugging"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO subscription_events (event_type, stripe_customer_id, subscription_id, phone, status, event_data)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (event_type, customer_id, subscription_id, phone, status, json.dumps(additional_data or {})))
                conn.commit()
                logger.info(f"📋 Logged Stripe event: {event_type} for customer {customer_id}")
    except Exception as e:
        logger.error(f"Error logging Stripe event: {e}")

def extract_phone_from_stripe_metadata(metadata):
    """Extract phone number from Stripe customer metadata"""
    phone_fields = ['phone', 'phone_number', 'mobile', 'cell', 'sms_number']
    
    for field in phone_fields:
        if field in metadata and metadata[field]:
            return normalize_phone_number(metadata[field])
    
    return None

def handle_subscription_created(subscription):
    """Handle new subscription creation"""
    customer_id = subscription['customer']
    subscription_id = subscription['id']
    status = subscription['status']
    
    logger.info(f"🎉 New subscription created: {subscription_id} for customer {customer_id}")
    
    try:
        customer = stripe.Customer.retrieve(customer_id)
        
        phone = extract_phone_from_stripe_metadata(customer.get('metadata', {}))
        
        if not phone and customer.get('phone'):
            phone = normalize_phone_number(customer['phone'])
        
        if phone:
            update_user_profile(
                phone, 
                stripe_customer_id=customer_id,
                subscription_status=status,
                subscription_id=subscription_id
            )
            
            add_to_whitelist(phone, send_welcome=True, source='stripe_subscription')
            log_stripe_event('subscription_created', customer_id, subscription_id, phone, status)
            
            logger.info(f"✅ Subscription activated for {phone}")
        else:
            logger.warning(f"⚠️ No phone number found for customer {customer_id}")
            log_stripe_event('subscription_created', customer_id, subscription_id, None, status, 
                           {'error': 'No phone number found'})
        
    except Exception as e:
        logger.error(f"❌ Error handling subscription creation: {e}")
        log_stripe_event('subscription_created', customer_id, subscription_id, None, 'error', 
                        {'error': str(e)})

def handle_subscription_deleted(subscription):
    """Handle subscription cancellation"""
    customer_id = subscription['customer']
    subscription_id = subscription['id']
    
    logger.info(f"❌ Subscription cancelled: {subscription_id} for customer {customer_id}")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT phone FROM user_profiles 
                    WHERE stripe_customer_id = %s
                """, (customer_id,))
                result = c.fetchone()
                
                if result:
                    phone = result['phone']
                    
                    update_user_profile(phone, subscription_status='cancelled')
                    remove_from_whitelist(phone, send_goodbye=True)
                    log_stripe_event('subscription_deleted', customer_id, subscription_id, phone, 'cancelled')
                    
                    logger.info(f"✅ Subscription cancelled for {phone}")
                else:
                    logger.warning(f"⚠️ No user found for customer {customer_id}")
                    log_stripe_event('subscription_deleted', customer_id, subscription_id, None, 'cancelled',
                                   {'error': 'No user found'})
        
    except Exception as e:
        logger.error(f"❌ Error handling subscription deletion: {e}")
        log_stripe_event('subscription_deleted', customer_id, subscription_id, None, 'error',
                        {'error': str(e)})

# === ADMIN ENDPOINTS ===
@app.route('/admin/remove-user', methods=['POST'])
def admin_remove_user():
    """Admin endpoint to completely remove a user and their data"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        
        if not phone:
            return jsonify({"error": "Phone number required"}), 400
        
        phone = normalize_phone_number(phone)
        
        actions_taken = []
        
        # Remove from whitelist
        success = remove_from_whitelist(phone, send_goodbye=True)
        if success:
            actions_taken.append("Removed from whitelist")
        
        # Remove user profile and related data
        try:
            with get_db_connection() as conn:
                with conn.cursor() as c:
                    
                    # Get user info before deletion for logging
                    c.execute("SELECT first_name, location FROM user_profiles WHERE phone = %s", (phone,))
                    user_info = c.fetchone()
                    
                    # Delete user profile
                    c.execute("DELETE FROM user_profiles WHERE phone = %s", (phone,))
                    profile_deleted = c.rowcount
                    
                    # Delete messages
                    c.execute("DELETE FROM messages WHERE phone = %s", (phone,))
                    messages_deleted = c.rowcount
                    
                    # Delete onboarding log
                    c.execute("DELETE FROM onboarding_log WHERE phone = %s", (phone,))
                    onboarding_deleted = c.rowcount
                    
                    # Delete usage analytics
                    c.execute("DELETE FROM usage_analytics WHERE phone = %s", (phone,))
                    analytics_deleted = c.rowcount
                    
                    # Delete SMS delivery log
                    c.execute("DELETE FROM sms_delivery_log WHERE phone = %s", (phone,))
                    sms_log_deleted = c.rowcount
                    
                    # Delete monthly usage
                    c.execute("DELETE FROM monthly_sms_usage WHERE phone = %s", (phone,))
                    usage_deleted = c.rowcount
                    
                    # Delete whitelist events
                    c.execute("DELETE FROM whitelist_events WHERE phone = %s", (phone,))
                    whitelist_events_deleted = c.rowcount
                    
                    # Delete subscription events (keep for audit trail but mark as deleted)
                    c.execute("""
                        UPDATE subscription_events 
                        SET status = 'user_deleted', processed = TRUE
                        WHERE phone = %s
                    """, (phone,))
                    subscription_events_updated = c.rowcount
                    
                    conn.commit()
                    
                    if profile_deleted > 0:
                        actions_taken.append(f"Deleted user profile")
                    if messages_deleted > 0:
                        actions_taken.append(f"Deleted {messages_deleted} messages")
                    if onboarding_deleted > 0:
                        actions_taken.append(f"Deleted {onboarding_deleted} onboarding logs")
                    if analytics_deleted > 0:
                        actions_taken.append(f"Deleted {analytics_deleted} analytics records")
                    if sms_log_deleted > 0:
                        actions_taken.append(f"Deleted {sms_log_deleted} SMS delivery logs")
                    if usage_deleted > 0:
                        actions_taken.append(f"Deleted {usage_deleted} usage records")
                    if whitelist_events_deleted > 0:
                        actions_taken.append(f"Deleted {whitelist_events_deleted} whitelist events")
                    if subscription_events_updated > 0:
                        actions_taken.append(f"Updated {subscription_events_updated} subscription events")
                    
                    # Log the removal
                    c.execute("""
                        INSERT INTO onboarding_log (phone, step, response, timestamp)
                        VALUES (%s, -999, %s, CURRENT_TIMESTAMP)
                    """, (phone, f"REMOVED: User and all data deleted by admin"))
                    
                    conn.commit()
                    actions_taken.append("Logged user removal")
                    
                    user_name = user_info['first_name'] if user_info else "Unknown"
                    user_location = user_info['location'] if user_info else "Unknown"
                    
        except Exception as db_error:
            logger.error(f"Database error removing user: {db_error}")
            return jsonify({"error": f"Database error: {str(db_error)}"}), 500
        
        logger.info(f"🗑️ Completely removed user: {phone} ({user_name} from {user_location})")
        
        return jsonify({
            "success": True,
            "message": f"Completely removed user {phone} and all associated data",
            "actions_taken": actions_taken,
            "user_info": {
                "phone": phone,
                "name": user_name,
                "location": user_location
            }
        })
        
    except Exception as e:
        logger.error(f"Error removing user: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/reset-user', methods=['POST'])
def admin_reset_user():
    """Admin endpoint to reset user's usage quotas and clear message history"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        
        if not phone:
            return jsonify({"error": "Phone number required"}), 400
        
        phone = normalize_phone_number(phone)
        
        actions_taken = []
        
        try:
            with get_db_connection() as conn:
                with conn.cursor() as c:
                    
                    # Get user info
                    c.execute("SELECT first_name, location FROM user_profiles WHERE phone = %s", (phone,))
                    user_info = c.fetchone()
                    
                    if not user_info:
                        return jsonify({"error": "User not found"}), 404
                    
                    # Reset monthly usage
                    c.execute("DELETE FROM monthly_sms_usage WHERE phone = %s", (phone,))
                    usage_reset = c.rowcount
                    
                    # Clear message history (optional - keep for debugging)
                    c.execute("DELETE FROM messages WHERE phone = %s", (phone,))
                    messages_cleared = c.rowcount
                    
                    # Clear usage analytics (optional)
                    c.execute("DELETE FROM usage_analytics WHERE phone = %s", (phone,))
                    analytics_cleared = c.rowcount
                    
                    conn.commit()
                    
                    if usage_reset > 0:
                        actions_taken.append(f"Reset monthly usage quota")
                    if messages_cleared > 0:
                        actions_taken.append(f"Cleared {messages_cleared} message history")
                    if analytics_cleared > 0:
                        actions_taken.append(f"Cleared {analytics_cleared} analytics records")
                    
                    # Log the reset
                    c.execute("""
                        INSERT INTO onboarding_log (phone, step, response, timestamp)
                        VALUES (%s, 998, %s, CURRENT_TIMESTAMP)
                    """, (phone, f"RESET: Usage quota and history reset by admin"))
                    
                    conn.commit()
                    actions_taken.append("Logged user reset")
                    
        except Exception as db_error:
            logger.error(f"Database error resetting user: {db_error}")
            return jsonify({"error": f"Database error: {str(db_error)}"}), 500
        
        # Send reset confirmation
        reset_msg = f"Hi {user_info['first_name']}! Your Hey Alex account has been reset. Your message quota is refreshed and you're ready to go!"
        
        try:
            result = send_sms(phone, reset_msg, bypass_quota=True)
            if "error" not in result:
                actions_taken.append("Sent reset confirmation SMS")
                save_message(phone, "assistant", reset_msg, "user_reset", 0)
            else:
                actions_taken.append(f"Failed to send SMS: {result['error']}")
        except Exception as sms_error:
            actions_taken.append(f"SMS error: {str(sms_error)}")
        
        logger.info(f"🔄 Reset user: {phone} ({user_info['first_name']})")
        
        return jsonify({
            "success": True,
            "message": f"Reset user {phone} - quota refreshed and history cleared",
            "actions_taken": actions_taken,
            "user_info": {
                "phone": phone,
                "name": user_info['first_name'],
                "location": user_info['location']
            }
        })
        
    except Exception as e:
        logger.error(f"Error resetting user: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/check-user', methods=['POST'])
def admin_check_user():
    """Admin endpoint to check user status and recent activity"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        
        if not phone:
            return jsonify({"error": "Phone number required"}), 400
        
        phone = normalize_phone_number(phone)
        
        user_info = {}
        
        # Check if in whitelist
        whitelist = load_whitelist()
        user_info['in_whitelist'] = phone in whitelist
        
        # Get user profile
        profile = get_user_profile(phone)
        user_info['profile'] = profile
        
        # Get recent messages
        try:
            with get_db_connection() as conn:
                with conn.cursor() as c:
                    c.execute("""
                        SELECT role, content, intent_type, ts
                        FROM messages
                        WHERE phone = %s
                        ORDER BY id DESC
                        LIMIT 5
                    """, (phone,))
                    messages = c.fetchall()
                    user_info['recent_messages'] = [dict(msg) for msg in messages]
                    
                    # Get recent SMS delivery logs
                    c.execute("""
                        SELECT message_content, delivery_status, message_id, timestamp
                        FROM sms_delivery_log
                        WHERE phone = %s
                        ORDER BY id DESC
                        LIMIT 3
                    """, (phone,))
                    sms_logs = c.fetchall()
                    user_info['recent_sms_delivery'] = [dict(log) for log in sms_logs]
                    
                    # Get subscription events
                    c.execute("""
                        SELECT event_type, status, timestamp
                        FROM subscription_events
                        WHERE phone = %s
                        ORDER BY id DESC
                        LIMIT 3
                    """, (phone,))
                    events = c.fetchall()
                    user_info['subscription_events'] = [dict(event) for event in events]
                    
        except Exception as db_error:
            logger.error(f"Database error checking user: {db_error}")
            user_info['db_error'] = str(db_error)
        
        return jsonify({
            "success": True,
            "phone": phone,
            "user_info": user_info
        })
        
    except Exception as e:
        logger.error(f"Error checking user: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/restore-user', methods=['POST'])
def admin_restore_user():
    """Admin endpoint to restore a user's complete profile"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        first_name = data.get('first_name')
        location = data.get('location')
        stripe_customer_id = data.get('stripe_customer_id')
        subscription_status = data.get('subscription_status', 'active')
        
        if not phone:
            return jsonify({"error": "Phone number required"}), 400
        
        if not first_name or not location:
            return jsonify({"error": "first_name and location are required"}), 400
        
        phone = normalize_phone_number(phone)
        
        actions_taken = []
        
        # Add to whitelist
        success = add_to_whitelist(phone, send_welcome=False, source='admin_restore')
        if success:
            actions_taken.append("Added to whitelist")
        
        # Create/update user profile
        try:
            with get_db_connection() as conn:
                with conn.cursor() as c:
                    
                    c.execute("DELETE FROM user_profiles WHERE phone = %s", (phone,))
                    
                    c.execute("""
                        INSERT INTO user_profiles 
                        (phone, first_name, location, onboarding_step, onboarding_completed, 
                         stripe_customer_id, subscription_status, created_date, updated_date)
                        VALUES (%s, %s, %s, 3, TRUE, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, (phone, first_name, location, stripe_customer_id, subscription_status))
                    
                    conn.commit()
                    actions_taken.append("Created complete user profile")
                    
                    c.execute("""
                        INSERT INTO onboarding_log (phone, step, response, timestamp)
                        VALUES (%s, 999, %s, CURRENT_TIMESTAMP)
                    """, (phone, f"RESTORED: {first_name} in {location}"))
                    
                    conn.commit()
                    actions_taken.append("Logged profile restoration")
                    
        except Exception as db_error:
            logger.error(f"Database error restoring user: {db_error}")
            return jsonify({"error": f"Database error: {str(db_error)}"}), 500
        
        # Send confirmation SMS
        confirmation_msg = f"Hi {first_name}! Your Hey Alex account has been restored. You're all set up in {location}. Ask me anything!"
        
        try:
            result = send_sms(phone, confirmation_msg, bypass_quota=True)
            if "error" not in result:
                actions_taken.append("Sent confirmation SMS")
                save_message(phone, "assistant", confirmation_msg, "profile_restored", 0)
            else:
                actions_taken.append(f"Failed to send SMS: {result['error']}")
        except Exception as sms_error:
            actions_taken.append(f"SMS error: {str(sms_error)}")
        
        logger.info(f"👤 Restored user profile: {first_name} ({phone}) in {location}")
        
        return jsonify({
            "success": True,
            "message": f"Restored user profile for {first_name} ({phone})",
            "actions_taken": actions_taken,
            "profile": {
                "phone": phone,
                "first_name": first_name,
                "location": location,
                "onboarding_completed": True,
                "stripe_customer_id": stripe_customer_id,
                "subscription_status": subscription_status
            }
        })
        
    except Exception as e:
        logger.error(f"Error restoring user: {e}")
        return jsonify({"error": str(e)}), 500

# === STRIPE WEBHOOK ===
@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events"""
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    
    if not sig_header:
        logger.error("Missing Stripe signature header")
        return jsonify({'error': 'Missing signature header'}), 400
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
        
        logger.info(f"📨 Received Stripe webhook: {event['type']}")
        
        if event['type'] == 'customer.subscription.created':
            handle_subscription_created(event['data']['object'])
        
        elif event['type'] == 'customer.subscription.deleted':
            handle_subscription_deleted(event['data']['object'])
        
        elif event['type'] == 'customer.subscription.updated':
            subscription = event['data']['object']
            logger.info(f"📝 Subscription updated: {subscription['id']} - Status: {subscription['status']}")
        
        elif event['type'] == 'invoice.payment_failed':
            invoice = event['data']['object']
            logger.warning(f"💳 Payment failed for customer: {invoice['customer']}")
        
        elif event['type'] == 'invoice.payment_succeeded':
            invoice = event['data']['object']
            logger.info(f"✅ Payment succeeded for customer: {invoice['customer']}")
        
        else:
            logger.info(f"ℹ️ Unhandled Stripe event type: {event['type']}")
        
        return jsonify({'status': 'success'}), 200
        
    except ValueError as e:
        logger.error(f"❌ Invalid payload: {e}")
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"❌ Invalid signature: {e}")
        return jsonify({'error': 'Invalid signature'}), 400
    except Exception as e:
        logger.error(f"💥 Error processing Stripe webhook: {e}")
        return jsonify({'error': 'Webhook processing failed'}), 500

# === MAIN SMS WEBHOOK ===
@app.route("/sms", methods=["POST"])
@handle_errors  
def sms_webhook():
    start_time = time.time()
    
    sender = request.form.get("from")
    body = (request.form.get("body") or "").strip()
    
    logger.info(f"📱 SMS received from {sender}: {repr(body)}")
    
    if not sender:
        return jsonify({"error": "Missing 'from' field"}), 400
    
    if not body:
        return jsonify({"message": "Empty message received"}), 200
    
    # Check whitelist
    whitelist = load_whitelist()
    if sender not in whitelist:
        logger.warning(f"🚫 Unauthorized sender: {sender}")
        return jsonify({"message": "Unauthorized sender"}), 403
    
    # Content filtering
    is_valid, filter_reason = content_filter.is_valid_query(body)
    if not is_valid:
        logger.warning(f"🚫 Content filtered for {sender}: {filter_reason}")
        return jsonify({"message": "Content filtered"}), 400
    
    # Save user message
    save_message(sender, "user", body)
    
    # Handle special commands
    if body.lower() in ['stop', 'quit', 'unsubscribe']:
        response_msg = "You've been unsubscribed from Hey Alex. Text START to resume service."
        try:
            send_sms(sender, response_msg, bypass_quota=True)
            return jsonify({"message": "Unsubscribe processed"}), 200
        except Exception as e:
            logger.error(f"Failed to send unsubscribe message: {e}")
            return jsonify({"error": "Failed to process unsubscribe"}), 500
    
    if body.lower() in ['start', 'subscribe', 'resume']:
        if is_user_onboarded(sender):
            response_msg = WELCOME_MSG
        else:
            create_user_profile(sender)
            response_msg = ONBOARDING_NAME_MSG
        
        try:
            send_sms(sender, response_msg, bypass_quota=True)
            save_message(sender, "assistant", response_msg, "start_command", 0)
            return jsonify({"message": "Start message sent"}), 200
        except Exception as e:
            logger.error(f"Failed to send start message: {e}")
            return jsonify({"error": "Failed to send start message"}), 500
    
    # Check if user needs to complete onboarding
    profile = get_user_profile(sender)
    logger.info(f"👤 User profile for {sender}: {profile}")
    
    if not profile:
        logger.info(f"📝 No profile found for {sender}, creating new profile")
        create_user_profile(sender)
        
        try:
            send_sms(sender, ONBOARDING_NAME_MSG, bypass_quota=True)
            save_message(sender, "assistant", ONBOARDING_NAME_MSG, "onboarding_start", 0)
            return jsonify({"message": "Onboarding started for new user"}), 200
        except Exception as e:
            logger.error(f"Failed to send onboarding start message: {e}")
            return jsonify({"error": "Failed to start onboarding"}), 500
    
    elif not profile['onboarding_completed']:
        logger.info(f"🚀 User {sender} is in onboarding process (step {profile['onboarding_step']})")
        
        try:
            response_msg = handle_onboarding_response(sender, body)
            result = send_sms(sender, response_msg)
            
            if "error" not in result:
                logger.info(f"✅ Onboarding response sent to {sender}")
                return jsonify({"message": "Onboarding response sent"}), 200
            else:
                logger.error(f"❌ Failed to send onboarding response to {sender}: {result['error']}")
                return jsonify({"error": "Failed to send onboarding response"}), 500
                
        except Exception as e:
            logger.error(f"💥 Onboarding error for {sender}: {e}")
            fallback_msg = "Sorry, there was an error during setup. Please try again."
            try:
                send_sms(sender, fallback_msg, bypass_quota=True)
                return jsonify({"message": "Onboarding fallback sent"}), 200
            except Exception as fallback_error:
                logger.error(f"Failed to send onboarding fallback: {fallback_error}")
                return jsonify({"error": "Onboarding failed"}), 500
    
    # Check if user is requesting a longer response
    is_longer_request = detect_longer_request(body)
    
    # User is fully onboarded - continue to normal processing
    logger.info(f"✅ User {sender} is fully onboarded: {profile['first_name']} in {profile['location']}")
    
    intent = detect_intent(body, sender)
    intent_type = intent.type if intent else "general"
    
    # Add longer request flag to intent type for logging
    if is_longer_request:
        intent_type += "_longer"
        logger.info(f"🔍 User requested longer response for: {body}")
    
    user_context = get_user_context_for_queries(sender)
    
    try:
        if is_longer_request:
            # Get the last user query for context
            last_query = get_conversation_context(sender, "last_query")
            if last_query:
                # Re-process the last query with longer response
                longer_query = f"Provide detailed information about: {last_query}"
                if user_context['personalized']:
                    personalized_msg = f"User's name is {user_context['first_name']} and they live in {user_context['location']}. " + longer_query
                    response_msg = ask_claude(sender, personalized_msg)
                else:
                    response_msg = ask_claude(sender, longer_query)
                
                # Use longer length limit
                response_msg = truncate_response(response_msg, LONGER_SMS_LENGTH)
                message_parts = 3  # Count as 3 messages
            else:
                response_msg = "I'd be happy to provide more details! What would you like to know more about?"
                message_parts = 1
        
        elif intent and intent.type == "weather":
            if user_context['personalized']:
                city = user_context['location']
                logger.info(f"🌍 Using user's saved location: {city}")
                query = f"weather forecast {city}"
                response_msg = web_search(query, search_type="general")
                first_name = user_context['first_name']
                response_msg = f"Hi {first_name}! " + response_msg
            else:
                response_msg = web_search("weather forecast", search_type="general")
            
            response_msg = truncate_response(response_msg, MAX_SMS_LENGTH)
            if not is_longer_request:
                response_msg += " Text 'longer' for detailed forecast."
            message_parts = 1
        else:
            if user_context['personalized']:
                personalized_msg = f"User's name is {user_context['first_name']} and they live in {user_context['location']}. " + body
                response_msg = ask_claude(sender, personalized_msg)
            else:
                response_msg = ask_claude(sender, body)
            
            if "Let me search for" in response_msg:
                search_term = body
                if user_context['personalized'] and not any(keyword in body.lower() for keyword in ['in ', 'near ', 'at ']):
                    search_term += f" in {user_context['location']}"
                response_msg = web_search(search_term, search_type="general")
            
            response_msg = truncate_response(response_msg, MAX_SMS_LENGTH)
            if not is_longer_request and len(response_msg) >= MAX_SMS_LENGTH - 50:
                response_msg = response_msg[:-30] + " Text 'longer' for more."
            message_parts = 1
        
        original_length = len(response_msg)
        
        if original_length > len(response_msg):
            logger.info(f"📏 Response truncated from {original_length} to {len(response_msg)} chars")
        
        # Log message parts for cost tracking
        logger.info(f"📊 Response will use {message_parts} message parts")
        
        response_time = int((time.time() - start_time) * 1000)
        save_message(sender, "assistant", response_msg, intent_type, response_time)
        
        result = send_sms(sender, response_msg)
        
        if "error" not in result:
            # Track usage with correct message count
            # track_monthly_sms_usage(sender, message_count=message_parts, is_outgoing=True)
            log_usage_analytics(sender, intent_type, True, response_time)
            logger.info(f"✅ Response sent to {sender} in {response_time}ms (length: {len(response_msg)} chars, {message_parts} parts)")
            return jsonify({"message": "Response sent successfully"}), 200
        else:
            log_usage_analytics(sender, intent_type, False, response_time)
            logger.error(f"❌ Failed to send response to {sender}: {result['error']}")
            return jsonify({"error": "Failed to send response"}), 500
            
    except Exception as e:
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(sender, intent_type, False, response_time)
        logger.error(f"💥 Processing error for {sender}: {e}")
        
        fallback_msg = "Sorry, I'm having trouble processing your request. Please try again in a moment."
        try:
            send_sms(sender, fallback_msg, bypass_quota=True)
            return jsonify({"message": "Fallback response sent"}), 200
        except Exception as fallback_error:
            logger.error(f"Failed to send fallback message: {fallback_error}")
            return jsonify({"error": "Processing failed"}), 500

# === HEALTH CHECK ===
@app.route('/')
def health_check():
    return jsonify({
        'status': 'healthy',
        'version': APP_VERSION,
        'latest_changes': CHANGELOG[APP_VERSION],
        'database_type': 'PostgreSQL',
        'sms_char_limit': MAX_SMS_LENGTH,
        'monthly_message_limit': MONTHLY_LIMIT,
        'clicksend_max_limit': CLICKSEND_MAX_LENGTH,
        'admin_endpoints': [
            '/admin/remove-user',
            '/admin/reset-user', 
            '/admin/restore-user'
        ]
    })

# Initialize database on startup
init_db()

if __name__ == "__main__":
    logger.info(f"🚀 Starting Hey Alex SMS Assistant v{APP_VERSION}")
    logger.info(f"📋 Latest changes: {CHANGELOG[APP_VERSION]}")
    logger.info(f"🗄️ Database: PostgreSQL (persistent storage)")
    logger.info(f"📏 SMS response limit: {MAX_SMS_LENGTH} characters (1 SMS part)")
    logger.info(f"📊 Monthly message limit: {MONTHLY_LIMIT} messages")
    logger.info(f"🔧 Admin endpoints available: /admin/remove-user, /admin/reset-user, /admin/restore-user")
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
