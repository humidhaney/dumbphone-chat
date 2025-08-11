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
APP_VERSION = "2.5"
CHANGELOG = {
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

# === Stripe Test Endpoints ===
@app.route('/test/stripe', methods=['GET'])
def test_stripe_connection():
    """Test Stripe API connection"""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "STRIPE_SECRET_KEY not configured"}), 400
    
    try:
        # Test API connection by retrieving account info
        account = stripe.Account.retrieve()
        
        return jsonify({
            "status": "success",
            "message": "Stripe API connection successful",
            "account_id": account.id,
            "business_profile": account.business_profile.name if account.business_profile else "Not set",
            "country": account.country,
            "currency": account.default_currency,
            "charges_enabled": account.charges_enabled,
            "payouts_enabled": account.payouts_enabled
        })
        
    except stripe.error.AuthenticationError as e:
        return jsonify({
            "status": "error",
            "message": "Invalid Stripe API key",
            "error": str(e)
        }), 401
        
    except Exception as e:
        return jsonify({
            "status": "error", 
            "message": "Stripe connection failed",
            "error": str(e)
        }), 500

@app.route('/test/webhook', methods=['POST'])
def test_webhook():
    """Test webhook without Stripe signature verification"""
    try:
        payload = request.get_json()
        
        logger.info(f"🧪 Test webhook received: {json.dumps(payload, indent=2)}")
        
        # Simulate different event types
        event_type = payload.get('type', 'test_event')
        
        if event_type == 'checkout.session.completed':
            test_session = {
                'id': 'cs_test_123',
                'customer': 'cus_test_123',
                'customer_details': {
                    'phone': '+15551234567'
                }
            }
            handle_subscription_created(test_session)
            
        return jsonify({
            "status": "success",
            "message": f"Test webhook processed: {event_type}",
            "received_data": payload
        })
        
    except Exception as e:
        logger.error(f"Test webhook error: {e}")
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

@app.route('/test/logs', methods=['GET'])
def get_recent_logs():
    """Get recent subscription events for testing"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT event_type, phone, email, timestamp
                FROM subscription_events
                ORDER BY timestamp DESC
                LIMIT 10
            """)
            
            events = []
            for row in c.fetchall():
                events.append({
                    'event_type': row[0],
                    'phone': row[1], 
                    'email': row[2],
                    'timestamp': row[3]
                })
            
            return jsonify({
                "recent_events": events,
                "whitelist_count": len(load_whitelist()),
                "app_version": APP_VERSION
            })
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === Stripe Webhook Handlers ===
@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    
    if not STRIPE_WEBHOOK_SECRET:
        logger.error("STRIPE_WEBHOOK_SECRET not configured")
        return "Webhook secret not configured", 400
    
    try:
        # Verify webhook signature
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        logger.error(f"Invalid payload: {e}")
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Invalid signature: {e}")
        return "Invalid signature", 400
    
    logger.info(f"🔔 Stripe webhook received: {event['type']}")
    
    # Handle the event
    try:
        if event['type'] == 'checkout.session.completed':
            handle_subscription_created(event['data']['object'])
        elif event['type'] == 'customer.subscription.deleted':
            handle_subscription_cancelled(event['data']['object'])
        elif event['type'] == 'invoice.payment_failed':
            handle_payment_failed(event['data']['object'])
        elif event['type'] == 'customer.subscription.updated':
            handle_subscription_updated(event['data']['object'])
        else:
            logger.info(f"Unhandled event type: {event['type']}")
    except Exception as e:
        logger.error(f"Error processing webhook {event['type']}: {e}")
        return f"Error processing webhook: {str(e)}", 500
    
    return "Success", 200

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
        
        # Add to whitelist
        if add_to_whitelist(phone):
            # Store customer relationship
            store_customer_data(phone, email, customer_id, 'active')
            
            # Log the subscription
            log_subscription_event(phone, email, customer_id, "subscription_created")
            
            # Send welcome SMS
            try:
                send_sms(phone, WELCOME_MSG, bypass_quota=True)
                logger.info(f"📱 Welcome SMS sent to {phone}")
            except Exception as sms_error:
                logger.error(f"Failed to send welcome SMS to {phone}: {sms_error}")
            
            logger.info(f"✅ Added {phone} ({email}) to whitelist - subscription created")
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
        
        # Remove from whitelist
        if remove_from_whitelist(phone):
            # Update customer status
            store_customer_data(phone, email, customer_id, 'cancelled')
            
            # Log the cancellation
            log_subscription_event(phone, email, customer_id, "subscription_cancelled")
            
            # Send goodbye SMS
            goodbye_msg = "Thanks for using Hey Alex! Your subscription has been cancelled. You can resubscribe anytime at heyalex.co"
            try:
                send_sms(phone, goodbye_msg, bypass_quota=True)
                logger.info(f"📱 Goodbye SMS sent to {phone}")
            except Exception as sms_error:
                logger.error(f"Failed to send goodbye SMS to {phone}: {sms_error}")
            
            logger.info(f"❌ Removed {phone} ({email}) from whitelist - subscription cancelled")
        else:
            logger.warning(f"Failed to remove {phone} from whitelist")
        
    except Exception as e:
        logger.error(f"Error handling subscription cancelled: {e}")

