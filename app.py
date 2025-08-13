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
    "2.7": "Fixed user onboarding persistence logic, enhanced whitelist/profile coordination",
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
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO stripe_events 
                (event_type, customer_id, subscription_id, phone, status, details, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (event_type, customer_id, subscription_id, phone, status, json.dumps(details) if details else None))
            conn.commit()
            logger.info(f"📋 Logged Stripe event: {event_type} for {phone}")
    except Exception as e:
        logger.error(f"Error logging Stripe event: {e}")

# === Database Initialization ===
def init_db():
    try:
        logger.info(f"🗄️ Initializing database at: {DB_PATH}")
        
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            # Check if database exists and has data
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            existing_tables = [row[0] for row in c.fetchall()]
            logger.info(f"📊 Existing tables: {existing_tables}")
            
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
            
            c.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_phone_ts 
            ON messages(phone, ts DESC);
            """)
            
            # User profiles table for onboarding and Stripe integration
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
            
            c.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_profiles_phone 
            ON user_profiles(phone);
            """)
            
            c.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_profiles_customer 
            ON user_profiles(stripe_customer_id);
            """)
            
            # Usage analytics table - FIXED: This was missing!
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
            CREATE INDEX IF NOT EXISTS idx_usage_analytics_phone_ts 
            ON usage_analytics(phone, timestamp DESC);
            """)
            
            # Stripe events table for audit trail
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
            
            c.execute("""
            CREATE INDEX IF NOT EXISTS idx_stripe_events_customer 
            ON stripe_events(customer_id);
            """)
            
            # Other existing tables...
            c.execute("""
            CREATE TABLE IF NOT EXISTS onboarding_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                step INTEGER NOT NULL,
                response TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """)
            
            c.execute("""
            CREATE TABLE IF NOT EXISTS whitelist_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('added','removed')),
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                source TEXT DEFAULT 'manual'
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
            
            # Check and add missing columns to existing tables
            logger.info("🔄 Checking for missing columns...")
            
            # Check user_profiles for Stripe columns
            c.execute("PRAGMA table_info(user_profiles)")
            columns = [row[1] for row in c.fetchall()]
            
            stripe_columns = [
                ('stripe_customer_id', 'TEXT'),
                ('subscription_status', 'TEXT DEFAULT "inactive"'),
                ('subscription_id', 'TEXT'),
                ('trial_end_date', 'DATETIME')
            ]
            
            for col_name, col_type in stripe_columns:
                if col_name not in columns:
                    try:
                        c.execute(f"ALTER TABLE user_profiles ADD COLUMN {col_name} {col_type}")
                        logger.info(f"✅ Added missing column: {col_name}")
                    except sqlite3.OperationalError as e:
                        if "duplicate column name" not in str(e):
                            logger.warning(f"⚠️ Could not add column {col_name}: {e}")
            
            # Check whitelist_events for source column
            c.execute("PRAGMA table_info(whitelist_events)")
            columns = [row[1] for row in c.fetchall()]
            
            if 'source' not in columns:
                try:
                    c.execute("ALTER TABLE whitelist_events ADD COLUMN source TEXT DEFAULT 'manual'")
                    logger.info("✅ Added missing column: source to whitelist_events")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e):
                        logger.warning(f"⚠️ Could not add source column: {e}")
            
            conn.commit()
            
            # Final verification - check that usage_analytics exists and works
            try:
                c.execute("SELECT COUNT(*) FROM usage_analytics")
                logger.info("✅ usage_analytics table verified working")
            except sqlite3.OperationalError:
                logger.error("❌ usage_analytics table still missing - creating manually")
                c.execute("""
                CREATE TABLE usage_analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    intent_type TEXT,
                    success BOOLEAN,
                    response_time_ms INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """)
                conn.commit()
                logger.info("✅ usage_analytics table created manually")
            
            # Check for existing data
            c.execute("SELECT COUNT(*) FROM user_profiles")
            user_count = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM messages")  
            message_count = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM usage_analytics")
            analytics_count = c.fetchone()[0]
            
            logger.info(f"📊 Database initialized successfully")
            logger.info(f"📊 Found {user_count} user profiles, {message_count} messages, {analytics_count} analytics records")
            
            # Show recent users for debugging
            if user_count > 0:
                c.execute("""
                    SELECT phone, first_name, location, subscription_status, created_date 
                    FROM user_profiles 
                    ORDER BY created_date DESC 
                    LIMIT 5
                """)
                recent_users = c.fetchall()
                logger.info(f"📊 Recent users: {recent_users}")
            
    except Exception as e:
        logger.error(f"💥 Database initialization error: {e}")
        raise

# === User Profile Management ===
def get_user_profile(phone):
    """Get user profile and onboarding status"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
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
            else:
                return None
    except Exception as e:
        logger.error(f"Error getting user profile for {phone}: {e}")
        return None

def create_user_profile(phone, stripe_customer_id=None, subscription_status='inactive'):
    """Create new user profile for onboarding"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR IGNORE INTO user_profiles 
                (phone, onboarding_step, onboarding_completed, stripe_customer_id, subscription_status)
                VALUES (?, 1, FALSE, ?, ?)
            """, (phone, stripe_customer_id, subscription_status))
            conn.commit()
            logger.info(f"📝 Created user profile for {phone} with status {subscription_status}")
            return True
    except Exception as e:
        logger.error(f"Error creating user profile for {phone}: {e}")
        return False

