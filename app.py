from flask import Flask, request, jsonify
import requests
import os
import json
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
import hmac
import hashlib

# Database imports - try psycopg3 first, then psycopg2
POSTGRES_AVAILABLE = False
psycopg = None
RealDictCursor = None
PSYCOPG_VERSION = None

try:
    # Try psycopg3 first (better Python 3.13 support)
    import psycopg
    from psycopg.rows import dict_row
    POSTGRES_AVAILABLE = True
    PSYCOPG_VERSION = 3
except ImportError:
    try:
        # Fallback to psycopg2
        import psycopg2 as psycopg
        from psycopg2.extras import RealDictCursor
        POSTGRES_AVAILABLE = True
        PSYCOPG_VERSION = 2
    except ImportError:
        pass

# SQLite fallback
import sqlite3

# Try to import stripe, but handle gracefully if not available
try:
    import stripe
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False
    stripe = None

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
    "3.0": "Complete SMS assistant with PostgreSQL, Claude AI, search, onboarding, and all features",
    "2.8": "Added PostgreSQL support for persistent data storage",
    "2.7": "Fixed PostgreSQL connection and query handling",
    "2.6": "Added automatic welcome message when new users are added to whitelist"
}

# === Database Configuration ===
DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "chat.db")

USE_POSTGRES = bool(DATABASE_URL and POSTGRES_AVAILABLE)
logger.info(f"🗄️ Database Configuration:")
logger.info(f"  DATABASE_URL: {'✅ Set' if DATABASE_URL else '❌ Missing'}")
logger.info(f"  PostgreSQL Available: {'✅ Yes' if POSTGRES_AVAILABLE else '❌ No'}")
logger.info(f"  Using: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")
if POSTGRES_AVAILABLE:
    logger.info(f"  PostgreSQL Version: psycopg{PSYCOPG_VERSION}")

# === Config & API Keys ===
CLICKSEND_USERNAME = os.getenv("CLICKSEND_USERNAME")
CLICKSEND_API_KEY = os.getenv("CLICKSEND_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

# Debug API key availability
logger.info(f"🔑 API Keys Status:")
logger.info(f"  CLICKSEND_USERNAME: {'✅ Set' if CLICKSEND_USERNAME else '❌ Missing'}")
logger.info(f"  CLICKSEND_API_KEY: {'✅ Set' if CLICKSEND_API_KEY else '❌ Missing'}")
logger.info(f"  ANTHROPIC_API_KEY: {'✅ Set' if ANTHROPIC_API_KEY else '❌ Missing'}")
logger.info(f"  SERPAPI_API_KEY: {'✅ Set' if SERPAPI_API_KEY else '❌ Missing'}")

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

if STRIPE_AVAILABLE and STRIPE_SECRET_KEY:
    try:
        stripe.api_key = STRIPE_SECRET_KEY
        logger.info("✅ Stripe API initialized successfully")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Stripe API: {e}")
        STRIPE_AVAILABLE = False
else:
    logger.warning("❌ Stripe not available")

# Initialize Anthropic client
anthropic_client = None
if ANTHROPIC_API_KEY:
    try:
        import anthropic as anthropic_lib
        anthropic_lib.api_key = ANTHROPIC_API_KEY
        anthropic_client = anthropic_lib
        logger.info("✅ Anthropic client initialized successfully")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Anthropic: {e}")
        anthropic_client = None
else:
    logger.warning("❌ ANTHROPIC_API_KEY not found")

WHITELIST_FILE = "whitelist.txt"
USAGE_FILE = "usage.json"
MONTHLY_USAGE_FILE = "monthly_usage.json"
USAGE_LIMIT = 200
MONTHLY_LIMIT = 300
RESET_DAYS = 30

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

# === Database Connection Management ===
def get_db_connection():
    """Get database connection based on environment"""
    if USE_POSTGRES:
        if PSYCOPG_VERSION == 3:
            return psycopg.connect(DATABASE_URL)
        else:
            return psycopg.connect(DATABASE_URL)
    else:
        return sqlite3.connect(DB_PATH)

def execute_query(query, params=None, fetch=False, fetchall=False, fetchone=False):
    """Execute database query with proper connection handling"""
    try:
        if USE_POSTGRES:
            with get_db_connection() as conn:
                if PSYCOPG_VERSION == 3:
                    with conn.cursor(row_factory=dict_row) as cursor:
                        cursor.execute(query, params or ())
                        conn.commit()
                        
                        if fetchall:
                            return cursor.fetchall()
                        elif fetchone:
                            return cursor.fetchone()
                        elif fetch:
                            return cursor.fetchall()
                        else:
                            return cursor.rowcount
                else:
                    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                        cursor.execute(query, params or ())
                        conn.commit()
                        
                        if fetchall:
                            return cursor.fetchall()
                        elif fetchone:
                            return cursor.fetchone()
                        elif fetch:
                            return cursor.fetchall()
                        else:
                            return cursor.rowcount
        else:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, params or ())
                conn.commit()
                
                if fetchall:
                    return cursor.fetchall()
                elif fetchone:
                    return cursor.fetchone()
                elif fetch:
                    return cursor.fetchall()
                else:
                    return cursor.rowcount
                    
    except Exception as e:
        logger.error(f"Database query error: {e}")
        logger.error(f"Query: {query}")
        logger.error(f"Params: {params}")
        raise

