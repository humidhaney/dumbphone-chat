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

# Database imports - PostgreSQL support
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False
    psycopg2 = None
    logging.warning("PostgreSQL not available - falling back to SQLite")

# SQLite fallback
import sqlite3

# Try to import stripe, but handle gracefully if not available
try:
    import stripe
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False
    stripe = None
    logging.warning("Stripe module not available - payment features disabled")

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
APP_VERSION = "2.7"
CHANGELOG = {
    "2.7": "Added PostgreSQL support for persistent data storage on Render",
    "2.6": "Added automatic welcome message when new users are added to whitelist, enhanced whitelist tracking",
    "2.5": "Added Stripe webhook integration for automatic whitelist management based on subscription status",
    "2.4": "Fixed content filter false positives for philosophical questions, improved spam detection accuracy",
}

# === Database Configuration ===
DATABASE_URL = os.getenv("DATABASE_URL")  # Render sets this automatically for PostgreSQL
DB_PATH = os.getenv("DB_PATH", "chat.db")  # Fallback for local SQLite

# Determine database type
USE_POSTGRES = bool(DATABASE_URL and POSTGRES_AVAILABLE)
logger.info(f"🗄️ Database Configuration:")
logger.info(f"  DATABASE_URL: {'✅ Set' if DATABASE_URL else '❌ Missing'}")
logger.info(f"  PostgreSQL Module: {'✅ Available' if POSTGRES_AVAILABLE else '❌ Missing'}")
logger.info(f"  Using: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")

if USE_POSTGRES:
    logger.info(f"🔗 PostgreSQL connection configured")
else:
    logger.info(f"📁 SQLite fallback - path: {DB_PATH}")
    if DATABASE_URL:
        logger.warning("⚠️ DATABASE_URL set but PostgreSQL not available - install psycopg2-binary")

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

# Stripe Configuration - Only if available
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
elif STRIPE_SECRET_KEY and not STRIPE_AVAILABLE:
    logger.warning("❌ STRIPE_SECRET_KEY found but Stripe module not available")
else:
    logger.warning("❌ STRIPE_SECRET_KEY not found - payment features disabled")

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

# === Database Connection Management ===
def get_db_connection():
    """Get database connection based on environment"""
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    else:
        return sqlite3.connect(DB_PATH)

def execute_query(query, params=None, fetch=False, fetchall=False, fetchone=False):
    """Execute database query with proper connection handling"""
    try:
        if USE_POSTGRES:
            with get_db_connection() as conn:
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
    """Initialize database with proper schema for PostgreSQL or SQLite"""
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
                """
            ]
            
            for table_sql in tables:
                execute_query(table_sql)
            
            logger.info("✅ PostgreSQL tables created successfully")
            
        else:
            # SQLite table creation (fallback for local development)
            tables = [
                # Messages table
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
                
                # User profiles table
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
                
                # Other tables...
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
                """
            ]
            
            for table_sql in tables:
                execute_query(table_sql)
            
            logger.info("✅ SQLite tables created successfully")
        
        # Check for existing data
        user_count = execute_query("SELECT COUNT(*) FROM user_profiles", fetchone=True)[0]
        message_count = execute_query("SELECT COUNT(*) FROM messages", fetchone=True)[0]
        
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

# === Database Functions (Updated for PostgreSQL) ===
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
        execute_query("""
            INSERT INTO user_profiles (phone, onboarding_step, onboarding_completed)
            VALUES (%s, 1, FALSE)
            ON CONFLICT (phone) DO NOTHING
        """ if USE_POSTGRES else """
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
        
        if USE_POSTGRES:
            update_parts.append("updated_date = CURRENT_TIMESTAMP")
        else:
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

# === File-based functions (still needed for whitelist.txt) ===
def load_whitelist():
    try:
        with open(WHITELIST_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

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

# === Placeholder functions for features we'll implement ===
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

# === Basic SMS and content filtering (simplified for this migration) ===
def send_sms(to_number, message, bypass_quota=False):
    """Placeholder SMS function"""
    logger.info(f"📤 [SMS PLACEHOLDER] Would send to {to_number}: {message[:50]}...")
    return {"status": "sent"}

class ContentFilter:
    def is_valid_query(self, text):
        return True, ""

content_filter = ContentFilter()

def web_search(query, search_type="general"):
    """Placeholder search function"""
    return f"Search results for: {query}"

def ask_claude(phone, message):
    """Placeholder Claude function"""
    return f"Claude response to: {message[:50]}..."

@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]
    confidence: float = 1.0

def detect_intent(text, phone=None):
    return IntentResult("general", {})

# === Routes ===
@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint with database status"""
    try:
        # Test database connection
        user_count = execute_query("SELECT COUNT(*) FROM user_profiles", fetchone=True)[0]
        db_status = "✅ Connected"
    except Exception as e:
        db_status = f"❌ Error: {str(e)}"
        user_count = "unknown"
    
    return jsonify({
        "status": "healthy",
        "version": APP_VERSION,
        "database": {
            "type": "PostgreSQL" if USE_POSTGRES else "SQLite",
            "status": db_status,
            "user_count": user_count
        },
        "services": {
            "anthropic": "✅" if anthropic_client else "❌",
            "serpapi": "✅" if SERPAPI_API_KEY else "❌", 
            "clicksend": "✅" if CLICKSEND_USERNAME and CLICKSEND_API_KEY else "❌",
            "stripe": "✅" if STRIPE_AVAILABLE else "❌"
        }
    })

@app.route('/admin/users', methods=['GET'])
def get_all_users():
    """Admin endpoint to view all users"""
    try:
        rows = execute_query("""
            SELECT phone, first_name, location, onboarding_step, onboarding_completed, created_date
            FROM user_profiles
            ORDER BY created_date DESC
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

@app.route('/test-db', methods=['GET'])
def test_database():
    """Test database operations"""
    try:
        # Test creating a user profile
        test_phone = "+1234567890"
        
        # Create test user
        create_user_profile(test_phone)
        
        # Update test user
        update_user_profile(test_phone, first_name="Test", location="Test City", onboarding_completed=True)
        
        # Get user profile
        profile = get_user_profile(test_phone)
        
        # Save test message
        save_message(test_phone, "user", "test message")
        
        # Load history
        history = load_history(test_phone, limit=1)
        
        return jsonify({
            "status": "success",
            "database_type": "PostgreSQL" if USE_POSTGRES else "SQLite",
            "test_profile": profile,
            "test_history": history
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