def update_user_profile(phone, **kwargs):
    """Update user profile information with flexible field updates"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            # Build dynamic update query
            update_parts = []
            params = []
            
            allowed_fields = [
                'first_name', 'location', 'onboarding_step', 'onboarding_completed',
                'stripe_customer_id', 'subscription_status', 'subscription_id', 'trial_end_date'
            ]
            
            for field, value in kwargs.items():
                if field in allowed_fields:
                    update_parts.append(f"{field} = ?")
                    params.append(value)
            
            if not update_parts:
                logger.warning(f"No valid fields to update for {phone}")
                return False
            
            update_parts.append("updated_date = CURRENT_TIMESTAMP")
            params.append(phone)
            
            query = f"""
                UPDATE user_profiles 
                SET {', '.join(update_parts)}
                WHERE phone = ?
            """
            
            c.execute(query, params)
            conn.commit()
            logger.info(f"📝 Updated user profile for {phone}: {kwargs}")
            return True
    except Exception as e:
        logger.error(f"Error updating user profile for {phone}: {e}")
        return False

def get_user_by_customer_id(customer_id):
    """Get user profile by Stripe customer ID"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
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
            else:
                return None
    except Exception as e:
        logger.error(f"Error getting user by customer ID {customer_id}: {e}")
        return None

# === Onboarding System ===
def log_onboarding_step(phone, step, response):
    """Log onboarding step response"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO onboarding_log (phone, step, response)
                VALUES (?, ?, ?)
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
def log_whitelist_event(phone, action, source='manual'):
    """Log whitelist addition/removal events"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO whitelist_events (phone, action, source, timestamp)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
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
            
            # Create user profile if it doesn't exist
            profile = get_user_profile(phone)
            if not profile:
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

def remove_from_whitelist(phone, send_goodbye=False, source='manual'):
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
            log_whitelist_event(phone, "removed", source)
            
            logger.info(f"📱 Removed {phone} from whitelist (source: {source})")
            
            # Send goodbye message if requested
            if send_goodbye:
                try:
                    send_sms(phone, SUBSCRIPTION_CANCELLED_MSG, bypass_quota=True)
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

