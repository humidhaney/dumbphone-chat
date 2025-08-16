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

# PostgreSQL support - Try psycopg3 first, fallback to psycopg2
try:
    import psycopg
    from psycopg.rows import dict_row
    POSTGRESQL_VERSION = "psycopg3"
    POSTGRESQL_AVAILABLE = True
    logger.info("✅ PostgreSQL driver (psycopg3) loaded successfully")
except ImportError:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        POSTGRESQL_VERSION = "psycopg2"
        POSTGRESQL_AVAILABLE = True
        logger.info("✅ PostgreSQL driver (psycopg2) loaded successfully")
    except ImportError as e:
        POSTGRESQL_AVAILABLE = False
        POSTGRESQL_VERSION = None
        logger.error(f"❌ No PostgreSQL driver available: {e}")
        logger.error("🚨 CRITICAL: PostgreSQL is required for production!")
        raise ImportError("PostgreSQL driver required for production deployment")

app = Flask(__name__)

# Version tracking
APP_VERSION = "3.0"
CHANGELOG = {
    "3.0": "Migrated whitelist to database storage, enhanced persistence and reliability across deployments",
    "2.9": "Added PostgreSQL support for persistent database storage in production, maintains SQLite for local development",
    "2.8": "Enhanced search capabilities for sports schedules and business hours, improved query specificity",
    "2.7": "Fixed user onboarding persistence logic, enhanced whitelist/profile coordination",
    "2.6": "Added automatic welcome message when new users are added to whitelist, enhanced whitelist tracking",
    "2.5": "Added Stripe webhook integration for automatic whitelist management based on subscription status",
    "2.4": "Fixed content filter false positives for philosophical questions, improved spam detection accuracy",
    "2.3": "Added ClickSend contact list sync, enhanced broadcasting capabilities, and contact management features"
}