def init_db():
    """Initialize database with proper schema"""
    try:
        logger.info(f"🗄️ Initializing {'PostgreSQL' if USE_POSTGRES else 'SQLite'} database")
        
        if USE_POSTGRES:
            # PostgreSQL table creation
            tables = [
                # Messages table
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    role VARCHAR(20) NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    intent_type VARCHAR(50),
                    response_time_ms INTEGER
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_messages_phone_ts ON messages(phone, ts DESC);",
                
                # User profiles table
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) UNIQUE NOT NULL,
                    first_name VARCHAR(100),
                    location VARCHAR(200),
                    onboarding_step INTEGER DEFAULT 0,
                    onboarding_completed BOOLEAN DEFAULT FALSE,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_phone ON user_profiles(phone);",
                
                # Onboarding log table
                """
                CREATE TABLE IF NOT EXISTS onboarding_log (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    step INTEGER NOT NULL,
                    response TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
                
                # Whitelist events table
                """
                CREATE TABLE IF NOT EXISTS whitelist_events (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    action VARCHAR(20) NOT NULL CHECK(action IN ('added','removed')),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    source VARCHAR(50) DEFAULT 'manual'
                );
                """,
                
                # SMS delivery log table
                """
                CREATE TABLE IF NOT EXISTS sms_delivery_log (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    message_content TEXT NOT NULL,
                    clicksend_response TEXT,
                    delivery_status VARCHAR(50),
                    message_id VARCHAR(100),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
                
                # Monthly SMS usage table
                """
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
                """,
                "CREATE INDEX IF NOT EXISTS idx_monthly_usage_phone_period ON monthly_sms_usage(phone, period_start DESC);",
                
                # Usage analytics table
                """
                CREATE TABLE IF NOT EXISTS usage_analytics (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    intent_type VARCHAR(50),
                    success BOOLEAN,
                    response_time_ms INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
                
                # Conversation context table
                """
                CREATE TABLE IF NOT EXISTS conversation_context (
                    id SERIAL PRIMARY KEY,
                    phone VARCHAR(20) NOT NULL,
                    context_key VARCHAR(100) NOT NULL,
                    context_value TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(phone, context_key)
                );
                """
            ]
            
            for table_sql in tables:
                execute_query(table_sql)
            
            logger.info("✅ PostgreSQL tables created successfully")
            
        else:
            # SQLite table creation (fallback for local development)
            tables = [
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                    intent_type TEXT,
                    response_time_ms INTEGER
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_messages_phone_ts ON messages(phone, ts DESC);",
                
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE NOT NULL,
                    first_name TEXT,
                    location TEXT,
                    onboarding_step INTEGER DEFAULT 0,
                    onboarding_completed BOOLEAN DEFAULT FALSE,
                    created_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_date DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_phone ON user_profiles(phone);",
                
                """
                CREATE TABLE IF NOT EXISTS onboarding_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    response TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """,
                
                """
                CREATE TABLE IF NOT EXISTS whitelist_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('added','removed')),
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    source TEXT DEFAULT 'manual'
                );
                """,
                
                """
                CREATE TABLE IF NOT EXISTS sms_delivery_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    message_content TEXT NOT NULL,
                    clicksend_response TEXT,
                    delivery_status TEXT,
                    message_id TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """,
                
                """
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
                """,
                
                """
                CREATE TABLE IF NOT EXISTS usage_analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    intent_type TEXT,
                    success BOOLEAN,
                    response_time_ms INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """,
                
                """
                CREATE TABLE IF NOT EXISTS conversation_context (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    context_value TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(phone, context_key)
                );
                """
            ]
            
            for table_sql in tables:
                execute_query(table_sql)
            
            logger.info("✅ SQLite tables created successfully")
        
        # Check for existing data
        user_result = execute_query("SELECT COUNT(*) as count FROM user_profiles", fetchone=True)
        message_result = execute_query("SELECT COUNT(*) as count FROM messages", fetchone=True)
        
        if USE_POSTGRES:
            user_count = user_result['count']
            message_count = message_result['count']
        else:
            user_count = user_result[0]
            message_count = message_result[0]
        
        logger.info(f"📊 Database initialized successfully")
        logger.info(f"📊 Found {user_count} user profiles and {message_count} messages")
        
    except Exception as e:
        logger.error(f"💥 Database initialization error: {e}")
        raise

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

def load_whitelist():
    try:
        with open(WHITELIST_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

# === Database Functions ===
def get_user_profile(phone):
    """Get user profile and onboarding status"""
    try:
        result = execute_query("""
            SELECT first_name, location, onboarding_step, onboarding_completed
            FROM user_profiles
            WHERE phone = %s
        """ if USE_POSTGRES else """
            SELECT first_name, location, onboarding_step, onboarding_completed
            FROM user_profiles
            WHERE phone = ?
        """, (phone,), fetchone=True)
        
        if result:
            if USE_POSTGRES:
                return {
                    'first_name': result['first_name'],
                    'location': result['location'],
                    'onboarding_step': result['onboarding_step'],
                    'onboarding_completed': bool(result['onboarding_completed'])
                }
            else:
                return {
                    'first_name': result[0],
                    'location': result[1],
                    'onboarding_step': result[2],
                    'onboarding_completed': bool(result[3])
                }
        else:
            return None
    except Exception as e:
        logger.error(f"Error getting user profile for {phone}: {e}")
        return None

def create_user_profile(phone):
    """Create new user profile for onboarding"""
    try:
        if USE_POSTGRES:
            execute_query("""
                INSERT INTO user_profiles (phone, onboarding_step, onboarding_completed)
                VALUES (%s, 1, FALSE)
                ON CONFLICT (phone) DO NOTHING
            """, (phone,))
        else:
            execute_query("""
                INSERT OR IGNORE INTO user_profiles 
                (phone, onboarding_step, onboarding_completed)
                VALUES (?, 1, FALSE)
            """, (phone,))
        
        logger.info(f"📝 Created user profile for {phone}")
        return True
    except Exception as e:
        logger.error(f"Error creating user profile for {phone}: {e}")
        return False

def update_user_profile(phone, first_name=None, location=None, onboarding_step=None, onboarding_completed=None):
    """Update user profile information"""
    try:
        # Build dynamic update query
        update_parts = []
        params = []
        
        if first_name is not None:
            update_parts.append("first_name = %s" if USE_POSTGRES else "first_name = ?")
            params.append(first_name)
        
        if location is not None:
            update_parts.append("location = %s" if USE_POSTGRES else "location = ?")
            params.append(location)
        
        if onboarding_step is not None:
            update_parts.append("onboarding_step = %s" if USE_POSTGRES else "onboarding_step = ?")
            params.append(onboarding_step)
        
        if onboarding_completed is not None:
            update_parts.append("onboarding_completed = %s" if USE_POSTGRES else "onboarding_completed = ?")
            params.append(onboarding_completed)
        
        update_parts.append("updated_date = CURRENT_TIMESTAMP")
        params.append(phone)
        
        query = f"""
            UPDATE user_profiles 
            SET {', '.join(update_parts)}
            WHERE phone = {'%s' if USE_POSTGRES else '?'}
        """
        
        execute_query(query, params)
        logger.info(f"📝 Updated user profile for {phone}")
        return True
    except Exception as e:
        logger.error(f"Error updating user profile for {phone}: {e}")
        return False

def save_message(phone, role, content, intent_type=None, response_time_ms=None):
    """Save message to database"""
    execute_query("""
        INSERT INTO messages (phone, role, content, intent_type, response_time_ms) 
        VALUES (%s, %s, %s, %s, %s)
    """ if USE_POSTGRES else """
        INSERT INTO messages (phone, role, content, intent_type, response_time_ms) 
        VALUES (?, ?, ?, ?, ?)
    """, (phone, role, content, intent_type, response_time_ms))

def load_history(phone, limit=4):
    """Load conversation history"""
    rows = execute_query("""
        SELECT role, content
        FROM messages
        WHERE phone = %s
        ORDER BY id DESC
        LIMIT %s
    """ if USE_POSTGRES else """
        SELECT role, content
        FROM messages
        WHERE phone = ?
        ORDER BY id DESC
        LIMIT ?
    """, (phone, limit), fetchall=True)
    
    if USE_POSTGRES:
        return [{"role": row['role'], "content": row['content']} for row in reversed(rows)]
    else:
        return [{"role": row[0], "content": row[1]} for row in reversed(rows)]

def log_usage_analytics(phone, intent_type, success, response_time_ms):
    """Log usage analytics"""
    execute_query("""
        INSERT INTO usage_analytics (phone, intent_type, success, response_time_ms)
        VALUES (%s, %s, %s, %s)
    """ if USE_POSTGRES else """
        INSERT INTO usage_analytics (phone, intent_type, success, response_time_ms)
        VALUES (?, ?, ?, ?)
    """, (phone, intent_type, success, response_time_ms))

def log_whitelist_event(phone, action):
    """Log whitelist addition/removal events"""
    try:
        execute_query("""
            INSERT INTO whitelist_events (phone, action)
            VALUES (%s, %s)
        """ if USE_POSTGRES else """
            INSERT INTO whitelist_events (phone, action, timestamp)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (phone, action))
        logger.info(f"📋 Logged whitelist event: {action} for {phone}")
    except Exception as e:
        logger.error(f"Error logging whitelist event: {e}")