def load_whitelist():
    try:
        with open(WHITELIST_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

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
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO sms_delivery_log (phone, message_content, clicksend_response, delivery_status, message_id)
            VALUES (?, ?, ?, ?, ?)
        """, (phone, message_content, json.dumps(clicksend_response), delivery_status, message_id))
        conn.commit()

def save_message(phone, role, content, intent_type=None, response_time_ms=None):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO messages (phone, role, content, intent_type, response_time_ms) 
            VALUES (?, ?, ?, ?, ?)
        """, (phone, role, content, intent_type, response_time_ms))
        conn.commit()

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
    """Log usage analytics with error handling for missing table"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO usage_analytics (phone, intent_type, success, response_time_ms)
                VALUES (?, ?, ?, ?)
            """, (phone, intent_type, success, response_time_ms))
            conn.commit()
    except sqlite3.OperationalError as e:
        if "no such table: usage_analytics" in str(e):
            logger.error("❌ usage_analytics table missing - attempting to create it")
            try:
                with closing(sqlite3.connect(DB_PATH)) as conn:
                    c = conn.cursor()
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
                    # Try the insert again
                    c.execute("""
                        INSERT INTO usage_analytics (phone, intent_type, success, response_time_ms)
                        VALUES (?, ?, ?, ?)
                    """, (phone, intent_type, success, response_time_ms))
                    conn.commit()
                    logger.info("✅ Created usage_analytics table and logged data")
            except Exception as create_error:
                logger.error(f"💥 Failed to create usage_analytics table: {create_error}")
        else:
            logger.error(f"💥 Usage analytics logging error: {e}")
    except Exception as e:
        logger.error(f"💥 Unexpected error logging usage analytics: {e}")

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
        
        return True, usage_info, None

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

    # Process results
    org = data.get("organic_results", [])
    if org:
        top = org[0]
        title = top.get("title", "")
        snippet = top.get("snippet", "")
        
        result = f"{title}"
        if snippet:
            result += f" — {snippet}"
        return result[:500]
    
    return f"No results found for '{q}'."

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
        
        try:
            log_usage_analytics(phone, "claude_chat", True, response_time)
        except Exception as analytics_error:
            logger.error(f"Analytics logging failed: {analytics_error}")
        
        return reply
        
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

# === FIXED SMS WEBHOOK WITH IMPROVED LOGIC ===
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
    
    intent = detect_intent(body, sender)
    intent_type = intent.type if intent else "general"
    
    # Get user context for personalized responses
    user_context = get_user_context_for_queries(sender)
    
    try:
        # Process based on intent
        if intent and intent.type == "weather":
            # Use user's location if no city specified and user is onboarded
            if user_context['personalized']:
                city = user_context['location']
                logger.info(f"🌍 Using user's saved location: {city}")
                query = f"weather forecast {city}"
                response_msg = web_search(query, search_type="general")
                first_name = user_context['first_name']
                response_msg = f"Hi {first_name}! " + response_msg
            else:
                response_msg = web_search("weather forecast", search_type="general")
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
            log_usage_analytics(sender, intent_type, False, response_time)
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