# === Config & API Keys ===
CLICKSEND_USERNAME = os.getenv("CLICKSEND_USERNAME")
CLICKSEND_API_KEY = os.getenv("CLICKSEND_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

# Database Configuration - PostgreSQL for production, SQLite for local development
DATABASE_URL = os.getenv("DATABASE_URL")  # This will be set by Render
DB_PATH = os.getenv("DB_PATH", "chat.db")  # Fallback for local development

# Debug API key availability
logger.info(f"🔑 API Keys Status:")
logger.info(f"  CLICKSEND_USERNAME: {'✅ Set' if CLICKSEND_USERNAME else '❌ Missing'}")
logger.info(f"  CLICKSEND_API_KEY: {'✅ Set' if CLICKSEND_API_KEY else '❌ Missing'}")
logger.info(f"  ANTHROPIC_API_KEY: {'✅ Set' if ANTHROPIC_API_KEY else '❌ Missing'}")
logger.info(f"  SERPAPI_API_KEY: {'✅ Set' if SERPAPI_API_KEY else '❌ Missing'}")
logger.info(f"  DATABASE_URL: {'✅ Set (PostgreSQL)' if DATABASE_URL else '❌ Missing (using SQLite)'}")
logger.info(f"  POSTGRESQL_DRIVER: {'✅ Available' if POSTGRESQL_AVAILABLE else '❌ Not Available'}")

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    logger.info("✅ Stripe API initialized successfully")
else:
    logger.warning("❌ STRIPE_SECRET_KEY not found")

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

# Legacy file paths for migration
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

# SUBSCRIPTION MESSAGES
SUBSCRIPTION_WELCOME_MSG = (
    "🎉 Welcome to Hey Alex! Your subscription is now active. "
    "I'm your personal SMS research assistant. Let's get you set up!"
)

SUBSCRIPTION_CANCELLED_MSG = (
    "Thanks for using Hey Alex! Your subscription has been cancelled. "
    "You can resubscribe anytime at heyalex.co to continue using the service."
)

TRIAL_ENDED_MSG = (
    "Your Hey Alex trial has ended. Subscribe at heyalex.co to continue using your personal SMS assistant!"
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
    """Get database connection - PostgreSQL only for production"""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is required for production deployment")
    
    if not POSTGRESQL_AVAILABLE:
        raise ImportError("PostgreSQL driver is required but not available")
    
    try:
        # Parse the DATABASE_URL
        parsed = urlparse(DATABASE_URL)
        
        if POSTGRESQL_VERSION == "psycopg3":
            # Use psycopg3 (modern driver)
            conn = psycopg.connect(
                host=parsed.hostname,
                port=parsed.port,
                dbname=parsed.path[1:],  # Remove leading slash
                user=parsed.username,
                password=parsed.password,
                sslmode='require',
                row_factory=dict_row
            )
            return conn, "postgresql"
        else:
            # Use psycopg2 (legacy driver)
            conn = psycopg2.connect(
                host=parsed.hostname,
                port=parsed.port,
                database=parsed.path[1:],  # Remove leading slash
                user=parsed.username,
                password=parsed.password,
                cursor_factory=RealDictCursor,
                sslmode='require'
            )
            return conn, "postgresql"
            
    except Exception as e:
        logger.error(f"❌ Failed to connect to PostgreSQL: {e}")
        logger.error(f"🔍 Connection details: host={parsed.hostname}, port={parsed.port}, db={parsed.path[1:]}")
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

def extract_phone_from_stripe_metadata(metadata):
    """Extract phone number from Stripe metadata"""
    # Check various possible fields where phone might be stored
    phone_fields = ['phone', 'phone_number', 'mobile', 'cell', 'tel']
    
    for field in phone_fields:
        if field in metadata and metadata[field]:
            return normalize_phone_number(metadata[field])
    
    return None

def log_stripe_event(event_type, customer_id, subscription_id, phone, status, details=None):
    """Log Stripe webhook events for debugging and audit"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO stripe_events 
                        (event_type, customer_id, subscription_id, phone, status, details, timestamp)
                        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    """, (event_type, customer_id, subscription_id, phone, status, json.dumps(details) if details else None))
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    INSERT INTO stripe_events 
                    (event_type, customer_id, subscription_id, phone, status, details, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (event_type, customer_id, subscription_id, phone, status, json.dumps(details) if details else None))
                connection.commit()
        
        logger.info(f"📋 Logged Stripe event: {event_type} for {phone}")
    except Exception as e:
        logger.error(f"Error logging Stripe event: {e}")

# === Database Initialization ===
def init_db():
    try:
        conn, db_type = get_db_connection()
        logger.info(f"🗄️ Initializing {db_type} database")
        
        if db_type == "postgresql":
            # PostgreSQL schema
            tables = [
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id SERIAL PRIMARY KEY,
                    phone TEXT UNIQUE NOT NULL,
                    first_name TEXT,
                    location TEXT,
                    onboarding_step INTEGER DEFAULT 0,
                    onboarding_completed BOOLEAN DEFAULT FALSE,
                    stripe_customer_id TEXT,
                    subscription_status TEXT DEFAULT 'inactive',
                    subscription_id TEXT,
                    trial_end_date TIMESTAMP,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS whitelist (
                    id SERIAL PRIMARY KEY,
                    phone TEXT UNIQUE NOT NULL,
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    added_by TEXT DEFAULT 'system',
                    is_active BOOLEAN DEFAULT TRUE
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    phone TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    intent_type TEXT,
                    response_time_ms INTEGER
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS usage_analytics (
                    id SERIAL PRIMARY KEY,
                    phone TEXT NOT NULL,
                    intent_type TEXT,
                    success BOOLEAN,
                    response_time_ms INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS stripe_events (
                    id SERIAL PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    customer_id TEXT,
                    subscription_id TEXT,
                    phone TEXT,
                    status TEXT,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS onboarding_log (
                    id SERIAL PRIMARY KEY,
                    phone TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    response TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS whitelist_events (
                    id SERIAL PRIMARY KEY,
                    phone TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('added','removed')),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    source TEXT DEFAULT 'manual'
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS sms_delivery_log (
                    id SERIAL PRIMARY KEY,
                    phone TEXT NOT NULL,
                    message_content TEXT NOT NULL,
                    clicksend_response TEXT,
                    delivery_status TEXT,
                    message_id TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS monthly_sms_usage (
                    id SERIAL PRIMARY KEY,
                    phone TEXT NOT NULL,
                    message_count INTEGER DEFAULT 1,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    last_message_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    quota_warnings_sent INTEGER DEFAULT 0,
                    quota_exceeded BOOLEAN DEFAULT FALSE,
                    UNIQUE(phone, period_start)
                );
                """
            ]
            
            indexes = [
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_phone ON user_profiles(phone);",
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_customer ON user_profiles(stripe_customer_id);",
                "CREATE INDEX IF NOT EXISTS idx_whitelist_phone ON whitelist(phone);",
                "CREATE INDEX IF NOT EXISTS idx_whitelist_active ON whitelist(is_active);",
                "CREATE INDEX IF NOT EXISTS idx_messages_phone_ts ON messages(phone, ts DESC);",
                "CREATE INDEX IF NOT EXISTS idx_usage_analytics_phone_ts ON usage_analytics(phone, timestamp DESC);",
                "CREATE INDEX IF NOT EXISTS idx_stripe_events_customer ON stripe_events(customer_id);",
                "CREATE INDEX IF NOT EXISTS idx_monthly_usage_phone_period ON monthly_sms_usage(phone, period_start DESC);"
            ]
            
            
            if POSTGRESQL_VERSION == "psycopg3":
                # psycopg3 - auto-commit by default, no need for 'with conn:'
                with conn.cursor() as cursor:
                    for table in tables:
                        cursor.execute(table)
                    for index in indexes:
                        cursor.execute(index)
                conn.commit()  # Explicit commit for schema changes
            else:
                # psycopg2 - needs 'with conn:' for auto-commit
                with conn:
                    with conn.cursor() as cursor:
                        for table in tables:
                            cursor.execute(table)
                        for index in indexes:
                            cursor.execute(index)
            
            logger.info("✅ PostgreSQL database initialized successfully")
            
        else:
            # SQLite schema for local development
            with closing(conn) as connection:
                c = connection.cursor()
                
                # User profiles table
                c.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE NOT NULL,
                    first_name TEXT,
                    location TEXT,
                    onboarding_step INTEGER DEFAULT 0,
                    onboarding_completed BOOLEAN DEFAULT FALSE,
                    stripe_customer_id TEXT,
                    subscription_status TEXT DEFAULT 'inactive',
                    subscription_id TEXT,
                    trial_end_date DATETIME,
                    created_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_date DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                # Whitelist table
                c.execute("""
                CREATE TABLE IF NOT EXISTS whitelist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE NOT NULL,
                    added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                    added_by TEXT DEFAULT 'system',
                    is_active BOOLEAN DEFAULT TRUE
                );
                """)
                
                # Messages table
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
                
                # Stripe events table
                c.execute("""
                CREATE TABLE IF NOT EXISTS stripe_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    customer_id TEXT,
                    subscription_id TEXT,
                    phone TEXT,
                    status TEXT,
                    details TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                # Onboarding log table
                c.execute("""
                CREATE TABLE IF NOT EXISTS onboarding_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    response TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """)
                
                # Whitelist events table
                c.execute("""
                CREATE TABLE IF NOT EXISTS whitelist_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('added','removed')),
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    source TEXT DEFAULT 'manual'
                );
                """)
                
                # SMS delivery log table
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
                
                # Monthly SMS usage table
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
                
                # Create indexes for SQLite
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_profiles_phone ON user_profiles(phone);")
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_profiles_customer ON user_profiles(stripe_customer_id);")
                c.execute("CREATE INDEX IF NOT EXISTS idx_whitelist_phone ON whitelist(phone);")
                c.execute("CREATE INDEX IF NOT EXISTS idx_whitelist_active ON whitelist(is_active);")
                c.execute("CREATE INDEX IF NOT EXISTS idx_messages_phone_ts ON messages(phone, ts DESC);")
                c.execute("CREATE INDEX IF NOT EXISTS idx_usage_analytics_phone_ts ON usage_analytics(phone, timestamp DESC);")
                c.execute("CREATE INDEX IF NOT EXISTS idx_stripe_events_customer ON stripe_events(customer_id);")
                c.execute("CREATE INDEX IF NOT EXISTS idx_monthly_usage_phone_period ON monthly_sms_usage(phone, period_start DESC);")
                
                connection.commit()
                logger.info("✅ SQLite database initialized successfully")
        
        # Migrate legacy whitelist file if it exists
        migrate_legacy_whitelist()
        
        # Check existing data
        if db_type == "postgresql":
            if POSTGRESQL_VERSION == "psycopg3":
                with conn.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM user_profiles")
                    user_count = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) FROM messages")
                    message_count = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) FROM whitelist WHERE is_active = TRUE")
                    whitelist_count = cursor.fetchone()['count']
            else:
                with conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT COUNT(*) FROM user_profiles")
                        user_count = cursor.fetchone()['count']
                        
                        cursor.execute("SELECT COUNT(*) FROM messages")
                        message_count = cursor.fetchone()['count']
                        
                        cursor.execute("SELECT COUNT(*) FROM whitelist WHERE is_active = TRUE")
                        whitelist_count = cursor.fetchone()['count']
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("SELECT COUNT(*) FROM user_profiles")
                user_count = c.fetchone()[0]
                
                c.execute("SELECT COUNT(*) FROM messages")
                message_count = c.fetchone()[0]
                
                c.execute("SELECT COUNT(*) FROM whitelist WHERE is_active = TRUE")
                whitelist_count = c.fetchone()[0]
        
        logger.info(f"📊 Database ready: {user_count} users, {message_count} messages, {whitelist_count} whitelisted")
        
        conn.close()  # Close connection for both versions
        
    except Exception as e:
        logger.error(f"💥 Database initialization error: {e}")
        raise

def migrate_legacy_whitelist():
    """Migrate whitelist from text file to database if file exists"""
    try:
        if os.path.exists(WHITELIST_FILE):
            logger.info("📁 Found legacy whitelist file, migrating to database...")
            
            with open(WHITELIST_FILE, 'r') as f:
                legacy_phones = [line.strip() for line in f if line.strip()]
            
            if legacy_phones:
                conn, db_type = get_db_connection()
                migrated_count = 0
                
                for phone in legacy_phones:
                    try:
                        normalized_phone = normalize_phone_number(phone)
                        if normalized_phone:
                            if db_type == "postgresql":
                                with conn:
                                    with conn.cursor() as cursor:
                                        cursor.execute("""
                                            INSERT INTO whitelist (phone, added_by, added_date)
                                            VALUES (%s, 'legacy_migration', CURRENT_TIMESTAMP)
                                            ON CONFLICT (phone) DO NOTHING
                                        """, (normalized_phone,))
                            else:
                                with closing(conn) as connection:
                                    c = connection.cursor()
                                    c.execute("""
                                        INSERT OR IGNORE INTO whitelist (phone, added_by, added_date)
                                        VALUES (?, 'legacy_migration', CURRENT_TIMESTAMP)
                                    """, (normalized_phone,))
                                    connection.commit()
                            
                            migrated_count += 1
                    except Exception as phone_error:
                        logger.error(f"Error migrating phone {phone}: {phone_error}")
                
                logger.info(f"✅ Migrated {migrated_count} numbers from legacy whitelist")
                
                # Backup the old file
                backup_file = f"{WHITELIST_FILE}.backup.{int(time.time())}"
                os.rename(WHITELIST_FILE, backup_file)
                logger.info(f"📁 Legacy whitelist backed up to {backup_file}")
                
                if db_type == "postgresql":
                    conn.close()
            else:
                logger.info("📁 Legacy whitelist file was empty")
        else:
            logger.info("📁 No legacy whitelist file found")
            
    except Exception as e:
        logger.error(f"Error migrating legacy whitelist: {e}")

# === Enhanced Whitelist Management with Database Storage ===
def load_whitelist():
    """Load active whitelist numbers from database"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT phone FROM whitelist WHERE is_active = TRUE")
                    results = cursor.fetchall()
                    return set(row['phone'] for row in results)
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("SELECT phone FROM whitelist WHERE is_active = TRUE")
                results = c.fetchall()
                return set(row[0] for row in results)
                
    except Exception as e:
        logger.error(f"Error loading whitelist from database: {e}")
        # Fallback to file-based whitelist if database fails
        try:
            if os.path.exists(WHITELIST_FILE):
                with open(WHITELIST_FILE, "r") as f:
                    return set(line.strip() for line in f if line.strip())
        except Exception as file_error:
            logger.error(f"Fallback file whitelist also failed: {file_error}")
        return set()

def add_to_whitelist(phone, send_welcome=True, source='manual'):
    """Enhanced whitelist addition with database storage"""
    if not phone:
        return False
        
    phone = normalize_phone_number(phone)
    
    try:
        conn, db_type = get_db_connection()
        
        # Check if already exists
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT is_active FROM whitelist WHERE phone = %s", (phone,))
                    existing = cursor.fetchone()
                    
                    if existing:
                        if not existing['is_active']:
                            # Reactivate existing entry
                            cursor.execute("""
                                UPDATE whitelist 
                                SET is_active = TRUE, added_date = CURRENT_TIMESTAMP, added_by = %s
                                WHERE phone = %s
                            """, (source, phone))
                            logger.info(f"📱 Reactivated {phone} in whitelist")
                        else:
                            logger.info(f"📱 {phone} already in active whitelist")
                            return True
                    else:
                        # Add new entry
                        cursor.execute("""
                            INSERT INTO whitelist (phone, added_by, added_date, is_active)
                            VALUES (%s, %s, CURRENT_TIMESTAMP, TRUE)
                        """, (phone, source))
                        logger.info(f"📱 Added new user {phone} to whitelist")
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("SELECT is_active FROM whitelist WHERE phone = ?", (phone,))
                existing = c.fetchone()
                
                if existing:
                    if not existing[0]:
                        # Reactivate existing entry
                        c.execute("""
                            UPDATE whitelist 
                            SET is_active = 1, added_date = CURRENT_TIMESTAMP, added_by = ?
                            WHERE phone = ?
                        """, (source, phone))
                        logger.info(f"📱 Reactivated {phone} in whitelist")
                    else:
                        logger.info(f"📱 {phone} already in active whitelist")
                        return True
                else:
                    # Add new entry
                    c.execute("""
                        INSERT INTO whitelist (phone, added_by, added_date, is_active)
                        VALUES (?, ?, CURRENT_TIMESTAMP, 1)
                    """, (phone, source))
                    logger.info(f"📱 Added new user {phone} to whitelist")
                
                connection.commit()
        
        # Log the event
        log_whitelist_event(phone, "added", source)
        
        # Create user profile if it doesn't exist
        profile = get_user_profile(phone)
        if not profile:
            create_user_profile(phone)
        
        # Send welcome message for new users
        if send_welcome:
            try:
                send_sms(phone, ONBOARDING_NAME_MSG, bypass_quota=True)
                logger.info(f"🎉 Onboarding started for new user {phone}")
                
                # Log the welcome message
                save_message(phone, "assistant", ONBOARDING_NAME_MSG, "onboarding_start", 0)
                
            except Exception as sms_error:
                logger.error(f"Failed to send onboarding SMS to {phone}: {sms_error}")
        
        if db_type == "postgresql":
            conn.close()
            
        return True
        
    except Exception as e:
        logger.error(f"Failed to add {phone} to whitelist: {e}")
        return False

def remove_from_whitelist(phone, send_goodbye=False, source='manual'):
    """Enhanced whitelist removal with database storage"""
    if not phone:
        return False
        
    phone = normalize_phone_number(phone)
    
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE whitelist 
                        SET is_active = FALSE 
                        WHERE phone = %s AND is_active = TRUE
                    """, (phone,))
                    
                    if cursor.rowcount > 0:
                        logger.info(f"📱 Removed {phone} from whitelist (source: {source})")
                        removed = True
                    else:
                        logger.info(f"📱 {phone} was not in active whitelist")
                        removed = False
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    UPDATE whitelist 
                    SET is_active = 0 
                    WHERE phone = ? AND is_active = 1
                """, (phone,))
                
                if c.rowcount > 0:
                    logger.info(f"📱 Removed {phone} from whitelist (source: {source})")
                    removed = True
                else:
                    logger.info(f"📱 {phone} was not in active whitelist")
                    removed = False
                
                connection.commit()
        
        if removed:
            # Log the removal
            log_whitelist_event(phone, "removed", source)
            
            # Send goodbye message if requested
            if send_goodbye:
                try:
                    send_sms(phone, SUBSCRIPTION_CANCELLED_MSG, bypass_quota=True)
                    logger.info(f"👋 Goodbye message sent to {phone}")
                except Exception as sms_error:
                    logger.error(f"Failed to send goodbye SMS to {phone}: {sms_error}")
        
        if db_type == "postgresql":
            conn.close()
            
        return True
        
    except Exception as e:
        logger.error(f"Failed to remove {phone} from whitelist: {e}")
        return False

def get_whitelist_stats():
    """Get whitelist statistics from database"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM whitelist WHERE is_active = TRUE")
                    active_count = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) FROM whitelist WHERE is_active = FALSE")
                    inactive_count = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) FROM whitelist")
                    total_count = cursor.fetchone()['count']
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("SELECT COUNT(*) FROM whitelist WHERE is_active = 1")
                active_count = c.fetchone()[0]
                
                c.execute("SELECT COUNT(*) FROM whitelist WHERE is_active = 0")
                inactive_count = c.fetchone()[0]
                
                c.execute("SELECT COUNT(*) FROM whitelist")
                total_count = c.fetchone()[0]
        
        return {
            "active": active_count,
            "inactive": inactive_count,
            "total": total_count
        }
        
    except Exception as e:
        logger.error(f"Error getting whitelist stats: {e}")
        return {"active": 0, "inactive": 0, "total": 0}

