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
APP_VERSION = "2.6"
CHANGELOG = {
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

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    logger.info("Stripe API initialized successfully")
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
DB_PATH = os.getenv("DB_PATH", "chat.db")

# WELCOME MESSAGE
WELCOME_MSG = (
    "Hey there! 🌟 I'm Alex - think of me as your personal research assistant who lives in your texts. "
    "I'm great at finding: ✓ Weather & forecasts ✓ Restaurant info & hours ✓ Local business details "
    "✓ Current news & headlines No apps, no browsing - just text me your question and I'll handle the rest! "
    "Try asking \"weather today\" to get started."
)

# NEW USER WELCOME MESSAGE - Start of onboarding
NEW_USER_WELCOME_MSG = (
    "🎉 Welcome to Hey Alex! I'm your personal SMS research assistant. "
    "Before we start, I need to get to know you better. What's your first name?"
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

# === Enhanced Whitelist Management with Auto-Welcome ===
def add_to_whitelist(phone, send_welcome=True):
    """Enhanced whitelist addition with automatic welcome message"""
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
            
def log_whitelist_event(phone, action):
    """Log whitelist addition/removal events"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO whitelist_events (phone, action, timestamp)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (phone, action))
            conn.commit()
            logger.info(f"📋 Logged whitelist event: {action} for {phone}")
    except Exception as e:
        logger.error(f"Error logging whitelist event: {e}")

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

def load_whitelist():
    try:
        with open(WHITELIST_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

# === Onboarding System ===
def get_user_profile(phone):
    """Get user profile and onboarding status"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT first_name, location, onboarding_step, onboarding_completed
                FROM user_profiles
                WHERE phone = ?
            """, (phone,))
            result = c.fetchone()
            
            if result:
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
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR IGNORE INTO user_profiles 
                (phone, onboarding_step, onboarding_completed)
                VALUES (?, 1, FALSE)
            """, (phone,))
            conn.commit()
            logger.info(f"📝 Created user profile for {phone}")
            return True
    except Exception as e:
        logger.error(f"Error creating user profile for {phone}: {e}")
        return False

def update_user_profile(phone, first_name=None, location=None, onboarding_step=None, onboarding_completed=None):
    """Update user profile information"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            # Build dynamic update query
            update_parts = []
            params = []
            
            if first_name is not None:
                update_parts.append("first_name = ?")
                params.append(first_name)
            
            if location is not None:
                update_parts.append("location = ?")
                params.append(location)
            
            if onboarding_step is not None:
                update_parts.append("onboarding_step = ?")
                params.append(onboarding_step)
            
            if onboarding_completed is not None:
                update_parts.append("onboarding_completed = ?")
                params.append(onboarding_completed)
            
            update_parts.append("updated_date = CURRENT_TIMESTAMP")
            params.append(phone)
            
            query = f"""
                UPDATE user_profiles 
                SET {', '.join(update_parts)}
                WHERE phone = ?
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
    """Log whitelist addition/removal events"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO whitelist_events (phone, action, timestamp)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (phone, action))
            conn.commit()
            logger.info(f"📋 Logged whitelist event: {action} for {phone}")
    except Exception as e:
        logger.error(f"Error logging whitelist event: {e}")

# === Admin API Endpoints (Updated) ===
@app.route('/admin/users', methods=['GET'])
def get_all_users():
    """Admin endpoint to view all users with their profiles and onboarding status"""
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
                    up.created_date,
                    up.updated_date,
                    COALESCE(mu.message_count, 0) as usage_count,
                    COALESCE(mu.quota_exceeded, 0) as quota_exceeded,
                    MAX(m.ts) as last_message_date
                FROM user_profiles up
                LEFT JOIN monthly_sms_usage mu ON up.phone = mu.phone 
                    AND mu.period_start = (
                        SELECT MAX(period_start) FROM monthly_sms_usage mu2 
                        WHERE mu2.phone = up.phone
                    )
                LEFT JOIN messages m ON up.phone = m.phone
                GROUP BY up.phone
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
                    'created_date': row[5],
                    'updated_date': row[6],
                    'usage_count': row[7],
                    'quota_exceeded': bool(row[8]),
                    'last_message_date': row[9]
                })
            
            return jsonify({
                'total_users': len(users),
                'users': users
            })
            
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/user/<phone>/profile', methods=['GET'])
def get_user_profile_admin(phone):
    """Admin endpoint to get specific user profile"""
    try:
        phone = normalize_phone_number(phone)
        profile = get_user_profile(phone)
        
        if not profile:
            return jsonify({"error": "User not found"}), 404
            
        # Get additional info
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            # Get onboarding log
            c.execute("""
                SELECT step, response, timestamp
                FROM onboarding_log
                WHERE phone = ?
                ORDER BY timestamp ASC
            """, (phone,))
            
            onboarding_history = []
            for row in c.fetchall():
                onboarding_history.append({
                    'step': row[0],
                    'response': row[1],
                    'timestamp': row[2]
                })
            
            # Get message history
            c.execute("""
                SELECT role, content, intent_type, ts
                FROM messages
                WHERE phone = ?
                ORDER BY ts DESC
                LIMIT 10
            """, (phone,))
            
            recent_messages = []
            for row in c.fetchall():
                recent_messages.append({
                    'role': row[0],
                    'content': row[1],
                    'intent_type': row[2],
                    'timestamp': row[3]
                })
        
        return jsonify({
            'profile': profile,
            'onboarding_history': onboarding_history,
            'recent_messages': recent_messages
        })
        
    except Exception as e:
        logger.error(f"Error getting user profile for {phone}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/user/<phone>/reset-onboarding', methods=['POST'])
def reset_user_onboarding(phone):
    """Admin endpoint to reset user onboarding"""
    try:
        phone = normalize_phone_number(phone)
        
        # Reset onboarding status
        success = update_user_profile(
            phone, 
            first_name=None, 
            location=None, 
            onboarding_step=1, 
            onboarding_completed=False
        )
        
        if success:
            # Send onboarding start message
            send_sms(phone, ONBOARDING_NAME_MSG, bypass_quota=True)
            save_message(phone, "assistant", ONBOARDING_NAME_MSG, "onboarding_reset", 0)
            
            return jsonify({
                "success": True,
                "message": f"Reset onboarding for {phone} and sent start message"
            })
        else:
            return jsonify({"error": "Failed to reset onboarding"}), 500
            
    except Exception as e:
        logger.error(f"Error resetting onboarding for {phone}: {e}")
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

@app.route('/admin/whitelist', methods=['GET'])
def get_whitelist():
    """Admin endpoint to view current whitelist"""
    try:
        whitelist = load_whitelist()
        
        # Get additional info for each number
        whitelist_info = []
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            for phone in whitelist:
                # Get usage stats
                c.execute("""
                    SELECT message_count, quota_exceeded 
                    FROM monthly_sms_usage 
                    WHERE phone = ? 
                    ORDER BY period_start DESC 
                    LIMIT 1
                """, (phone,))
                usage_result = c.fetchone()
                
                usage_count = usage_result[0] if usage_result else 0
                quota_exceeded = bool(usage_result[1]) if usage_result else False
                
                # Get last message date
                c.execute("""
                    SELECT MAX(ts) FROM messages WHERE phone = ?
                """, (phone,))
                last_message_result = c.fetchone()
                last_message = last_message_result[0] if last_message_result and last_message_result[0] else None
                
                whitelist_info.append({
                    'phone': phone,
                    'usage_count': usage_count,
                    'quota_exceeded': quota_exceeded,
                    'last_message': last_message
                })
        
        return jsonify({
            'total_users': len(whitelist),
            'users': whitelist_info
        })
        
    except Exception as e:
        logger.error(f"Error getting whitelist: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/whitelist/events', methods=['GET'])
def get_whitelist_events():
    """Admin endpoint to view whitelist addition/removal events"""
    try:
        limit = request.args.get('limit', 50, type=int)
        
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT phone, action, timestamp
                FROM whitelist_events
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            
            events = []
            for row in c.fetchall():
                events.append({
                    'phone': row[0],
                    'action': row[1],
                    'timestamp': row[2]
                })
            
            return jsonify({
                'events': events,
                'total': len(events)
            })
            
    except Exception as e:
        logger.error(f"Error getting whitelist events: {e}")
        return jsonify({"error": str(e)}), 500

# === Stripe Webhook Handlers (Updated) ===
def handle_subscription_created(session):
    """Add customer to whitelist when subscription is created"""
    try:
        logger.info(f"📝 Processing subscription created: {session.get('id')}")
        
        # Get customer details from Stripe
        customer_id = session.get('customer')
        if not customer_id:
            logger.error("No customer ID in checkout session")
            return
            
        customer = stripe.Customer.retrieve(customer_id)
        
        email = customer.email
        phone = customer.phone or extract_phone_from_session(session)
        
        if not phone:
            logger.error(f"No phone number found for customer {customer_id} ({email})")
            return
        
        # Normalize phone number
        phone = normalize_phone_number(phone)
        
        # Add to whitelist with automatic welcome message
        if add_to_whitelist(phone, send_welcome=True):
            # Store customer relationship
            store_customer_data(phone, email, customer_id, 'active')
            
            # Log the subscription
            log_subscription_event(phone, email, customer_id, "subscription_created")
            
            logger.info(f"✅ Added {phone} ({email}) to whitelist - subscription created with welcome message")
        else:
            logger.warning(f"Failed to add {phone} to whitelist")
        
    except Exception as e:
        logger.error(f"Error handling subscription created: {e}")

def handle_subscription_cancelled(subscription):
    """Remove customer from whitelist when subscription is cancelled"""
    try:
        logger.info(f"❌ Processing subscription cancelled: {subscription.get('id')}")
        
        customer_id = subscription.get('customer')
        if not customer_id:
            logger.error("No customer ID in subscription")
            return
            
        customer = stripe.Customer.retrieve(customer_id)
        
        email = customer.email
        phone = get_phone_from_customer_id(customer_id) or customer.phone
        
        if not phone:
            logger.error(f"No phone number found for cancelled customer {customer_id} ({email})")
            return
        
        phone = normalize_phone_number(phone)
        
        # Remove from whitelist with goodbye message
        if remove_from_whitelist(phone, send_goodbye=True):
            # Update customer status
            store_customer_data(phone, email, customer_id, 'cancelled')
            
            # Log the cancellation
            log_subscription_event(phone, email, customer_id, "subscription_cancelled")
            
            logger.info(f"❌ Removed {phone} ({email}) from whitelist - subscription cancelled with goodbye message")
        else:
            logger.warning(f"Failed to remove {phone} from whitelist")
        
    except Exception as e:
        logger.error(f"Error handling subscription cancelled: {e}")

# === Database Initialization (Updated) ===
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        
        # Existing tables...
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
        
        c.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE NOT NULL,
            email TEXT,
            stripe_customer_id TEXT UNIQUE,
            status TEXT DEFAULT 'active',
            added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
            cancelled_date DATETIME
        );
        """)
        
        c.execute("""
        CREATE TABLE IF NOT EXISTS subscription_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            email TEXT,
            stripe_customer_id TEXT,
            event_type TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        # New tables for onboarding and user profiles
        c.execute("""
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
        """)
        
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_profiles_phone 
        ON user_profiles(phone);
        """)
        
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_profiles_onboarding 
        ON user_profiles(phone, onboarding_step);
        """)
        
        # New table for tracking onboarding progress
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
        CREATE INDEX IF NOT EXISTS idx_onboarding_log_phone 
        ON onboarding_log(phone, timestamp DESC);
        """)
        
        # NEW TABLE: Whitelist events for tracking additions/removals
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
        CREATE INDEX IF NOT EXISTS idx_subscribers_phone 
        ON subscribers(phone);
        """)
        
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_subscribers_customer_id 
        ON subscribers(stripe_customer_id);
        """)
        
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_subscription_events_customer_id 
        ON subscription_events(stripe_customer_id);
        """)
        
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_whitelist_events_phone 
        ON whitelist_events(phone, timestamp DESC);
        """)
        
        conn.commit()