def handle_subscription_updated(subscription):
    """Handle subscription updates (e.g., plan changes, renewals)"""
    try:
        logger.info(f"🔄 Processing subscription updated: {subscription.get('id')}")
        
        customer_id = subscription.get('customer')
        status = subscription.get('status')
        
        if not customer_id:
            logger.error("No customer ID in subscription update")
            return
            
        customer = stripe.Customer.retrieve(customer_id)
        email = customer.email
        phone = get_phone_from_customer_id(customer_id) or customer.phone
        
        if not phone:
            logger.error(f"No phone number found for customer {customer_id} ({email})")
            return
        
        phone = normalize_phone_number(phone)
        
        # Handle different status changes
        if status == 'active':
            # Reactivate if needed
            add_to_whitelist(phone)
            store_customer_data(phone, email, customer_id, 'active')
            log_subscription_event(phone, email, customer_id, "subscription_reactivated")
            logger.info(f"✅ Reactivated subscription for {phone}")
            
        elif status in ['canceled', 'unpaid', 'past_due']:
            # Deactivate
            remove_from_whitelist(phone)
            store_customer_data(phone, email, customer_id, status)
            log_subscription_event(phone, email, customer_id, f"subscription_{status}")
            logger.info(f"❌ Deactivated subscription for {phone} (status: {status})")
        
    except Exception as e:
        logger.error(f"Error handling subscription updated: {e}")

def handle_payment_failed(invoice):
    """Handle failed payments"""
    try:
        logger.info(f"💳 Processing payment failed: {invoice.get('id')}")
        
        customer_id = invoice.get('customer')
        if not customer_id:
            logger.error("No customer ID in failed invoice")
            return
            
        customer = stripe.Customer.retrieve(customer_id)
        email = customer.email
        phone = get_phone_from_customer_id(customer_id) or customer.phone
        
        if not phone:
            logger.error(f"No phone number found for customer {customer_id} ({email})")
            return
        
        phone = normalize_phone_number(phone)
        
        # Log the payment failure
        log_subscription_event(phone, email, customer_id, "payment_failed")
        
        # Send payment failed notification
        failed_msg = "⚠️ Hey Alex payment failed. Please update your payment method at heyalex.co to continue service."
        try:
            send_sms(phone, failed_msg, bypass_quota=True)
            logger.info(f"📱 Payment failed SMS sent to {phone}")
        except Exception as sms_error:
            logger.error(f"Failed to send payment failed SMS to {phone}: {sms_error}")
        
        logger.warning(f"💳 Payment failed notification sent to {phone} ({email})")
        
    except Exception as e:
        logger.error(f"Error handling payment failed: {e}")

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

# === Enhanced Whitelist Management ===
def add_to_whitelist(phone):
    """Enhanced whitelist addition with database logging"""
    if not phone:
        return False
        
    wl = load_whitelist()
    if phone not in wl:
        try:
            with open(WHITELIST_FILE, "a") as f:
                f.write(phone + "\n")
            
            logger.info(f"📱 Added {phone} to whitelist")
            return True
        except Exception as e:
            logger.error(f"Failed to add {phone} to whitelist: {e}")
    else:
        logger.info(f"📱 {phone} already in whitelist")
        return True
    return False

def remove_from_whitelist(phone):
    """Enhanced whitelist removal"""
    if not phone:
        return False
        
    wl = load_whitelist()
    if phone in wl:
        try:
            wl.remove(phone)
            with open(WHITELIST_FILE, "w") as f:
                for num in wl:
                    f.write(num + "\n")
            
            logger.info(f"📱 Removed {phone} from whitelist")
            return True
        except Exception as e:
            logger.error(f"Failed to remove {phone} from whitelist: {e}")
    else:
        logger.info(f"📱 {phone} not in whitelist")
        return True
    return False