# === User Profile Management ===
def get_user_profile(phone):
    """Get user profile and onboarding status"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT first_name, location, onboarding_step, onboarding_completed,
                               stripe_customer_id, subscription_status, subscription_id, trial_end_date
                        FROM user_profiles
                        WHERE phone = %s
                    """, (phone,))
                    result = cursor.fetchone()
            
            if result:
                return {
                    'first_name': result['first_name'],
                    'location': result['location'],
                    'onboarding_step': result['onboarding_step'],
                    'onboarding_completed': bool(result['onboarding_completed']),
                    'stripe_customer_id': result['stripe_customer_id'],
                    'subscription_status': result['subscription_status'],
                    'subscription_id': result['subscription_id'],
                    'trial_end_date': result['trial_end_date']
                }
        else:
            # SQLite
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    SELECT first_name, location, onboarding_step, onboarding_completed,
                           stripe_customer_id, subscription_status, subscription_id, trial_end_date
                    FROM user_profiles
                    WHERE phone = ?
                """, (phone,))
                result = c.fetchone()
                
                if result:
                    return {
                        'first_name': result[0],
                        'location': result[1],
                        'onboarding_step': result[2],
                        'onboarding_completed': bool(result[3]),
                        'stripe_customer_id': result[4],
                        'subscription_status': result[5],
                        'subscription_id': result[6],
                        'trial_end_date': result[7]
                    }
        
        return None
            
    except Exception as e:
        logger.error(f"Error getting user profile for {phone}: {e}")
        return None

def create_user_profile(phone, stripe_customer_id=None, subscription_status='inactive'):
    """Create new user profile for onboarding"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO user_profiles 
                        (phone, onboarding_step, onboarding_completed, stripe_customer_id, subscription_status)
                        VALUES (%s, 1, FALSE, %s, %s)
                        ON CONFLICT (phone) DO NOTHING
                    """, (phone, stripe_customer_id, subscription_status))
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    INSERT OR IGNORE INTO user_profiles 
                    (phone, onboarding_step, onboarding_completed, stripe_customer_id, subscription_status)
                    VALUES (?, 1, FALSE, ?, ?)
                """, (phone, stripe_customer_id, subscription_status))
                connection.commit()
        
        logger.info(f"📝 Created user profile for {phone} with status {subscription_status}")
        return True
    except Exception as e:
        logger.error(f"Error creating user profile for {phone}: {e}")
        return False