def log_onboarding_step(phone, step, response):
    """Log onboarding step response"""
    try:
        execute_query("""
            INSERT INTO onboarding_log (phone, step, response)
            VALUES (%s, %s, %s)
        """ if USE_POSTGRES else """
            INSERT INTO onboarding_log (phone, step, response)
            VALUES (?, ?, ?)
        """, (phone, step, response))
    except Exception as e:
        logger.error(f"Error logging onboarding step: {e}")

def set_conversation_context(phone, key, value):
    """Set conversation context"""
    try:
        if USE_POSTGRES:
            execute_query("""
                INSERT INTO conversation_context (phone, context_key, context_value, timestamp)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (phone, context_key) DO UPDATE SET
                    context_value = EXCLUDED.context_value,
                    timestamp = CURRENT_TIMESTAMP
            """, (phone, key, value))
        else:
            execute_query("""
                INSERT OR REPLACE INTO conversation_context (phone, context_key, context_value, timestamp)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """, (phone, key, value))
    except Exception as e:
        logger.error(f"Error setting conversation context: {e}")

def get_conversation_context(phone, key):
    """Get conversation context"""
    try:
        result = execute_query("""
            SELECT context_value FROM conversation_context
            WHERE phone = %s AND context_key = %s
            AND timestamp > (CURRENT_TIMESTAMP - INTERVAL '10 minutes')
        """ if USE_POSTGRES else """
            SELECT context_value FROM conversation_context
            WHERE phone = ? AND context_key = ?
            AND timestamp > datetime('now', '-10 minutes')
        """, (phone, key), fetchone=True)
        
        if result:
            return result['context_value'] if USE_POSTGRES else result[0]
        return None
    except Exception as e:
        logger.error(f"Error getting conversation context: {e}")
        return None

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
        # Check existing usage
        result = execute_query("""
            SELECT id, message_count, quota_warnings_sent, quota_exceeded
            FROM monthly_sms_usage
            WHERE phone = %s AND period_start = %s
        """ if USE_POSTGRES else """
            SELECT id, message_count, quota_warnings_sent, quota_exceeded
            FROM monthly_sms_usage
            WHERE phone = ? AND period_start = ?
        """, (phone, period_start), fetchone=True)
        
        if result:
            if USE_POSTGRES:
                usage_id, current_count, warnings_sent, quota_exceeded = result['id'], result['message_count'], result['quota_warnings_sent'], result['quota_exceeded']
            else:
                usage_id, current_count, warnings_sent, quota_exceeded = result[0], result[1], result[2], result[3]
            
            new_count = current_count + 1
            
            execute_query("""
                UPDATE monthly_sms_usage 
                SET message_count = %s, last_message_date = CURRENT_TIMESTAMP
                WHERE id = %s
            """ if USE_POSTGRES else """
                UPDATE monthly_sms_usage 
                SET message_count = ?, last_message_date = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (new_count, usage_id))
        else:
            new_count = 1
            warnings_sent = 0
            quota_exceeded = False
            
            execute_query("""
                INSERT INTO monthly_sms_usage 
                (phone, message_count, period_start, period_end, quota_warnings_sent, quota_exceeded)
                VALUES (%s, %s, %s, %s, %s, %s)
            """ if USE_POSTGRES else """
                INSERT INTO monthly_sms_usage 
                (phone, message_count, period_start, period_end, quota_warnings_sent, quota_exceeded)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (phone, new_count, period_start, period_end, warnings_sent, quota_exceeded))
        
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
            warning_message = QUOTA_EXCEEDED_MSG.format(
                days_remaining=usage_info["days_remaining"]
            )
            logger.warning(f"📊 QUOTA EXCEEDED: {phone} - {new_count}/{MONTHLY_LIMIT} messages")
            return False, usage_info, warning_message
        
        # Check for warning thresholds
        warning_thresholds = [250, 280, 295]
        for threshold in warning_thresholds:
            if new_count == threshold and warnings_sent < len([t for t in warning_thresholds if t <= threshold]):
                warning_message = QUOTA_WARNING_MSG.format(
                    count=new_count,
                    remaining=usage_info["remaining"]
                )
                
                execute_query("""
                    UPDATE monthly_sms_usage 
                    SET quota_warnings_sent = quota_warnings_sent + 1
                    WHERE phone = %s AND period_start = %s
                """ if USE_POSTGRES else """
                    UPDATE monthly_sms_usage 
                    SET quota_warnings_sent = quota_warnings_sent + 1
                    WHERE phone = ? AND period_start = ?
                """, (phone, period_start))
                
                logger.info(f"📊 QUOTA WARNING: {phone} - {new_count}/{MONTHLY_LIMIT} messages (threshold: {threshold})")
                break
        
        logger.info(f"📊 Monthly usage: {phone} - {new_count}/{MONTHLY_LIMIT} messages")
        return True, usage_info, warning_message
        
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
                return False, ""
        
        # Check for actual spam
        for category, keywords in self.spam_keywords.items():
            for keyword in keywords:
                if len(keyword.split()) == 1:
                    pattern = r'\b' + re.escape(keyword) + r'\b.*\b(now|today|click|call|text)\b'
                    if re.search(pattern, text_lower):
                        return True, f"Spam detected: {category}"
                else:
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

