from flask import Flask, request, jsonify
import requests
import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
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
APP_VERSION = "3.0"
CHANGELOG = {
    "3.0": "MAJOR: Migrated from SQLite to PostgreSQL for persistent data storage - no more data loss on redeploys!",
    "2.9": "Increased SMS response limit to 720 characters for longer, more detailed answers",
    "2.8": "Added comprehensive admin debug endpoints for SMS testing and troubleshooting",
    "2.7": "Added complete Stripe webhook integration for automatic subscription management and user lifecycle",
    "2.6": "Added automatic welcome message when new users are added to whitelist, enhanced whitelist tracking",
    "2.5": "Added Stripe webhook integration for automatic whitelist management based on subscription status",
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
    logger.info(f"🔑 Stripe Keys Status:")
    logger.info(f"  STRIPE_SECRET_KEY: {'✅ Set' if STRIPE_SECRET_KEY else '❌ Missing'}")
    logger.info(f"  STRIPE_WEBHOOK_SECRET: {'✅ Set' if STRIPE_WEBHOOK_SECRET else '❌ Missing'}")
    logger.info(f"  STRIPE_PUBLISHABLE_KEY: {'✅ Set' if STRIPE_PUBLISHABLE_KEY else '❌ Missing'}")
else:
    logger.warning("STRIPE_SECRET_KEY not found")

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

# SMS Response Limits
MAX_SMS_LENGTH = 720  # Increased from 500 to 720 characters
CLICKSEND_MAX_LENGTH = 1600  # ClickSend absolute limit

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
    "You get 300 messages per month. Try asking \"weather today\" to start!"
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

# === PostgreSQL Connection Manager ===
@contextmanager
def get_db_connection():
    """Context manager for PostgreSQL connections"""
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
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
    
    # Remove all non-digit characters
    digits_only = re.sub(r'\D', '', phone)
    
    # Add country code if missing (assume US)
    if len(digits_only) == 10:
        digits_only = '1' + digits_only
    
    # Format as +1XXXXXXXXXX
    if len(digits_only) == 11 and digits_only.startswith('1'):
        return '+' + digits_only
    
    # If it's already formatted correctly or other country
    if phone.startswith('+'):
        return phone
    
    return '+' + digits_only

def truncate_response(response_msg, max_length=MAX_SMS_LENGTH):
    """Intelligently truncate response to fit SMS limits"""
    if len(response_msg) <= max_length:
        return response_msg
    
    # Try to truncate at a sentence boundary
    truncated = response_msg[:max_length - 3]  # Leave room for "..."
    
    # Look for the last sentence ending
    sentence_ends = ['.', '!', '?']
    last_sentence_end = -1
    
    for end_char in sentence_ends:
        pos = truncated.rfind(end_char)
        if pos > last_sentence_end and pos > max_length * 0.7:  # Don't truncate too early
            last_sentence_end = pos
    
    if last_sentence_end > 0:
        return truncated[:last_sentence_end + 1]
    else:
        # No good sentence boundary, truncate at word boundary
        last_space = truncated.rfind(' ')
        if last_space > max_length * 0.8:  # Only if we don't lose too much
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
                
                c.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_profiles_phone 
                ON user_profiles(phone);
                """)
                
                # Onboarding log
                c.execute("""
                CREATE TABLE IF NOT EXISTS onboarding_log (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    step INTEGER NOT NULL,
                    response TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                # Whitelist events
                c.execute("""
                CREATE TABLE IF NOT EXISTS whitelist_events (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    action VARCHAR(20) NOT NULL CHECK(action IN ('added','removed')),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    source VARCHAR(50) DEFAULT 'manual'
                );
                """)
                
                # SMS delivery log
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
                
                # Usage analytics
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
                
                # Monthly SMS usage
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
                
                # Stripe subscription events
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
                
                # Fact check incidents
                c.execute("""
                CREATE TABLE IF NOT EXISTS fact_check_incidents (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    query TEXT NOT NULL,
                    response TEXT NOT NULL,
                    incident_type VARCHAR(50) DEFAULT 'potential_hallucination',
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                # Conversation context
                c.execute("""
                CREATE TABLE IF NOT EXISTS conversation_context (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    context_key VARCHAR(100) NOT NULL,
                    context_value TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(phone, context_key)
                );
                """)
                
                # ClickSend sync log
                c.execute("""
                CREATE TABLE IF NOT EXISTS clicksend_sync_log (
                    id SERIAL PRIMARY KEY,
                    list_id INTEGER,
                    list_name VARCHAR(200),
                    contacts_synced INTEGER,
                    sync_status VARCHAR(50),
                    sync_details TEXT,
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
                logger.info(f"📏 SMS response limit set to {MAX_SMS_LENGTH} characters")
                
                # Show recent users for debugging
                if user_count > 0:
                    c.execute("""
                        SELECT phone, first_name, location, onboarding_completed, created_date 
                        FROM user_profiles 
                        ORDER BY created_date DESC 
                        LIMIT 5
                    """)
                    recent_users = c.fetchall()
                    logger.info(f"📊 Recent users: {[dict(user) for user in recent_users]}")
                
    except Exception as e:
        logger.error(f"💥 PostgreSQL database initialization error: {e}")
        raise

# === Onboarding System ===
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
                
                # Build dynamic update query
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
        
        # Basic validation for first name
        if len(first_name) < 1 or len(first_name) > 50:
            return "Please enter a valid first name."
        
        # Remove any non-alphabetic characters except spaces, hyphens, apostrophes
        clean_name = re.sub(r"[^a-zA-Z\s\-']", "", first_name)
        if not clean_name:
            return "Please enter a valid first name using only letters."
        
        # Update profile with first name and move to step 2
        update_user_profile(phone, first_name=clean_name, onboarding_step=2)
        log_onboarding_step(phone, 1, clean_name)
        
        response = ONBOARDING_LOCATION_MSG.format(name=clean_name)
        save_message(phone, "assistant", response, "onboarding_location", 0)
        
        logger.info(f"👤 Collected name '{clean_name}' for {phone}, asking for location")
        return response
        
    elif current_step == 2:
        # Collecting location (city or zip code)
        location = message.strip().title()
        
        # Basic validation for location
        if len(location) < 2 or len(location) > 100:
            return "Please enter a valid city name or zip code."
        
        # Update profile and complete onboarding
        update_user_profile(phone, location=location, onboarding_step=3, onboarding_completed=True)
        log_onboarding_step(phone, 2, location)
        
        # Get the user's name for the completion message
        updated_profile = get_user_profile(phone)
        first_name = updated_profile['first_name'] if updated_profile else "there"
        
        response = ONBOARDING_COMPLETE_MSG.format(name=first_name)
        save_message(phone, "assistant", response, "onboarding_complete", 0)
        
        logger.info(f"🎉 Completed onboarding for {phone}: {first_name} in {location}")
        return response
    
    else:
        # Shouldn't happen, but handle gracefully
        logger.warning(f"Unexpected onboarding step {current_step} for {phone}")
        return "There was an error with your setup. You can now ask me questions!"

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
    
    # Check if this is a new user
    is_new_user = phone not in wl
    
    if is_new_user:
        try:
            with open(WHITELIST_FILE, "a") as f:
                f.write(phone + "\n")
            
            # Log the new user addition
            log_whitelist_event(phone, "added", source)
            
            logger.info(f"📱 Added new user {phone} to whitelist (source: {source})")
            
            # Create user profile for onboarding
            create_user_profile(phone)
            
            # Send welcome message to start onboarding for new users
            if send_welcome:
                try:
                    result = send_sms(phone, ONBOARDING_NAME_MSG, bypass_quota=True)
                    if "error" not in result:
                        logger.info(f"🎉 Onboarding started for new user {phone}")
                        # Log the welcome message
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
            
            # Log the removal
            log_whitelist_event(phone, "removed")
            
            logger.info(f"📱 Removed {phone} from whitelist")
            
            # Send goodbye message if requested
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
    
    # Apply ClickSend's absolute limit (1600 chars)
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
        
        logger.info(f"📋 ClickSend Response Status: {resp.status_code}")
        logger.info(f"📋 ClickSend Response Body: {json.dumps(result, indent=2)}")
        
        if resp.status_code == 200:
            if "data" in result and "messages" in result["data"]:
                messages = result["data"]["messages"]
                if messages:
                    msg_status = messages[0].get("status")
                    msg_id = messages[0].get("message_id")
                    msg_parts = messages[0].get("message_parts", 1)
                    
                    logger.info(f"✅ SMS queued successfully to {to_number} ({msg_parts} parts)")
                    
                    log_sms_delivery(to_number, message, result, msg_status, msg_id)
                    
                    if not bypass_quota:
                        track_monthly_sms_usage(to_number, is_outgoing=True)
            
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

def get_current_period_dates():
    now = datetime.now(timezone.utc)
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = period_start + timedelta(days=30)
    return period_start.date(), period_end.date()

def track_monthly_sms_usage(phone, is_outgoing=True):
    if not is_outgoing:
        return True, {}, None
    
    period_start, period_end = get_current_period_dates()
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                
                c.execute("""
                    SELECT id, message_count, quota_warnings_sent, quota_exceeded
                    FROM monthly_sms_usage
                    WHERE phone = %s AND period_start = %s
                """, (phone, period_start))
                
                result = c.fetchone()
                
                if result:
                    usage_id, current_count, warnings_sent, quota_exceeded = result['id'], result['message_count'], result['quota_warnings_sent'], result['quota_exceeded']
                    new_count = current_count + 1
                    
                    c.execute("""
                        UPDATE monthly_sms_usage 
                        SET message_count = %s, last_message_date = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (new_count, usage_id))
                else:
                    new_count = 1
                    warnings_sent = 0
                    quota_exceeded = False
                    
                    c.execute("""
                        INSERT INTO monthly_sms_usage 
                        (phone, message_count, period_start, period_end, quota_warnings_sent, quota_exceeded)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (phone, new_count, period_start, period_end, warnings_sent, quota_exceeded))
                
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
                
                return True, usage_info, None
                
    except Exception as e:
        logger.error(f"Error tracking monthly SMS usage: {e}")
        return True, {}, None

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
        
        # Check for API errors in response
        if 'error' in data:
            logger.error(f"❌ SerpAPI error: {data['error']}")
            return "Search service error. Please try again later."
        
    except Exception as e:
        logger.error(f"💥 Search exception: {e}")
        return "Search service temporarily unavailable. Try again later."

    # Process results - return longer, more detailed responses
    org = data.get("organic_results", [])
    if org:
        top = org[0]
        title = top.get("title", "")
        snippet = top.get("snippet", "")
        
        result = f"{title}"
        if snippet:
            result += f" — {snippet}"
        
        # Allow longer search results (up to our new 720 char limit)
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
        
        # Updated system context for longer responses
        system_context = f"""You are Alex, a helpful SMS assistant that helps people stay connected to information without spending time online. 

IMPORTANT GUIDELINES:
- You can now provide responses up to {MAX_SMS_LENGTH} characters (increased from 500)
- Give more detailed, thorough answers while staying within the character limit
- Be friendly and helpful with comprehensive information
- You DO have access to web search capabilities
- For specific information requests, respond with "Let me search for [specific topic]" 
- Never make up detailed information - always offer to search for accurate, current details
- Be conversational and provide valuable, complete answers"""
        
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
            
            # Increased max_tokens for longer responses
            data = {
                "model": "claude-3-haiku-20240307",
                "max_tokens": 250,  # Increased from 150 to 250
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
        
        # Apply intelligent truncation to Claude responses
        truncated_reply = truncate_response(reply, MAX_SMS_LENGTH)
        
        if len(truncated_reply) < len(reply):
            logger.info(f"📏 Claude response truncated from {len(reply)} to {len(truncated_reply)} chars")
            
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(phone, "claude_chat", True, response_time)
        
        return truncated_reply
        
    except Exception as e:
        logger.error(f"💥 Claude integration error for {phone}: {e}")
        return "I'm having trouble processing that question. Let me try to search for that information instead."

# === Rate Limiting ===
def can_send(sender):
    return True, ""  # Simplified for demo

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
        # Get customer information from Stripe
        customer = stripe.Customer.retrieve(customer_id)
        
        # Extract phone number from customer metadata or other fields
        phone = extract_phone_from_stripe_metadata(customer.get('metadata', {}))
        
        if not phone and customer.get('phone'):
            phone = normalize_phone_number(customer['phone'])
        
        if phone:
            # Update user profile with subscription information
            update_user_profile(
                phone, 
                stripe_customer_id=customer_id,
                subscription_status=status,
                subscription_id=subscription_id
            )
            
            # Add to whitelist if not already there
            add_to_whitelist(phone, send_welcome=True, source='stripe_subscription')
            
            # Log the event
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
        # Find user by customer ID
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT phone FROM user_profiles 
                    WHERE stripe_customer_id = %s
                """, (customer_id,))
                result = c.fetchone()
                
                if result:
                    phone = result['phone']
                    
                    # Update subscription status
                    update_user_profile(phone, subscription_status='cancelled')
                    
                    # Remove from whitelist
                    remove_from_whitelist(phone, send_goodbye=True)
                    
                    # Log the event
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
@app.route('/admin/users', methods=['GET'])
def get_all_users():
    """Admin endpoint to view all users with their profiles and onboarding status"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT 
                        phone, first_name, location, onboarding_step,
                        onboarding_completed, stripe_customer_id, subscription_status, created_date
                    FROM user_profiles
                    ORDER BY created_date DESC
                """)
                
                users = []
                for row in c.fetchall():
                    users.append({
                        'phone': row['phone'],
                        'first_name': row['first_name'],
                        'location': row['location'],
                        'onboarding_step': row['onboarding_step'],
                        'onboarding_completed': bool(row['onboarding_completed']),
                        'stripe_customer_id': row['stripe_customer_id'],
                        'subscription_status': row['subscription_status'],
                        'created_date': row['created_date'].isoformat() if row['created_date'] else None
                    })
                
                return jsonify({
                    'total_users': len(users),
                    'users': users,
                    'database_type': 'PostgreSQL',
                    'sms_char_limit': MAX_SMS_LENGTH
                })
                
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
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
        
        # 1. Add to whitelist (without sending welcome)
        success = add_to_whitelist(phone, send_welcome=False, source='admin_restore')
        if success:
            actions_taken.append("Added to whitelist")
        
        # 2. Create/update user profile with complete onboarding
        try:
            with get_db_connection() as conn:
                with conn.cursor() as c:
                    
                    # Delete existing profile if any
                    c.execute("DELETE FROM user_profiles WHERE phone = %s", (phone,))
                    
                    # Insert complete profile
                    c.execute("""
                        INSERT INTO user_profiles 
                        (phone, first_name, location, onboarding_step, onboarding_completed, 
                         stripe_customer_id, subscription_status, created_date, updated_date)
                        VALUES (%s, %s, %s, 3, TRUE, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, (phone, first_name, location, stripe_customer_id, subscription_status))
                    
                    conn.commit()
                    actions_taken.append("Created complete user profile")
                    
                    # Log the restoration
                    c.execute("""
                        INSERT INTO onboarding_log (phone, step, response, timestamp)
                        VALUES (%s, 999, %s, CURRENT_TIMESTAMP)
                    """, (phone, f"RESTORED: {first_name} in {location}"))
                    
                    conn.commit()
                    actions_taken.append("Logged profile restoration")
                    
        except Exception as db_error:
            logger.error(f"Database error restoring user: {db_error}")
            return jsonify({"error": f"Database error: {str(db_error)}"}), 500
        
        # 3. Send confirmation SMS
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

@app.route('/admin/whitelist', methods=['GET'])
def get_whitelist():
    """View current whitelist"""
    try:
        whitelist = load_whitelist()
        return jsonify({
            'total_numbers': len(whitelist),
            'numbers': list(whitelist),
            'database_type': 'PostgreSQL',
            'sms_char_limit': MAX_SMS_LENGTH
        })
    except Exception as e:
        logger.error(f"Error getting whitelist: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/whitelist/add', methods=['POST'])
def admin_add_to_whitelist():
    """Admin endpoint to manually add users to whitelist"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        send_welcome = data.get('send_welcome', True)
        
        if not phone:
            return jsonify({"error": "Phone number required"}), 400
        
        phone = normalize_phone_number(phone)
        
        success = add_to_whitelist(phone, send_welcome=send_welcome, source='admin')
        
        if success:
            return jsonify({
                "success": True,
                "message": f"Added {phone} to whitelist",
                "welcome_sent": send_welcome,
                "database_type": "PostgreSQL",
                "sms_char_limit": MAX_SMS_LENGTH
            })
        else:
            return jsonify({"error": "Failed to add to whitelist"}), 500
            
    except Exception as e:
        logger.error(f"Error in admin add to whitelist: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/whitelist/remove', methods=['POST'])
def admin_remove_from_whitelist():
    """Admin endpoint to remove users from whitelist"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        send_goodbye = data.get('send_goodbye', False)
        
        if not phone:
            return jsonify({"error": "Phone number required"}), 400
        
        phone = normalize_phone_number(phone)
        
        success = remove_from_whitelist(phone, send_goodbye=send_goodbye)
        
        if success:
            return jsonify({
                "success": True,
                "message": f"Removed {phone} from whitelist",
                "goodbye_sent": send_goodbye
            })
        else:
            return jsonify({"error": "Failed to remove from whitelist"}), 500
            
    except Exception as e:
        logger.error(f"Error in admin remove from whitelist: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/reset-user', methods=['POST'])
def admin_reset_user():
    """Reset a user completely - remove from whitelist and database"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        
        if not phone:
            return jsonify({"error": "Phone number required"}), 400
        
        phone = normalize_phone_number(phone)
        
        actions_taken = []
        
        # Remove from whitelist
        if remove_from_whitelist(phone):
            actions_taken.append("Removed from whitelist")
        
        # Remove user profile and related data
        try:
            with get_db_connection() as conn:
                with conn.cursor() as c:
                    c.execute("DELETE FROM user_profiles WHERE phone = %s", (phone,))
                    c.execute("DELETE FROM messages WHERE phone = %s", (phone,))
                    c.execute("DELETE FROM monthly_sms_usage WHERE phone = %s", (phone,))
                    c.execute("DELETE FROM onboarding_log WHERE phone = %s", (phone,))
                    c.execute("DELETE FROM sms_delivery_log WHERE phone = %s", (phone,))
                    conn.commit()
                    actions_taken.append("Removed from database")
        except Exception as db_error:
            logger.error(f"Database error resetting user: {db_error}")
            return jsonify({"error": f"Database error: {str(db_error)}"}), 500
        
        return jsonify({
            "success": True,
            "message": f"Reset user {phone} completely",
            "actions_taken": actions_taken
        })
        
    except Exception as e:
        logger.error(f"Error resetting user: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/sms-logs', methods=['GET'])
def get_sms_logs():
    """View recent SMS delivery logs"""
    try:
        limit = request.args.get('limit', 20, type=int)
        
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT phone, message_content, delivery_status, message_id, timestamp
                    FROM sms_delivery_log
                    ORDER BY timestamp DESC
                    LIMIT %s
                """, (limit,))
                
                logs = []
                for row in c.fetchall():
                    content_preview = row['message_content'][:100] + '...' if len(row['message_content']) > 100 else row['message_content']
                    logs.append({
                        'phone': row['phone'],
                        'message_content': content_preview,
                        'message_length': len(row['message_content']),
                        'delivery_status': row['delivery_status'],
                        'message_id': row['message_id'],
                        'timestamp': row['timestamp'].isoformat() if row['timestamp'] else None
                    })