def update_user_profile(phone, **kwargs):
    """Update user profile information with flexible field updates"""
    try:
        conn, db_type = get_db_connection()
        
        allowed_fields = [
            'first_name', 'location', 'onboarding_step', 'onboarding_completed',
            'stripe_customer_id', 'subscription_status', 'subscription_id', 'trial_end_date'
        ]
        
        update_parts = []
        params = []
        
        for field, value in kwargs.items():
            if field in allowed_fields:
                if db_type == "postgresql":
                    update_parts.append(f"{field} = %s")
                else:
                    update_parts.append(f"{field} = ?")
                params.append(value)
        
        if not update_parts:
            logger.warning(f"No valid fields to update for {phone}")
            return False
        
        if db_type == "postgresql":
            update_parts.append("updated_date = CURRENT_TIMESTAMP")
            params.append(phone)
            
            query = f"""
                UPDATE user_profiles 
                SET {', '.join(update_parts)}
                WHERE phone = %s
            """
            
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, params)
        else:
            update_parts.append("updated_date = CURRENT_TIMESTAMP")
            params.append(phone)
            
            query = f"""
                UPDATE user_profiles 
                SET {', '.join(update_parts)}
                WHERE phone = ?
            """
            
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute(query, params)
                connection.commit()
        
        logger.info(f"📝 Updated user profile for {phone}: {kwargs}")
        return True
        
    except Exception as e:
        logger.error(f"Error updating user profile for {phone}: {e}")
        return False

def get_user_by_customer_id(customer_id):
    """Get user profile by Stripe customer ID"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT phone, first_name, location, subscription_status
                        FROM user_profiles
                        WHERE stripe_customer_id = %s
                    """, (customer_id,))
                    result = cursor.fetchone()
                    
                    if result:
                        return {
                            'phone': result['phone'],
                            'first_name': result['first_name'], 
                            'location': result['location'],
                            'subscription_status': result['subscription_status']
                        }
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    SELECT phone, first_name, location, subscription_status
                    FROM user_profiles
                    WHERE stripe_customer_id = ?
                """, (customer_id,))
                result = c.fetchone()
                
                if result:
                    return {
                        'phone': result[0],
                        'first_name': result[1], 
                        'location': result[2],
                        'subscription_status': result[3]
                    }
        
        return None
    except Exception as e:
        logger.error(f"Error getting user by customer ID {customer_id}: {e}")
        return None

# === Onboarding System ===
def log_onboarding_step(phone, step, response):
    """Log onboarding step response"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO onboarding_log (phone, step, response)
                        VALUES (%s, %s, %s)
                    """, (phone, step, response))
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    INSERT INTO onboarding_log (phone, step, response)
                    VALUES (?, ?, ?)
                """, (phone, step, response))
                connection.commit()
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

def log_whitelist_event(phone, action, source='manual'):
    """Log whitelist addition/removal events"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO whitelist_events (phone, action, source, timestamp)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    """, (phone, action, source))
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    INSERT INTO whitelist_events (phone, action, source, timestamp)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """, (phone, action, source))
                connection.commit()
        
        logger.info(f"📋 Logged whitelist event: {action} for {phone} (source: {source})")
    except Exception as e:
        logger.error(f"Error logging whitelist event: {e}")

# === Message and Analytics Functions ===
def save_message(phone, role, content, intent_type=None, response_time_ms=None):
    """Save message using new connection method"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO messages (phone, role, content, intent_type, response_time_ms) 
                        VALUES (%s, %s, %s, %s, %s)
                    """, (phone, role, content, intent_type, response_time_ms))
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    INSERT INTO messages (phone, role, content, intent_type, response_time_ms) 
                    VALUES (?, ?, ?, ?, ?)
                """, (phone, role, content, intent_type, response_time_ms))
                connection.commit()
                
    except Exception as e:
        logger.error(f"Error saving message: {e}")

def load_history(phone, limit=4):
    """Load conversation history"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT role, content
                        FROM messages
                        WHERE phone = %s
                        ORDER BY id DESC
                        LIMIT %s
                    """, (phone, limit))
                    rows = cursor.fetchall()
                    return [{"role": row['role'], "content": row['content']} for row in reversed(rows)]
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    SELECT role, content
                    FROM messages
                    WHERE phone = ?
                    ORDER BY id DESC
                    LIMIT ?
                """, (phone, limit))
                rows = c.fetchall()
                return [{"role": r, "content": t} for (r, t) in reversed(rows)]
    except Exception as e:
        logger.error(f"Error loading history: {e}")
        return []

def log_usage_analytics(phone, intent_type, success, response_time_ms):
    """Log usage analytics with error handling"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO usage_analytics (phone, intent_type, success, response_time_ms)
                        VALUES (%s, %s, %s, %s)
                    """, (phone, intent_type, success, response_time_ms))
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    INSERT INTO usage_analytics (phone, intent_type, success, response_time_ms)
                    VALUES (?, ?, ?, ?)
                """, (phone, intent_type, success, response_time_ms))
                connection.commit()
    except Exception as e:
        logger.error(f"Usage analytics logging error: {e}")