# === SMS Functions ===
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
        
        if resp.status_code == 200:
            if "data" in result and "messages" in result["data"]:
                messages = result["data"]["messages"]
                if messages:
                    msg_status = messages[0].get("status")
                    msg_id = messages[0].get("message_id")
                    
                    logger.info(f"✅ SMS queued successfully to {to_number}")
                    
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
    execute_query("""
        INSERT INTO sms_delivery_log (phone, message_content, clicksend_response, delivery_status, message_id)
        VALUES (%s, %s, %s, %s, %s)
    """ if USE_POSTGRES else """
        INSERT INTO sms_delivery_log (phone, message_content, clicksend_response, delivery_status, message_id)
        VALUES (?, ?, ?, ?, ?)
    """, (phone, message_content, json.dumps(clicksend_response), delivery_status, message_id))

# === Onboarding System ===
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

# === Enhanced Whitelist Management ===
def add_to_whitelist(phone, send_welcome=True):
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
            log_whitelist_event(phone, "added")
            
            logger.info(f"📱 Added new user {phone} to whitelist")
            
            # Create user profile for onboarding
            create_user_profile(phone)
            
            # Send welcome message to start onboarding for new users
            if send_welcome:
                try:
                    send_sms(phone, ONBOARDING_NAME_MSG, bypass_quota=True)
                    logger.info(f"🎉 Onboarding started for new user {phone}")
                    
                    # Log the welcome message
                    save_message(phone, "assistant", ONBOARDING_NAME_MSG, "onboarding_start", 0)
                    
                except Exception as sms_error:
                    logger.error(f"Failed to send onboarding SMS to {phone}: {sms_error}")
            
            return True
        except Exception as e:
            logger.error(f"Failed to add {phone} to whitelist: {e}")
            return False
    else:
        logger.info(f"📱 {phone} already in whitelist")
        return True

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
    
    if search_type == "news":
        params["tbm"] = "nws"
    elif search_type == "local":
        params["engine"] = "google_maps"
    
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

    # Process results based on search type
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
        
        if local_results:
            result_place = local_results[0]
            name = result_place.get('title', '') or result_place.get('name', '')
            address = result_place.get('address', '') or result_place.get('vicinity', '')
            rating = result_place.get('rating', '')
            phone = result_place.get('phone', '') or result_place.get('formatted_phone_number', '')
            
            result = name if name else "Business found"
            if rating:
                result += f" (★{rating})"
            if address:
                result += f" — {address}"
            if phone:
                result += f" — {phone}"
            
            return result[:500]
    
    # General search results
    org = data.get("organic_results", [])
    if org:
        for result in org[:3]:
            title = result.get("title", "")
            snippet = result.get("snippet", "")
            source = result.get("source", "")
            
            # Skip low-quality sources
            low_quality_indicators = [
                'reddit.com/r/', 'yahoo.answers', 'quora.com', 
                'answers.com', 'ask.com', '/forums/', 
                'discussion', 'forum', 'thread'
            ]
            
            if any(indicator in source.lower() or indicator in title.lower() 
                   for indicator in low_quality_indicators):
                continue
            
            result_text = f"{title}"
            if snippet:
                result_text += f" — {snippet}"
            
            return result_text[:500]
        
        # Fallback to first result
        top = org[0]
        title = top.get("title", "")
        snippet = top.get("snippet", "")
        
        result = f"{title}"
        if snippet:
            result += f" — {snippet}"
        return result[:500]
    
    return f"No results found for '{q}'."