@app.route('/admin/users', methods=['GET'])
def get_all_users():
    """Admin endpoint to view all users with their profiles and subscription status"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
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
                    up.created_date
                FROM user_profiles up
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
                    'created_date': row[9]
                })
            
            return jsonify({
                'total_users': len(users),
                'users': users
            })
            
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/stripe/events', methods=['GET'])
def get_stripe_events():
    """Admin endpoint to view recent Stripe webhook events"""
    try:
        limit = request.args.get('limit', 50, type=int)
        
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT event_type, customer_id, subscription_id, phone, status, details, timestamp
                FROM stripe_events
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            
            events = []
            for row in c.fetchall():
                event_details = None
                if row[5]:  # details column
                    try:
                        event_details = json.loads(row[5])
                    except:
                        event_details = row[5]
                
                events.append({
                    'event_type': row[0],
                    'customer_id': row[1],
                    'subscription_id': row[2],
                    'phone': row[3],
                    'status': row[4],
                    'details': event_details,
                    'timestamp': row[6]
                })
            
            return jsonify({
                'total_events': len(events),
                'events': events
            })
            
    except Exception as e:
        logger.error(f"Error getting Stripe events: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/subscription/status/<phone>', methods=['GET'])
def get_subscription_status(phone):
    """Admin endpoint to check a specific user's subscription status"""
    try:
        phone = normalize_phone_number(phone)
        profile = get_user_profile(phone)
        
        if not profile:
            return jsonify({"error": "User not found"}), 404
        
        # Get usage statistics
        whitelist = load_whitelist()
        is_whitelisted = phone in whitelist
        
        # Get recent messages
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT COUNT(*) FROM messages 
                WHERE phone = ? AND ts > datetime('now', '-30 days')
            """, (phone,))
            recent_message_count = c.fetchone()[0]
        
        return jsonify({
            'phone': phone,
            'profile': profile,
            'is_whitelisted': is_whitelisted,
            'recent_message_count': recent_message_count,
            'subscription_active': profile['subscription_status'] in ['active', 'trialing']
        })
        
    except Exception as e:
        logger.error(f"Error getting subscription status for {phone}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/sync/stripe', methods=['POST'])
def sync_stripe_customer():
    """Admin endpoint to manually sync a Stripe customer with phone number"""
    try:
        data = request.get_json()
        customer_id = data.get('customer_id')
        phone = data.get('phone')
        
        if not customer_id or not phone:
            return jsonify({"error": "Both customer_id and phone required"}), 400
        
        phone = normalize_phone_number(phone)
        
        # Get or create user profile
        profile = get_user_profile(phone)
        if not profile:
            create_user_profile(phone, stripe_customer_id=customer_id)
        else:
            update_user_profile(phone, stripe_customer_id=customer_id)
        
        # Get current subscription from Stripe
        try:
            customer = stripe.Customer.retrieve(customer_id)
            subscriptions = stripe.Subscription.list(customer=customer_id, status='all')
            
            if subscriptions.data:
                latest_sub = subscriptions.data[0]  # Most recent subscription
                update_user_profile(
                    phone,
                    subscription_status=latest_sub.status,
                    subscription_id=latest_sub.id
                )
                
                # Add to whitelist if subscription is active
                if latest_sub.status in ['active', 'trialing']:
                    add_to_whitelist(phone, send_welcome=False, source='admin_sync')
                
                return jsonify({
                    "success": True,
                    "message": f"Synced customer {customer_id} with phone {phone}",
                    "subscription_status": latest_sub.status
                })
            else:
                return jsonify({
                    "success": True,
                    "message": f"Synced customer {customer_id} with phone {phone}",
                    "subscription_status": "no_subscription"
                })
        
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error during sync: {e}")
            return jsonify({"error": f"Stripe error: {str(e)}"}), 500
            
    except Exception as e:
        logger.error(f"Error syncing Stripe customer: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "version": APP_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
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
            whitelist = load_whitelist()
            whitelist_count = len(whitelist)
            
            return jsonify({
                "total_users": total_users,
                "active_subscriptions": active_subscriptions,
                "onboarded_users": onboarded_users,
                "whitelist_count": whitelist_count,
                "recent_messages_30d": recent_messages,
                "successful_responses_30d": successful_responses,
                "recent_stripe_events_7d": recent_stripe_events,
                "success_rate": round((successful_responses / recent_messages * 100) if recent_messages > 0 else 0, 2)
            })
            
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({"error": str(e)}), 500

# === Landing Page Route ===
@app.route('/', methods=['GET'])
def landing_page():
    """Serve the landing page"""
    try:
        # Read the landing page HTML file
        with open('landing.html', 'r') as f:
            html_content = f.read()
        return html_content, 200, {'Content-Type': 'text/html'}
    except FileNotFoundError:
        # Fallback if landing page file doesn't exist
        return jsonify({
            "service": "Hey Alex SMS Assistant",
            "version": APP_VERSION,
            "status": "running",
            "description": "Personal SMS research assistant",
            "subscribe": "Visit heyalex.co to subscribe"
        })

# === Initialize and Run ===
# Initialize database on startup
init_db()

if __name__ == "__main__":
    logger.info(f"🚀 Starting Hey Alex SMS Assistant v{APP_VERSION}")
    logger.info(f"📋 Latest changes: {CHANGELOG[APP_VERSION]}")
    logger.info(f"🔗 Stripe webhook endpoint: /stripe/webhook")
    logger.info(f"📱 SMS webhook endpoint: /sms")
    logger.info(f"🏥 Health check endpoint: /health")
    logger.info(f"📊 Admin stats endpoint: /admin/stats")
    
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
    
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