# === SMS and Usage Tracking Functions ===
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
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO sms_delivery_log (phone, message_content, clicksend_response, delivery_status, message_id)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (phone, message_content, json.dumps(clicksend_response), delivery_status, message_id))
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    INSERT INTO sms_delivery_log (phone, message_content, clicksend_response, delivery_status, message_id)
                    VALUES (?, ?, ?, ?, ?)
                """, (phone, message_content, json.dumps(clicksend_response), delivery_status, message_id))
                connection.commit()
    except Exception as e:
        logger.error(f"Error logging SMS delivery: {e}")

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
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT id, message_count, quota_warnings_sent, quota_exceeded
                        FROM monthly_sms_usage
                        WHERE phone = %s AND period_start = %s
                    """, (phone, period_start))
                    
                    result = cursor.fetchone()
                    
                    if result:
                        usage_id, current_count, warnings_sent, quota_exceeded = result['id'], result['message_count'], result['quota_warnings_sent'], result['quota_exceeded']
                        new_count = current_count + 1
                        
                        cursor.execute("""
                            UPDATE monthly_sms_usage 
                            SET message_count = %s, last_message_date = CURRENT_TIMESTAMP
                            WHERE id = %s
                        """, (new_count, usage_id))
                    else:
                        new_count = 1
                        warnings_sent = 0
                        quota_exceeded = False
                        
                        cursor.execute("""
                            INSERT INTO monthly_sms_usage 
                            (phone, message_count, period_start, period_end, quota_warnings_sent, quota_exceeded)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (phone, new_count, period_start, period_end, warnings_sent, quota_exceeded))
        else:
            with closing(conn) as connection:
                c = connection.cursor()
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
                
                connection.commit()
        
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
        logger.error(f"Error tracking SMS usage: {e}")
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

# === Enhanced Intent Detection ===
@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]
    confidence: float = 1.0

def detect_sports_schedule_intent(text: str) -> Optional[IntentResult]:
    """Detect when user is asking about sports schedules"""
    sports_schedule_patterns = [
        r'\b(when|what time)\s+(?:is|are)\s+(?:the\s+)?(?:next|upcoming)\s+(.+?)\s+(game|match)\b',
        r'\bnext\s+(.+?)\s+(game|match)\b',
        r'\b(.+?)\s+(schedule|game time|next game)\b',
        r'\bwhen\s+(?:do|does)\s+(?:the\s+)?(.+?)\s+play\b'
    ]
    
    for pattern in sports_schedule_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            # Extract team name
            team_parts = match.groups()
            team = None
            for part in team_parts:
                if part and part.lower() not in ['game', 'match', 'schedule', 'next', 'time']:
                    team = part.strip()
                    break
            
            return IntentResult("sports_schedule", {
                "team": team,
                "query": text
            })
    
    return None

def detect_business_hours_intent(text: str) -> Optional[IntentResult]:
    """Detect when user is asking about business hours"""
    hours_patterns = [
        r'\b(?:what\s+time|when)\s+(?:does|do|is|are)\s+(.+?)\s+(?:open|close|hours)\b',
        r'\b(.+?)\s+(?:hours|open|close)\b',
        r'\bis\s+(.+?)\s+open\b'
    ]
    
    for pattern in hours_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            business = match.group(1).strip()
            return IntentResult("business_hours", {
                "business": business,
                "query": text
            })
    
    return None

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
    """Enhanced intent detection with sports and business queries"""
    
    # Check for sports schedule queries first
    sports_intent = detect_sports_schedule_intent(text)
    if sports_intent:
        return sports_intent
    
    # Check for business hours
    hours_intent = detect_business_hours_intent(text)
    if hours_intent:
        return hours_intent
    
    # Check for weather
    weather_intent = detect_weather_intent(text)
    if weather_intent:
        return weather_intent
    
    return None

# === Enhanced Web Search Functions ===
def enhanced_web_search(query, search_type="general", user_location=None):
    """Enhanced search with better query construction for specific types"""
    
    if not SERPAPI_API_KEY:
        logger.warning("❌ SERPAPI_API_KEY not configured - search unavailable")
        return "I'd love to search for that information, but my search service isn't configured right now. Please contact support."
    
    # Enhance the query based on search type
    if search_type == "sports_schedule":
        # For sports, we want current season schedule with dates
        current_year = datetime.now().year
        enhanced_query = f"{query} {current_year} schedule next game date time"
        
    elif search_type == "business_hours":
        # For business hours, add location context
        if user_location:
            enhanced_query = f"{query} hours {user_location}"
        else:
            enhanced_query = f"{query} hours open close times"
            
    else:
        enhanced_query = query
    
    logger.info(f"🔍 Enhanced search query: {enhanced_query}")
    
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google",
        "q": enhanced_query,
        "num": 5,  # Get more results to find better info
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
        "gl": "us",
    }
    
    try:
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

    # Enhanced result processing based on search type
    if search_type == "sports_schedule":
        return process_sports_results(data, query)
    elif search_type == "business_hours":
        return process_business_hours_results(data, query)
    else:
        return process_general_results(data, query)

def process_sports_results(data, original_query):
    """Process search results specifically for sports schedules"""
    
    # Look for answer box or featured snippet first
    if "answer_box" in data:
        answer = data["answer_box"]
        if "answer" in answer:
            return f"{answer['answer']}"
        elif "snippet" in answer:
            return f"{answer['snippet']}"
    
    # Look for sports results
    if "sports_results" in data:
        sports = data["sports_results"]
        if "games" in sports and sports["games"]:
            next_game = sports["games"][0]
            
            # Extract game info
            date = next_game.get("date", "")
            time = next_game.get("time", "")
            teams = next_game.get("teams", [])
            
            if len(teams) >= 2:
                team1 = teams[0].get("name", "")
                team2 = teams[1].get("name", "")
                
                result = f"Next game: {team1} vs {team2}"
                if date:
                    result += f" on {date}"
                if time:
                    result += f" at {time}"
                
                return result
    
    # Fall back to organic results
    org = data.get("organic_results", [])
    if org:
        top = org[0]
        title = top.get("title", "")
        snippet = top.get("snippet", "")
        
        result = f"{title}"
        if snippet:
            result += f" — {snippet}"
        return result[:500]
    
    return f"I couldn't find specific schedule information for {original_query}. Try checking the team's official website."

def process_business_hours_results(data, original_query):
    """Process search results specifically for business hours"""
    
    # Look for knowledge panel with hours
    if "knowledge_graph" in data:
        kg = data["knowledge_graph"]
        if "hours" in kg:
            hours = kg["hours"]
            return f"Hours: {hours}"
        elif "description" in kg:
            desc = kg["description"]
            if any(word in desc.lower() for word in ['open', 'hours', 'am', 'pm']):
                return desc
    
    # Look for local results with hours
    if "local_results" in data and data["local_results"]:
        local = data["local_results"][0]
        
        name = local.get("title", "")
        hours = local.get("hours", "")
        
        if hours:
            return f"{name} — {hours}"
    
    # Fall back to organic results
    return process_general_results(data, original_query)

def process_general_results(data, original_query):
    """Process general search results"""
    org = data.get("organic_results", [])
    if org:
        top = org[0]
        title = top.get("title", "")
        snippet = top.get("snippet", "")
        
        result = f"{title}"
        if snippet:
            result += f" — {snippet}"
        return result[:500]
    
    return f"No results found for '{original_query}'."

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
                search_result = enhanced_web_search(search_term, search_type="general")
                return search_result
        
        if len(reply) > 500:
            reply = reply[:497] + "..."
            
        response_time = int((time.time() - start_time) * 1000)
        
        try:
            log_usage_analytics(phone, "claude_chat", True, response_time)
        except Exception as analytics_error:
            logger.error(f"Analytics logging failed: {analytics_error}")
        
        return reply
        
    except Exception as e:
        logger.error(f"💥 Claude integration error for {phone}: {e}")
        return "I'm having trouble processing that question. Let me try to search for that information instead."

# === Enhanced Query Processing ===
def process_user_query(sender, body, user_context):
    """Enhanced query processing with better intent handling"""
    
    intent = detect_intent(body, sender)
    intent_type = intent.type if intent else "general"
    
    logger.info(f"🎯 Detected intent: {intent_type}")
    
    try:
        if intent and intent.type == "sports_schedule":
            # Handle sports schedule queries
            team = intent.entities.get("team", "")
            logger.info(f"🏈 Sports schedule query for team: {team}")
            
            response_msg = enhanced_web_search(
                body, 
                search_type="sports_schedule", 
                user_location=user_context.get('location')
            )
            
            if user_context['personalized']:
                first_name = user_context['first_name']
                response_msg = f"Hi {first_name}! " + response_msg
                
        elif intent and intent.type == "business_hours":
            # Handle business hours queries
            business = intent.entities.get("business", "")
            logger.info(f"🏪 Business hours query for: {business}")
            
            response_msg = enhanced_web_search(
                body, 
                search_type="business_hours", 
                user_location=user_context.get('location')
            )
            
        elif intent and intent.type == "weather":
            # Use user's location if available
            if user_context['personalized']:
                city = user_context['location']
                logger.info(f"🌍 Using user's saved location: {city}")
                query = f"weather forecast {city}"
                response_msg = enhanced_web_search(query, search_type="general")
                first_name = user_context['first_name']
                response_msg = f"Hi {first_name}! " + response_msg
            else:
                response_msg = enhanced_web_search("weather forecast", search_type="general")
                
        else:
            # Use Claude for general queries
            if user_context['personalized']:
                personalized_msg = f"User's name is {user_context['first_name']} and they live in {user_context['location']}. " + body
                response_msg = ask_claude(sender, personalized_msg)
            else:
                response_msg = ask_claude(sender, body)
            
            # If Claude suggests a search, perform it with enhanced search
            if "Let me search for" in response_msg:
                search_term = body
                # Add location context to search if available
                if user_context['personalized'] and not any(keyword in body.lower() for keyword in ['in ', 'near ', 'at ']):
                    search_term += f" in {user_context['location']}"
                response_msg = enhanced_web_search(search_term, search_type="general")
        
        return response_msg, intent_type
        
    except Exception as e:
        logger.error(f"💥 Query processing error: {e}")
        return "Sorry, I'm having trouble processing your request. Please try again.", "error"

# === Stripe Webhook Handlers ===
@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events for subscription management"""
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    
    if not STRIPE_WEBHOOK_SECRET:
        logger.error("❌ STRIPE_WEBHOOK_SECRET not configured")
        return jsonify({'error': 'Webhook secret not configured'}), 500
    
    try:
        # Verify webhook signature
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
        logger.info(f"✅ Verified Stripe webhook: {event['type']}")
        
    except ValueError as e:
        logger.error(f"❌ Invalid payload in Stripe webhook: {e}")
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"❌ Invalid signature in Stripe webhook: {e}")
        return jsonify({'error': 'Invalid signature'}), 400
    
    # Handle the event
    try:
        if event['type'] == 'customer.subscription.created':
            handle_subscription_created(event['data']['object'])
        
        elif event['type'] == 'customer.subscription.updated':
            handle_subscription_updated(event['data']['object'])
        
        elif event['type'] == 'customer.subscription.deleted':
            handle_subscription_deleted(event['data']['object'])
        
        elif event['type'] == 'customer.subscription.trial_will_end':
            handle_trial_will_end(event['data']['object'])
        
        elif event['type'] == 'invoice.payment_succeeded':
            handle_payment_succeeded(event['data']['object'])
        
        elif event['type'] == 'invoice.payment_failed':
            handle_payment_failed(event['data']['object'])
        
        else:
            logger.info(f"ℹ️ Unhandled Stripe event type: {event['type']}")
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"💥 Error processing Stripe webhook: {e}")
        return jsonify({'error': 'Webhook processing failed'}), 500

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