# === Intent Detection ===
@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]
    confidence: float = 1.0

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
    
    if any(re.search(pattern, text, re.I) for pattern in weather_patterns):
        city = _extract_city(text)
        day = _extract_day(text) or "today"
        return IntentResult("weather", {"city": city, "day": day})
    return None

def detect_hours_intent(text: str) -> Optional[IntentResult]:
    patterns = [
        r"what\s+time\s+does\s+(.+?)\s+(open|close)",
        r"hours\s+for\s+(.+)$",
        r"when\s+(?:does|is)\s+(.+?)\s+(?:open|close)",
        r"(.+?)\s+hours\b"
    ]
    
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            biz = m.group(1).strip()
            city = _extract_city(text)
            day = _extract_day(text)
            
            return IntentResult("hours", {"biz": biz, "city": city, "day": day})
    return None

def detect_restaurant_intent(text: str) -> Optional[IntentResult]:
    patterns = [
        r'\b(find|search|locate)\b.*\brestaurant\b',
        r'\brestaurant\s+(near|in|around)\b',
        r'\b(best|good|top)\s+restaurant\b',
        r'\b(pizza|burger|sushi|italian|mexican)\s+(?:restaurant|place|near)\b',
        r'\bwhere\s+(?:can|to)\s+eat\b',
        r'\bfood\s+(?:near|in|around)\b'
    ]
    
    if any(re.search(pattern, text, re.I) for pattern in patterns):
        city = _extract_city(text)
        return IntentResult("restaurant", {"city": city, "query": text})
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