def load_whitelist():
    try:
        with open(WHITELIST_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

# === API Endpoints ===
@app.route('/stripe/create-checkout-session', methods=['POST'])
def create_checkout_session():
    """Create Stripe checkout session with phone collection"""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 500
    
    try:
        data = request.get_json()
        
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='subscription',
            line_items=[{
                'price': data.get('price_id', 'price_1234567890abcdef'),  # Replace with actual price ID
                'quantity': 1,
            }],
            phone_number_collection={'enabled': True},
            success_url=data.get('success_url', 'https://heyalex.co/success'),
            cancel_url=data.get('cancel_url', 'https://heyalex.co'),
            metadata={
                'source': 'hey_alex_landing'
            }
        )
        
        return jsonify({"id": session.id})
        
    except Exception as e:
        logger.error(f"Error creating checkout session: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/admin/subscribers', methods=['GET'])
def get_subscribers():
    """Admin endpoint to view all subscribers"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT phone, email, stripe_customer_id, status, last_updated
                FROM subscribers
                ORDER BY last_updated DESC
            """)
            subscribers = []
            for row in c.fetchall():
                subscribers.append({
                    'phone': row[0],
                    'email': row[1],
                    'customer_id': row[2],
                    'status': row[3],
                    'last_updated': row[4]
                })
            
            return jsonify({
                'subscribers': subscribers,
                'total': len(subscribers)
            })
    except Exception as e:
        logger.error(f"Error getting subscribers: {e}")
        return jsonify({"error": str(e)}), 500

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
        
        # New tables for Stripe integration
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
        
        conn.commit()

# === Content Filter ===
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

def detect_intent(text: str, phone: str = None) -> Optional[IntentResult]:
    # For now, just detect weather - you can add more intent detection here
    return detect_weather_intent(text)

# === Web Search ===
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

# === Claude Integration ===
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

# === Main SMS Webhook ===
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
        return jsonify({"error": "Missing 'from' field"}), 400
    
    if not body:
        logger.warning(f"⚠️ Empty message body from {sender}")
        return jsonify({"message": "Empty message received"}), 200
    
    # Check monthly quota first
    can_send_sms, usage_info, warning_msg = track_monthly_sms_usage(sender, is_outgoing=False)
    if not can_send_sms:
        logger.warning(f"🚫 QUOTA EXCEEDED: {sender} - {usage_info['current_count']}/{usage_info['monthly_limit']} messages")
        if warning_msg:
            try:
                send_sms(sender, warning_msg, bypass_quota=True)
            except Exception as e:
                logger.error(f"Failed to send quota exceeded message: {e}")
        return jsonify({"message": "Monthly quota exceeded"}), 429
    
    # Check rate limits
    can_send_msg, rate_msg = can_send(sender)
    if not can_send_msg:
        logger.warning(f"🚫 RATE LIMITED: {sender} - {rate_msg}")
        return jsonify({"message": rate_msg}), 429
    
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
    
    # Clear old context
    clear_conversation_context(sender)
    
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
        response_msg = WELCOME_MSG
        try:
            send_sms(sender, response_msg, bypass_quota=True)
            return jsonify({"message": "Welcome message sent"}), 200
        except Exception as e:
            logger.error(f"Failed to send welcome message: {e}")
            return jsonify({"error": "Failed to send welcome"}), 500
    
    # Detect intent
    intent = detect_intent(body, sender)
    intent_type = intent.type if intent else "general"
    
    try:
        # Process based on intent
        if intent and intent.type == "weather":
            city = intent.entities.get("city", "your area")
            query = f"weather forecast {city}" if city != "your area" else "weather forecast"
            response_msg = web_search(query, search_type="general")
        else:
            # Use Claude for general queries
            response_msg = ask_claude(sender, body)
            
            # If Claude suggests a search, perform it
            if "Let me search for" in response_msg:
                search_term = body  # Use original query
                response_msg = web_search(search_term, search_type="general")
        
        # Ensure response is not too long for SMS
        if len(response_msg) > 1600:
            response_msg = response_msg[:1597] + "..."
        
        # Save assistant response
        response_time = int((time.time() - start_time) * 1000)
        save_message(sender, "assistant", response_msg, intent_type, response_time)
        
        # Send quota warning if needed
        if warning_msg:
            try:
                send_sms(sender, warning_msg, bypass_quota=True)
                time.sleep(2)  # Brief delay between messages
            except Exception as e:
                logger.error(f"Failed to send quota warning: {e}")
        
        # Send main response
        result = send_sms(sender, response_msg)
        
        if "error" not in result:
            log_usage_analytics(sender, intent_type, True, response_time)
            logger.info(f"✅ Response sent to {sender} in {response_time}ms")
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

def clear_conversation_context(phone):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            DELETE FROM conversation_context
            WHERE phone = ? AND timestamp < datetime('now', '-10 minutes')
        """, (phone,))
        conn.commit()

# Initialize database on startup
init_db()

if __name__ == "__main__":
    logger.info(f"🚀 Starting Hey Alex SMS Assistant v{APP_VERSION}")
    logger.info(f"📋 Latest changes: {CHANGELOG[APP_VERSION]}")
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