def handle_subscription_updated(subscription):
    """Handle subscription status changes"""
    customer_id = subscription['customer']
    subscription_id = subscription['id']
    status = subscription['status']
    
    logger.info(f"🔄 Subscription updated: {subscription_id} status: {status}")
    
    try:
        # Find user by customer ID
        user = get_user_by_customer_id(customer_id)
        
        if user:
            phone = user['phone']
            
            # Update subscription status
            update_user_profile(
                phone,
                subscription_status=status,
                subscription_id=subscription_id
            )
            
            # Handle status changes
            if status == 'active':
                # Subscription is active - ensure user is in whitelist
                add_to_whitelist(phone, send_welcome=False, source='stripe_reactivation')
                logger.info(f"✅ Subscription reactivated for {phone}")
                
            elif status in ['canceled', 'unpaid', 'past_due']:
                # Subscription ended - remove from whitelist
                remove_from_whitelist(phone, send_goodbye=True, source='stripe_cancellation')
                logger.info(f"❌ Subscription ended for {phone}")
            
            # Log the event
            log_stripe_event('subscription_updated', customer_id, subscription_id, phone, status)
            
        else:
            logger.warning(f"⚠️ No user found for customer {customer_id}")
            log_stripe_event('subscription_updated', customer_id, subscription_id, None, status,
                           {'error': 'User not found'})
        
    except Exception as e:
        logger.error(f"❌ Error handling subscription update: {e}")
        log_stripe_event('subscription_updated', customer_id, subscription_id, None, 'error',
                        {'error': str(e)})

def handle_subscription_deleted(subscription):
    """Handle subscription cancellation"""
    customer_id = subscription['customer']
    subscription_id = subscription['id']
    
    logger.info(f"❌ Subscription deleted: {subscription_id}")
    
    try:
        # Find user by customer ID
        user = get_user_by_customer_id(customer_id)
        
        if user:
            phone = user['phone']
            
            # Update subscription status
            update_user_profile(
                phone,
                subscription_status='canceled',
                subscription_id=None
            )
            
            # Remove from whitelist
            remove_from_whitelist(phone, send_goodbye=True, source='stripe_cancellation')
            
            # Log the event
            log_stripe_event('subscription_deleted', customer_id, subscription_id, phone, 'canceled')
            
            logger.info(f"✅ Subscription cancellation processed for {phone}")
        else:
            logger.warning(f"⚠️ No user found for customer {customer_id}")
            log_stripe_event('subscription_deleted', customer_id, subscription_id, None, 'canceled',
                           {'error': 'User not found'})
        
    except Exception as e:
        logger.error(f"❌ Error handling subscription deletion: {e}")
        log_stripe_event('subscription_deleted', customer_id, subscription_id, None, 'error',
                        {'error': str(e)})

def handle_trial_will_end(subscription):
    """Handle trial ending soon"""
    customer_id = subscription['customer']
    subscription_id = subscription['id']
    
    logger.info(f"⏰ Trial will end soon for subscription: {subscription_id}")
    
    try:
        # Find user by customer ID
        user = get_user_by_customer_id(customer_id)
        
        if user and user['phone']:
            phone = user['phone']
            
            # Send trial ending notification
            trial_msg = "⏰ Your Hey Alex trial ends soon! Subscribe at heyalex.co to continue using your personal SMS assistant."
            send_sms(phone, trial_msg, bypass_quota=True)
            
            # Log the event
            log_stripe_event('trial_will_end', customer_id, subscription_id, phone, 'trial_ending')
            
            logger.info(f"📧 Trial ending notification sent to {phone}")
        
    except Exception as e:
        logger.error(f"❌ Error handling trial will end: {e}")

def handle_payment_succeeded(invoice):
    """Handle successful payment"""
    customer_id = invoice['customer']
    subscription_id = invoice['subscription']
    
    logger.info(f"💰 Payment succeeded for customer {customer_id}")
    
    try:
        # Find user by customer ID
        user = get_user_by_customer_id(customer_id)
        
        if user and user['phone']:
            phone = user['phone']
            
            # Ensure user is active and in whitelist
            update_user_profile(phone, subscription_status='active')
            add_to_whitelist(phone, send_welcome=False, source='stripe_payment')
            
            # Log the event
            log_stripe_event('payment_succeeded', customer_id, subscription_id, phone, 'active')
            
            logger.info(f"✅ Payment processed successfully for {phone}")
        
    except Exception as e:
        logger.error(f"❌ Error handling payment success: {e}")

def handle_payment_failed(invoice):
    """Handle failed payment"""
    customer_id = invoice['customer']
    subscription_id = invoice['subscription']
    
    logger.info(f"❌ Payment failed for customer {customer_id}")
    
    try:
        # Find user by customer ID
        user = get_user_by_customer_id(customer_id)
        
        if user and user['phone']:
            phone = user['phone']
            
            # Send payment failure notification
            payment_msg = "❌ Your Hey Alex payment failed. Please update your payment method at heyalex.co to continue service."
            send_sms(phone, payment_msg, bypass_quota=True)
            
            # Log the event
            log_stripe_event('payment_failed', customer_id, subscription_id, phone, 'payment_failed')
            
            logger.info(f"📧 Payment failure notification sent to {phone}")
        
    except Exception as e:
        logger.error(f"❌ Error handling payment failure: {e}")

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