def detect_follow_up_intent(text: str, phone: str) -> Optional[IntentResult]:
    follow_up_patterns = [
        r'\bmore\s+(info|information|details)\b',
        r'\btell\s+me\s+more\b',
        r'\bhow\s+many\b',
        r'\bwhat\s+else\b',
        r'\bother\s+(details|info)\b',
        r'\bcontinue\b',
        r'\bgo\s+on\b'
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
    
    return None

# Ordered list of intent detectors
INTENT_DETECTORS = [
    detect_hours_intent,
    detect_weather_intent,
    detect_restaurant_intent,
    detect_news_intent,
    detect_follow_up_intent,
]

def detect_intent(text: str, phone: str = None) -> Optional[IntentResult]:
    for detector in INTENT_DETECTORS:
        if detector.__name__ == "detect_follow_up_intent" and phone:
            result = detector(text, phone)
        else:
            result = detector(text)
        if result:
            logger.info(f"Detected intent: {result.type} with entities: {result.entities}")
            return result
    return None

# === Claude Integration ===
def ask_claude(phone, user_msg):
    start_time = time.time()
    
    if not anthropic_client:
        logger.warning("❌ ANTHROPIC_API_KEY not configured - Claude unavailable")
        return "I'd love to help with that question, but my AI service isn't configured right now. Let me try to search for that information instead."
    
    try:
        history = load_history(phone, limit=4)
        
        system_context = """You are Alex, a helpful SMS assistant that helps people stay connected to information without spending time online. 

IMPORTANT GUIDELINES:
- Keep responses under 500 characters when possible for SMS
- Be friendly and helpful
- You DO have access to web search capabilities
- For specific information requests, respond with "Let me search for [specific topic]" 
- Never make up detailed information - always offer to search for accurate, current details
- Be conversational and helpful"""
        
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
                logger.info(f"✅ Claude responded successfully")
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
        
        if len(reply) > 500:
            reply = reply[:497] + "..."
            
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(phone, "claude_chat", True, response_time)
        
        return reply
        
    except Exception as e:
        logger.error(f"💥 Claude integration error for {phone}: {e}")
        return "I'm having trouble processing that question. Let me try to search for that information instead."

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

# === Main SMS Webhook ===
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
    
    # Rate limiting
    can_proceed, rate_limit_reason = can_send(sender)
    if not can_proceed:
        logger.warning(f"🚫 Rate limited for {sender}: {rate_limit_reason}")
        return jsonify({"message": "Rate limited"}), 429
    
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
        # Check if user is already onboarded
        if is_user_onboarded(sender):
            response_msg = WELCOME_MSG
        else:
            # Start or restart onboarding
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
    
    if not profile:
        # No profile exists - create one and start onboarding
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
        # Profile exists but onboarding not complete
        logger.info(f"🚀 User {sender} is in onboarding process (step {profile['onboarding_step']})")
        
        try:
            response_msg = handle_onboarding_response(sender, body)
            
            # Send response
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
    
    # User is fully onboarded - continue to normal processing
    logger.info(f"✅ User {sender} is fully onboarded: {profile['first_name']} in {profile['location']}")
    
    # Check monthly quota
    quota_allowed, usage_info, warning_message = track_monthly_sms_usage(sender, is_outgoing=False)
    
    if not quota_allowed:
        # Monthly quota exceeded
        try:
            send_sms(sender, warning_message, bypass_quota=True)
            return jsonify({"message": "Monthly quota exceeded message sent"}), 200
        except Exception as e:
            logger.error(f"Failed to send quota exceeded message: {e}")
            return jsonify({"error": "Monthly quota exceeded"}), 429
    
    # Process the user's query
    intent = detect_intent(body, sender)
    intent_type = intent.type if intent else "general"
    
    # Get user context for personalized responses
    user_context = get_user_context_for_queries(sender)
    
    try:
        # Process based on intent
        if intent and intent.type == "weather":
            # Use user's location if no city specified and user is onboarded
            if user_context['personalized'] and not intent.entities.get('city'):
                city = user_context['location']
                logger.info(f"🌍 Using user's saved location: {city}")
                query = f"weather forecast {city}"
                response_msg = web_search(query, search_type="general")
                first_name = user_context['first_name']
                response_msg = f"Hi {first_name}! " + response_msg
            else:
                city = intent.entities.get('city', 'current location')
                query = f"weather forecast {city}"
                response_msg = web_search(query, search_type="general")
                
        elif intent and intent.type == "hours":
            # Business hours query
            biz = intent.entities.get('biz', '')
            city = intent.entities.get('city', '')
            
            if user_context['personalized'] and not city:
                city = user_context['location']
                logger.info(f"🌍 Using user's saved location for business hours: {city}")
            
            query = f"{biz} hours"
            if city:
                query += f" {city}"
            
            response_msg = web_search(query, search_type="local")
            
        elif intent and intent.type == "restaurant":
            # Restaurant search
            city = intent.entities.get('city', '')
            
            if user_context['personalized'] and not city:
                city = user_context['location']
                logger.info(f"🌍 Using user's saved location for restaurant search: {city}")
            
            query = intent.entities.get('query', body)
            if city and 'in ' not in query.lower() and 'near ' not in query.lower():
                query += f" in {city}"
            
            response_msg = web_search(query, search_type="local")
            
        elif intent and intent.type == "news":
            # News search
            topic = intent.entities.get('topic', '').strip()
            if topic:
                query = f"latest news {topic}"
            else:
                query = "latest news headlines"
            
            response_msg = web_search(query, search_type="news")
            
        elif intent and intent.type == "follow_up":
            # Follow-up query
            search_term = intent.entities.get('original_query', body)
            response_msg = web_search(search_term, search_type="general")
            
        else:
            # Use Claude for general queries with user context
            if user_context['personalized']:
                personalized_msg = f"User's name is {user_context['first_name']} and they live in {user_context['location']}. " + body
                response_msg = ask_claude(sender, personalized_msg)
            else:
                response_msg = ask_claude(sender, body)
            
            # If Claude suggests a search, perform it
            if "Let me search for" in response_msg:
                search_term = body
                # Add location context to search if available
                if user_context['personalized'] and not any(keyword in body.lower() for keyword in ['in ', 'near ', 'at ']):
                    search_term += f" in {user_context['location']}"
                response_msg = web_search(search_term, search_type="general")
                
                # Store context for potential follow-ups
                set_conversation_context(sender, "last_searched_entity", search_term)
        
        # Ensure response is not too long for SMS
        if len(response_msg) > 1600:
            response_msg = response_msg[:1597] + "..."
        
        # Save assistant response
        response_time = int((time.time() - start_time) * 1000)
        save_message(sender, "assistant", response_msg, intent_type, response_time)
        
        # Send main response
        result = send_sms(sender, response_msg)
        
        if "error" not in result:
            log_usage_analytics(sender, intent_type, True, response_time)
            logger.info(f"✅ Response sent to {sender} in {response_time}ms")
            
            # Send quota warning if needed
            if warning_message:
                try:
                    send_sms(sender, warning_message, bypass_quota=True)
                    logger.info(f"📊 Quota warning sent to {sender}")
                except Exception as warning_error:
                    logger.error(f"Failed to send quota warning: {warning_error}")
            
            return jsonify({"message": "Response sent successfully"}), 200
        else:
            log_usage_analytics(sender, intent_type, False, response_time)
            logger.error(f"❌ Failed to send response to {sender}: {result['error']}")
            return jsonify({"error": "Failed to send response"}), 500
            
    except Exception as e:
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(sender, intent_type, False, response_time)
        logger.error(f"💥 Processing error for {sender}: {e}")
        
        # Send fallback response
        fallback_msg = "Sorry, I'm having trouble processing your request. Please try again in a moment."
        try:
            send_sms(sender, fallback_msg, bypass_quota=True)
            return jsonify({"message": "Fallback response sent"}), 200
        except Exception as fallback_error:
            logger.error(f"Failed to send fallback message: {fallback_error}")
            return jsonify({"error": "Processing failed"}), 500

# === Stripe Webhook (if available) ===
if STRIPE_AVAILABLE:
    @app.route('/webhook/stripe', methods=['POST'])
    def stripe_webhook():
        """Handle Stripe webhook events for subscription management"""
        payload = request.get_data(as_text=True)
        sig_header = request.headers.get('Stripe-Signature')

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            logger.error(f"Invalid payload in Stripe webhook: {e}")
            return jsonify({"error": "Invalid payload"}), 400
        except stripe.SignatureVerificationError as e:
            logger.error(f"Invalid signature in Stripe webhook: {e}")
            return jsonify({"error": "Invalid signature"}), 400

        logger.info(f"🔔 Received Stripe webhook: {event['type']}")
        
        # Handle the event
        if event['type'] == 'customer.subscription.created':
            subscription = event['data']['object']
            customer_id = subscription['customer']
            
            try:
                customer = stripe.Customer.retrieve(customer_id)
                phone = customer.metadata.get('phone')
                
                if phone:
                    phone = normalize_phone_number(phone)
                    add_to_whitelist(phone, send_welcome=True)
                    logger.info(f"✅ Added {phone} to whitelist via Stripe subscription created")
                else:
                    logger.warning(f"⚠️ No phone in customer metadata for subscription created: {customer_id}")
                    
            except Exception as e:
                logger.error(f"Error processing subscription created: {e}")
        
        elif event['type'] == 'customer.subscription.deleted':
            subscription = event['data']['object']
            customer_id = subscription['customer']
            
            try:
                customer = stripe.Customer.retrieve(customer_id)
                phone = customer.metadata.get('phone')
                
                if phone:
                    phone = normalize_phone_number(phone)
                    # Remove from whitelist (implement this function if needed)
                    logger.info(f"❌ Would remove {phone} from whitelist via Stripe subscription deleted")
                else:
                    logger.warning(f"⚠️ No phone in customer metadata for subscription deleted: {customer_id}")
                    
            except Exception as e:
                logger.error(f"Error processing subscription deleted: {e}")
        
        return jsonify({"status": "success"}), 200

# === Admin Endpoints ===
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
        
        success = add_to_whitelist(phone, send_welcome=send_welcome)
        
        if success:
            return jsonify({
                "success": True,
                "message": f"Added {phone} to whitelist",
                "welcome_sent": send_welcome
            })
        else:
            return jsonify({"error": "Failed to add to whitelist"}), 500
            
    except Exception as e:
        logger.error(f"Error in admin add to whitelist: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/users', methods=['GET'])
def get_all_users():
    """Admin endpoint to view all users with their profiles and onboarding status"""
    try:
        rows = execute_query("""
            SELECT 
                up.phone, 
                up.first_name, 
                up.location, 
                up.onboarding_step,
                up.onboarding_completed,
                up.created_date
            FROM user_profiles up
            ORDER BY up.created_date DESC
        """, fetchall=True)
        
        users = []
        for row in rows:
            if USE_POSTGRES:
                users.append({
                    'phone': row['phone'],
                    'first_name': row['first_name'],
                    'location': row['location'],
                    'onboarding_step': row['onboarding_step'],
                    'onboarding_completed': bool(row['onboarding_completed']),
                    'created_date': row['created_date'].isoformat() if row['created_date'] else None
                })
            else:
                users.append({
                    'phone': row[0],
                    'first_name': row[1],
                    'location': row[2],
                    'onboarding_step': row[3],
                    'onboarding_completed': bool(row[4]),
                    'created_date': row[5]
                })
        
        return jsonify({
            'total_users': len(users),
            'users': users
        })
        
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/analytics', methods=['GET'])
def get_analytics():
    """Admin endpoint to view usage analytics"""
    try:
        # Get recent usage analytics
        analytics = execute_query("""
            SELECT intent_type, COUNT(*) as count, AVG(response_time_ms) as avg_response_time
            FROM usage_analytics
            WHERE timestamp > (CURRENT_TIMESTAMP - INTERVAL '7 days')
            GROUP BY intent_type
            ORDER BY count DESC
        """ if USE_POSTGRES else """
            SELECT intent_type, COUNT(*) as count, AVG(response_time_ms) as avg_response_time
            FROM usage_analytics
            WHERE timestamp > datetime('now', '-7 days')
            GROUP BY intent_type
            ORDER BY count DESC
        """, fetchall=True)
        
        # Get monthly usage summary
        monthly_usage = execute_query("""
            SELECT COUNT(*) as total_users, SUM(message_count) as total_messages
            FROM monthly_sms_usage
            WHERE period_start >= (CURRENT_DATE - INTERVAL '30 days')
        """ if USE_POSTGRES else """
            SELECT COUNT(*) as total_users, SUM(message_count) as total_messages
            FROM monthly_sms_usage
            WHERE period_start >= date('now', '-30 days')
        """, fetchone=True)
        
        analytics_data = []
        for row in analytics:
            if USE_POSTGRES:
                analytics_data.append({
                    'intent_type': row['intent_type'],
                    'count': row['count'],
                    'avg_response_time': float(row['avg_response_time']) if row['avg_response_time'] else 0
                })
            else:
                analytics_data.append({
                    'intent_type': row[0],
                    'count': row[1],
                    'avg_response_time': float(row[2]) if row[2] else 0
                })
        
        if USE_POSTGRES:
            monthly_data = {
                'total_users': monthly_usage['total_users'] if monthly_usage else 0,
                'total_messages': monthly_usage['total_messages'] if monthly_usage else 0
            }
        else:
            monthly_data = {
                'total_users': monthly_usage[0] if monthly_usage else 0,
                'total_messages': monthly_usage[1] if monthly_usage else 0
            }
        
        return jsonify({
            'weekly_analytics': analytics_data,
            'monthly_summary': monthly_data
        })
        
    except Exception as e:
        logger.error(f"Error getting analytics: {e}")
        return jsonify({"error": str(e)}), 500

# === Health and Status Endpoints ===
@app.route('/', methods=['GET'])
def home():
    """Home page"""
    return '''
    <h1>Hey Alex SMS Assistant</h1>
    <p><strong>Version:</strong> ''' + APP_VERSION + '''</p>
    <p><strong>Status:</strong> Active and Ready</p>
    <br>
    <p><a href="/health">❤️ Health Check</a></p>
    <p><a href="/admin/users">👥 User Management</a></p>
    <p><a href="/admin/analytics">📊 Analytics</a></p>
    '''

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint with database status"""
    try:
        # Test database connection
        user_result = execute_query("SELECT COUNT(*) as count FROM user_profiles", fetchone=True)
        
        if USE_POSTGRES:
            user_count = user_result['count']
        else:
            user_count = user_result[0]
            
        db_status = "✅ Connected"
    except Exception as e:
        db_status = f"❌ Error: {str(e)}"
        user_count = "unknown"
    
    return jsonify({
        "status": "healthy",
        "version": APP_VERSION,
        "database": {
            "type": f"PostgreSQL (psycopg{PSYCOPG_VERSION})" if USE_POSTGRES else "SQLite",
            "status": db_status,
            "user_count": user_count
        },
        "services": {
            "anthropic": "✅" if anthropic_client else "❌",
            "serpapi": "✅" if SERPAPI_API_KEY else "❌", 
            "clicksend": "✅" if CLICKSEND_USERNAME and CLICKSEND_API_KEY else "❌",
            "stripe": "✅" if STRIPE_AVAILABLE else "❌"
        },
        "features": {
            "claude_ai": "✅" if anthropic_client else "❌",
            "web_search": "✅" if SERPAPI_API_KEY else "❌",
            "sms_sending": "✅" if CLICKSEND_USERNAME and CLICKSEND_API_KEY else "❌",
            "onboarding": "✅",
            "user_profiles": "✅",
            "monthly_quotas": "✅",
            "content_filtering": "✅",
            "rate_limiting": "✅",
            "intent_detection": "✅"
        }
    })

@app.route('/test-db', methods=['GET'])
def test_database():
    """Test database operations"""
    try:
        # Test creating a user profile
        test_phone = "+1234567890"
        
        # Create test user
        if USE_POSTGRES:
            execute_query("""
                INSERT INTO user_profiles (phone, first_name, location, onboarding_completed)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (phone) DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    location = EXCLUDED.location,
                    onboarding_completed = EXCLUDED.onboarding_completed,
                    updated_date = CURRENT_TIMESTAMP
            """, (test_phone, "Test User", "Test City", True))
        else:
            execute_query("""
                INSERT OR REPLACE INTO user_profiles 
                (phone, first_name, location, onboarding_completed)
                VALUES (?, ?, ?, ?)
            """, (test_phone, "Test User", "Test City", True))
        
        # Get user profile
        profile = execute_query("""
            SELECT first_name, location, onboarding_completed
            FROM user_profiles
            WHERE phone = %s
        """ if USE_POSTGRES else """
            SELECT first_name, location, onboarding_completed
            FROM user_profiles
            WHERE phone = ?
        """, (test_phone,), fetchone=True)
        
        # Save test message
        execute_query("""
            INSERT INTO messages (phone, role, content)
            VALUES (%s, %s, %s)
        """ if USE_POSTGRES else """
            INSERT INTO messages (phone, role, content)
            VALUES (?, ?, ?)
        """, (test_phone, "user", "test message"))
        
        # Load history
        history = execute_query("""
            SELECT role, content
            FROM messages
            WHERE phone = %s
            ORDER BY id DESC
            LIMIT 1
        """ if USE_POSTGRES else """
            SELECT role, content
            FROM messages
            WHERE phone = ?
            ORDER BY id DESC
            LIMIT 1
        """, (test_phone,), fetchall=True)
        
        # Format results based on database type
        if USE_POSTGRES:
            profile_dict = dict(profile)
            history_list = [dict(h) for h in history]
        else:
            profile_dict = {"first_name": profile[0], "location": profile[1], "onboarding_completed": bool(profile[2])}
            history_list = [{"role": h[0], "content": h[1]} for h in history]
        
        return jsonify({
            "status": "success",
            "database_type": f"PostgreSQL (psycopg{PSYCOPG_VERSION})" if USE_POSTGRES else "SQLite",
            "test_profile": profile_dict,
            "test_history": history_list
        })
        
    except Exception as e:
        logger.error(f"Database test error: {e}")
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

# Initialize database on startup
init_db()

if __name__ == "__main__":
    logger.info(f"🚀 Starting Hey Alex SMS Assistant v{APP_VERSION}")
    logger.info(f"📋 Latest changes: {CHANGELOG[APP_VERSION]}")
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