# === Helper Functions ===
def extract_phone_from_session(session):
    """Extract phone from checkout session metadata or custom fields"""
    try:
        # Check session metadata
        if hasattr(session, 'metadata') and session.metadata and session.metadata.get('phone'):
            return session.metadata['phone']
        
        # Check custom fields if they exist
        if hasattr(session, 'custom_fields') and session.custom_fields:
            for field in session.custom_fields:
                if field.get('key') == 'phone_number':
                    return field.get('text', {}).get('value')
        
        # Check if phone number collection was enabled
        if hasattr(session, 'customer_details') and session.customer_details:
            phone = session.customer_details.get('phone')
            if phone:
                return phone
        
        return None
    except Exception as e:
        logger.error(f"Error extracting phone from session: {e}")
        return None

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

def store_customer_data(phone, email, customer_id, status):
    """Store customer data in database"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO subscribers 
                (phone, email, stripe_customer_id, status, last_updated)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (phone, email, customer_id, status))
            conn.commit()
            logger.info(f"📊 Stored customer data: {phone} -> {status}")
    except Exception as e:
        logger.error(f"Error storing customer data: {e}")

def get_phone_from_customer_id(customer_id):
    """Get phone number from database using customer ID"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT phone FROM subscribers 
                WHERE stripe_customer_id = ?
                ORDER BY last_updated DESC
                LIMIT 1
            """, (customer_id,))
            result = c.fetchone()
            return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting phone from customer ID: {e}")
        return None

def log_subscription_event(phone, email, customer_id, event_type):
    """Log subscription events to database"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO subscription_events 
                (phone, email, stripe_customer_id, event_type, timestamp)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (phone, email, customer_id, event_type))
            conn.commit()
            logger.info(f"📋 Logged event: {event_type} for {phone}")
    except Exception as e:
        logger.error(f"Error logging subscription event: {e}")

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
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO usage_analytics (phone, intent_type, success, response_time_ms)
            VALUES (?, ?, ?, ?)
        """, (phone, intent_type, success, response_time_ms))
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

# === Rest of the code remains the same... ===
# [Content Filter, Intent Detection, Web Search, Claude Integration, Stripe Webhooks, Main SMS Handler, etc.]

# For brevity, I'm not including all the remaining functions, but they would be identical to your original code
# The key changes are in the whitelist management functions and the new admin endpoints

# Initialize database on startup
init_db()

if __name__ == "__main__":
    logger.info(f"🚀 Starting Hey Alex SMS Assistant v{APP_VERSION}")
    logger.info(f"📋 Latest changes: {CHANGELOG[APP_VERSION]}")
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