# === ENHANCED SMS WEBHOOK WITH IMPROVED SEARCH LOGIC ===
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
    
    # Content filtering
    is_valid, filter_reason = content_filter.is_valid_query(body)
    if not is_valid:
        logger.warning(f"🚫 Content filtered for {sender}: {filter_reason}")
        return jsonify({"message": "Content filtered"}), 400
    
    # Save user message first
    save_message(sender, "user", body)
    
    # Handle special commands before any other checks
    if body.lower() in ['stop', 'quit', 'unsubscribe']:
        response_msg = "You've been unsubscribed from Hey Alex. Text START to resume service."
        try:
            send_sms(sender, response_msg, bypass_quota=True)
            return jsonify({"message": "Unsubscribe processed"}), 200
        except Exception as e:
            logger.error(f"Failed to send unsubscribe message: {e}")
            return jsonify({"error": "Failed to process unsubscribe"}), 500
    
    if body.lower() in ['start', 'subscribe', 'resume']:
        # For START command, add to whitelist and begin onboarding
        add_to_whitelist(sender, send_welcome=False)  # Don't send duplicate welcome
        
        # Check if user is already onboarded
        if is_user_onboarded(sender):
            response_msg = WELCOME_MSG
        else:
            # Create profile if needed and start onboarding
            profile = get_user_profile(sender)
            if not profile:
                create_user_profile(sender)
            response_msg = ONBOARDING_NAME_MSG
        
        try:
            send_sms(sender, response_msg, bypass_quota=True)
            save_message(sender, "assistant", response_msg, "start_command", 0)
            return jsonify({"message": "Start message sent"}), 200
        except Exception as e:
            logger.error(f"Failed to send start message: {e}")
            return jsonify({"error": "Failed to send start message"}), 500
    
    # Get user profile and whitelist status
    profile = get_user_profile(sender)
    whitelist = load_whitelist()
    is_whitelisted = sender in whitelist
    
    logger.info(f"👤 User profile for {sender}: {profile}")
    logger.info(f"📋 Whitelist status for {sender}: {is_whitelisted}")
    
    # NEW LOGIC: Handle users based on their profile and whitelist status
    
    # Case 1: User has completed onboarding but not in whitelist (shouldn't happen, but fix it)
    if profile and profile['onboarding_completed'] and not is_whitelisted:
        logger.info(f"🔧 User {sender} completed onboarding but not in whitelist - adding them")
        add_to_whitelist(sender, send_welcome=False)
        is_whitelisted = True
    
    # Case 2: User not in whitelist and no profile (completely new user)
    if not is_whitelisted and not profile:
        logger.info(f"👋 New user {sender} - starting onboarding process")
        
        # Add to whitelist and create profile
        add_to_whitelist(sender, send_welcome=False)  # We'll send our own welcome
        create_user_profile(sender)
        
        try:
            send_sms(sender, ONBOARDING_NAME_MSG, bypass_quota=True)
            save_message(sender, "assistant", ONBOARDING_NAME_MSG, "onboarding_start", 0)
            return jsonify({"message": "Onboarding started for new user"}), 200
        except Exception as e:
            logger.error(f"Failed to send onboarding start message: {e}")
            return jsonify({"error": "Failed to start onboarding"}), 500
    
    # Case 3: User not in whitelist but has partial profile (interrupted onboarding)
    if not is_whitelisted and profile and not profile['onboarding_completed']:
        logger.info(f"🔄 User {sender} has partial profile - resuming onboarding")
        
        # Add to whitelist so they can continue
        add_to_whitelist(sender, send_welcome=False)
        is_whitelisted = True
    
    # Case 4: User not in whitelist and no valid scenario above
    if not is_whitelisted:
        logger.warning(f"🚫 Unauthorized sender: {sender} - asking them to text START")
        unauthorized_msg = "Hi! To use Hey Alex, please text START to begin your subscription."
        try:
            send_sms(sender, unauthorized_msg, bypass_quota=True)
            return jsonify({"message": "Unauthorized sender guidance sent"}), 200
        except Exception as e:
            logger.error(f"Failed to send unauthorized message: {e}")
            return jsonify({"error": "Failed to send unauthorized message"}), 500
    
    # At this point, user should be in whitelist. Double-check profile.
    if not profile:
        profile = get_user_profile(sender)
        if not profile:
            logger.error(f"🚨 User {sender} in whitelist but no profile - creating emergency profile")
            create_user_profile(sender)
            profile = get_user_profile(sender)
    
    # Check subscription status (if using Stripe integration)
    if profile and 'subscription_status' in profile:
        if profile['subscription_status'] not in ['active', 'trialing', 'inactive']:
            # 'inactive' is default for manual additions, allow it
            if profile['subscription_status'] in ['canceled', 'past_due', 'unpaid']:
                logger.warning(f"🚫 Inactive subscription for {sender}: {profile['subscription_status']}")
                inactive_msg = "Your Hey Alex subscription is inactive. Please subscribe at heyalex.co to continue using the service."
                try:
                    send_sms(sender, inactive_msg, bypass_quota=True)
                    return jsonify({"message": "Subscription inactive message sent"}), 200
                except Exception as e:
                    logger.error(f"Failed to send inactive subscription message: {e}")
                    return jsonify({"error": "Failed to send inactive message"}), 500
    
    # Handle onboarding process if not completed
    if not profile['onboarding_completed']:
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
            fallback_msg = "Sorry, there was an error during setup. Please try again or text START to restart."
            try:
                send_sms(sender, fallback_msg, bypass_quota=True)
                return jsonify({"message": "Onboarding fallback sent"}), 200
            except Exception as fallback_error:
                logger.error(f"Failed to send onboarding fallback: {fallback_error}")
                return jsonify({"error": "Onboarding failed"}), 500
    
    # User is fully onboarded - process normal queries
    logger.info(f"✅ User {sender} is fully onboarded: {profile['first_name']} in {profile['location']}")
    
    # Get user context for personalized responses
    user_context = get_user_context_for_queries(sender)
    
    try:
        # Process query using enhanced logic
        response_msg, intent_type = process_user_query(sender, body, user_context)
        
        # Ensure response is not too long for SMS
        if len(response_msg) > 1600:
            response_msg = response_msg[:1597] + "..."
        
        # Save assistant response
        response_time = int((time.time() - start_time) * 1000)
        save_message(sender, "assistant", response_msg, intent_type, response_time)
        
        # Send main response
        result = send_sms(sender, response_msg)
        
        if "error" not in result:
            # Log usage analytics with error handling
            try:
                log_usage_analytics(sender, intent_type, True, response_time)
            except Exception as analytics_error:
                logger.error(f"Analytics logging failed: {analytics_error}")
            
            logger.info(f"✅ Response sent to {sender} in {response_time}ms")
            return jsonify({"message": "Response sent successfully"}), 200
        else:
            try:
                log_usage_analytics(sender, intent_type, False, response_time)
            except Exception as analytics_error:
                logger.error(f"Analytics logging failed: {analytics_error}")
                
            logger.error(f"❌ Failed to send response to {sender}: {result['error']}")
            return jsonify({"error": "Failed to send response"}), 500
            
    except Exception as e:
        response_time = int((time.time() - start_time) * 1000)
        
        try:
            log_usage_analytics(sender, "general", False, response_time)
        except Exception as analytics_error:
            logger.error(f"Analytics logging failed: {analytics_error}")
            
        logger.error(f"💥 Processing error for {sender}: {e}")
        
        # Send fallback response
        fallback_msg = "Sorry, I'm having trouble processing your request. Please try again in a moment."
        try:
            send_sms(sender, fallback_msg, bypass_quota=True)
            return jsonify({"message": "Fallback response sent"}), 200
        except Exception as fallback_error:
            logger.error(f"Failed to send fallback message: {fallback_error}")
            return jsonify({"error": "Processing failed"}), 500

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
        
        success = add_to_whitelist(phone, send_welcome=send_welcome, source='admin')
        
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

@app.route('/admin/whitelist/remove', methods=['POST'])
def admin_remove_from_whitelist():
    """Admin endpoint to manually remove users from whitelist"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        send_goodbye = data.get('send_goodbye', False)
        
        if not phone:
            return jsonify({"error": "Phone number required"}), 400
        
        phone = normalize_phone_number(phone)
        
        success = remove_from_whitelist(phone, send_goodbye=send_goodbye, source='admin')
        
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

@app.route('/admin/whitelist', methods=['GET'])
def get_whitelist_admin():
    """Admin endpoint to view all whitelist entries"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT w.phone, w.added_date, w.added_by, w.is_active,
                               up.first_name, up.location, up.subscription_status
                        FROM whitelist w
                        LEFT JOIN user_profiles up ON w.phone = up.phone
                        ORDER BY w.added_date DESC
                    """)
                    
                    results = []
                    for row in cursor.fetchall():
                        results.append({
                            'phone': row['phone'],
                            'added_date': row['added_date'],
                            'added_by': row['added_by'],
                            'is_active': bool(row['is_active']),
                            'first_name': row['first_name'],
                            'location': row['location'],
                            'subscription_status': row['subscription_status']
                        })
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    SELECT w.phone, w.added_date, w.added_by, w.is_active,
                           up.first_name, up.location, up.subscription_status
                    FROM whitelist w
                    LEFT JOIN user_profiles up ON w.phone = up.phone
                    ORDER BY w.added_date DESC
                """)
                
                results = []
                for row in c.fetchall():
                    results.append({
                        'phone': row[0],
                        'added_date': row[1],
                        'added_by': row[2],
                        'is_active': bool(row[3]),
                        'first_name': row[4],
                        'location': row[5],
                        'subscription_status': row[6]
                    })
        
        stats = get_whitelist_stats()
        
        return jsonify({
            'stats': stats,
            'entries': results,
            'total_entries': len(results)
        })
        
    except Exception as e:
        logger.error(f"Error getting whitelist: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/users', methods=['GET'])
def get_all_users():
    """Admin endpoint to view all users with their profiles and subscription status"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT 
                            up.phone, 
                            up.first_name, 
                            up.location, 
                            up.onboarding_step,
                            up.onboarding_completed,
                            up.stripe_customer_id,
                            up.subscription_status,
                            up.subscription_id,
                            up.trial_end_date,
                            up.created_date,
                            w.is_active as in_whitelist
                        FROM user_profiles up
                        LEFT JOIN whitelist w ON up.phone = w.phone
                        ORDER BY up.created_date DESC
                    """)
                    
                    users = []
                    for row in cursor.fetchall():
                        users.append({
                            'phone': row['phone'],
                            'first_name': row['first_name'],
                            'location': row['location'],
                            'onboarding_step': row['onboarding_step'],
                            'onboarding_completed': bool(row['onboarding_completed']),
                            'stripe_customer_id': row['stripe_customer_id'],
                            'subscription_status': row['subscription_status'],
                            'subscription_id': row['subscription_id'],
                            'trial_end_date': row['trial_end_date'],
                            'created_date': row['created_date'],
                            'in_whitelist': bool(row['in_whitelist']) if row['in_whitelist'] is not None else False
                        })
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                c.execute("""
                    SELECT 
                        up.phone, 
                        up.first_name, 
                        up.location, 
                        up.onboarding_step,
                        up.onboarding_completed,
                        up.stripe_customer_id,
                        up.subscription_status,
                        up.subscription_id,
                        up.trial_end_date,
                        up.created_date,
                        w.is_active as in_whitelist
                    FROM user_profiles up
                    LEFT JOIN whitelist w ON up.phone = w.phone
                    ORDER BY up.created_date DESC
                """)
                
                users = []
                for row in c.fetchall():
                    users.append({
                        'phone': row[0],
                        'first_name': row[1],
                        'location': row[2],
                        'onboarding_step': row[3],
                        'onboarding_completed': bool(row[4]),
                        'stripe_customer_id': row[5],
                        'subscription_status': row[6],
                        'subscription_id': row[7],
                        'trial_end_date': row[8],
                        'created_date': row[9],
                        'in_whitelist': bool(row[10]) if row[10] is not None else False
                    })
        
        return jsonify({
            'total_users': len(users),
            'users': users
        })
        
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "version": APP_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": "postgresql" if DATABASE_URL else "sqlite",
        "services": {
            "database": "connected",
            "clicksend": "configured" if CLICKSEND_API_KEY else "not_configured",
            "anthropic": "configured" if ANTHROPIC_API_KEY else "not_configured",
            "serpapi": "configured" if SERPAPI_API_KEY else "not_configured",
            "stripe": "configured" if STRIPE_SECRET_KEY else "not_configured"
        }
    })

@app.route('/admin/stats', methods=['GET'])
def get_stats():
    """Admin endpoint to get system statistics"""
    try:
        conn, db_type = get_db_connection()
        
        if db_type == "postgresql":
            with conn:
                with conn.cursor() as cursor:
                    # Total users
                    cursor.execute("SELECT COUNT(*) FROM user_profiles")
                    total_users = cursor.fetchone()['count']
                    
                    # Active subscriptions
                    cursor.execute("SELECT COUNT(*) FROM user_profiles WHERE subscription_status IN ('active', 'trialing')")
                    active_subscriptions = cursor.fetchone()['count']
                    
                    # Completed onboarding
                    cursor.execute("SELECT COUNT(*) FROM user_profiles WHERE onboarding_completed = TRUE")
                    onboarded_users = cursor.fetchone()['count']
                    
                    # Messages in last 30 days
                    cursor.execute("SELECT COUNT(*) FROM messages WHERE ts > CURRENT_TIMESTAMP - INTERVAL '30 days'")
                    recent_messages = cursor.fetchone()['count']
                    
                    # Successful responses in last 30 days
                    cursor.execute("SELECT COUNT(*) FROM usage_analytics WHERE success = TRUE AND timestamp > CURRENT_TIMESTAMP - INTERVAL '30 days'")
                    successful_responses = cursor.fetchone()['count']
                    
                    # Stripe events in last 7 days
                    cursor.execute("SELECT COUNT(*) FROM stripe_events WHERE timestamp > CURRENT_TIMESTAMP - INTERVAL '7 days'")
                    recent_stripe_events = cursor.fetchone()['count']
        else:
            with closing(conn) as connection:
                c = connection.cursor()
                
                # Total users
                c.execute("SELECT COUNT(*) FROM user_profiles")
                total_users = c.fetchone()[0]
                
                # Active subscriptions
                c.execute("SELECT COUNT(*) FROM user_profiles WHERE subscription_status IN ('active', 'trialing')")
                active_subscriptions = c.fetchone()[0]
                
                # Completed onboarding
                c.execute("SELECT COUNT(*) FROM user_profiles WHERE onboarding_completed = TRUE")
                onboarded_users = c.fetchone()[0]
                
                # Messages in last 30 days
                c.execute("SELECT COUNT(*) FROM messages WHERE ts > datetime('now', '-30 days')")
                recent_messages = c.fetchone()[0]
                
                # Successful responses in last 30 days
                c.execute("SELECT COUNT(*) FROM usage_analytics WHERE success = TRUE AND timestamp > datetime('now', '-30 days')")
                successful_responses = c.fetchone()[0]
                
                # Stripe events in last 7 days
                c.execute("SELECT COUNT(*) FROM stripe_events WHERE timestamp > datetime('now', '-7 days')")
                recent_stripe_events = c.fetchone()[0]
        
        # Whitelist count
        stats = get_whitelist_stats()
        
        return jsonify({
            "total_users": total_users,
            "active_subscriptions": active_subscriptions,
            "onboarded_users": onboarded_users,
            "whitelist_stats": stats,
            "recent_messages_30d": recent_messages,
            "successful_responses_30d": successful_responses,
            "recent_stripe_events_7d": recent_stripe_events,
            "success_rate": round((successful_responses / recent_messages * 100) if recent_messages > 0 else 0, 2),
            "database_type": "postgresql" if DATABASE_URL else "sqlite"
        })
        
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({"error": str(e)}), 500

# === Landing Page Route ===
@app.route('/', methods=['GET'])
def landing_page():
    """Serve the landing page"""
    try:
        # Try to read the landing page HTML file
        possible_files = ['landing.html', 'Hey Alex - SMS Assistant Landing Page.html', 'index.html']
        
        for filename in possible_files:
            try:
                with open(filename, 'r') as f:
                    html_content = f.read()
                return html_content, 200, {'Content-Type': 'text/html'}
            except FileNotFoundError:
                continue
        
        # Fallback if no landing page file exists
        return jsonify({
            "service": "Hey Alex SMS Assistant",
            "version": APP_VERSION,
            "status": "running",
            "description": "Personal SMS research assistant",
            "subscribe": "Visit heyalex.co to subscribe",
            "database": "postgresql" if DATABASE_URL else "sqlite (local development)",
            "changelog": CHANGELOG[APP_VERSION]
        })
        
    except Exception as e:
        logger.error(f"Error serving landing page: {e}")
        return jsonify({
            "service": "Hey Alex SMS Assistant",
            "version": APP_VERSION,
            "status": "running",
            "error": "Landing page unavailable"
        })

# === Initialize and Run ===
# Initialize database on startup
init_db()

if __name__ == "__main__":
    logger.info(f"🚀 Starting Hey Alex SMS Assistant v{APP_VERSION}")
    logger.info(f"📋 Latest changes: {CHANGELOG[APP_VERSION]}")
    logger.info(f"🗄️ Database: {'PostgreSQL (Production)' if DATABASE_URL else 'SQLite (Local Development)'}")
    logger.info(f"🔗 Stripe webhook endpoint: /stripe/webhook")
    logger.info(f"📱 SMS webhook endpoint: /sms")
    logger.info(f"🏥 Health check endpoint: /health")
    logger.info(f"📊 Admin stats endpoint: /admin/stats")
    logger.info(f"👥 Admin whitelist endpoint: /admin/whitelist")
    
    # Check critical configurations
    missing_configs = []
    if not STRIPE_SECRET_KEY:
        missing_configs.append("STRIPE_SECRET_KEY")
    if not STRIPE_WEBHOOK_SECRET:
        missing_configs.append("STRIPE_WEBHOOK_SECRET")
    if not CLICKSEND_API_KEY:
        missing_configs.append("CLICKSEND_API_KEY")
    
    if missing_configs:
        logger.warning(f"⚠️ Missing configurations: {', '.join(missing_configs)}")
        logger.warning("Some features may not work properly")
    else:
        logger.info("✅ All critical configurations present")
    
    # Database connectivity check
    try:
        conn, db_type = get_db_connection()
        logger.info(f"✅ Database connectivity verified: {db_type}")
        
        # Check whitelist migration status
        stats = get_whitelist_stats()
        logger.info(f"📊 Whitelist status: {stats['active']} active, {stats['inactive']} inactive, {stats['total']} total")
        
        if db_type == "postgresql":
            conn.close()
    except Exception as e:
        logger.error(f"❌ Database connectivity failed: {e}")
    
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
