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
APP_VERSION = "2.9"
CHANGELOG = {
    "2.9": "Improved response quality: removed unnecessary location mentions, increased character limit to 700, refined Claude prompts",
    "2.8": "Added comprehensive debugging and user profile recovery system to prevent onboarding loops",
    "2.7": "Enhanced news detection and Google News integration with SerpAPI, improved topic-specific news searches",
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

# === Debug Functions ===
def debug_database_state():
    """Debug function to check database state"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            # Check user profiles
            c.execute("SELECT COUNT(*) FROM user_profiles")
            profile_count = c.fetchone()[0]
            
            c.execute("SELECT phone, first_name, onboarding_completed FROM user_profiles LIMIT 10")
            profiles = c.fetchall()
            
            # Check messages
            c.execute("SELECT COUNT(*) FROM messages")
            message_count = c.fetchone()[0]
            
            # Check whitelist content
            whitelist = load_whitelist()
            
            logger.info(f"🔍 DATABASE DEBUG:")
            logger.info(f"  Database path: {DB_PATH}")
            logger.info(f"  User profiles: {profile_count}")
            logger.info(f"  Messages: {message_count}")
            logger.info(f"  Whitelist entries: {len(whitelist)}")
            logger.info(f"  Sample profiles: {profiles}")
            logger.info(f"  Whitelist content: {list(whitelist)[:5]}")  # First 5 entries
            
            return {
                'profile_count': profile_count,
                'message_count': message_count,
                'whitelist_count': len(whitelist),
                'profiles': profiles,
                'whitelist': list(whitelist)
            }
            
    except Exception as e:
        logger.error(f"💥 Database debug error: {e}")
        return None

def debug_phone_lookup(phone):
    """Debug function to check phone number lookup"""
    try:
        normalized = normalize_phone_number(phone)
        logger.info(f"🔍 PHONE DEBUG:")
        logger.info(f"  Original: {phone}")
        logger.info(f"  Normalized: {normalized}")
        
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            # Try exact match
            c.execute("SELECT phone, first_name FROM user_profiles WHERE phone = ?", (phone,))
            exact_match = c.fetchone()
            
            # Try normalized match
            c.execute("SELECT phone, first_name FROM user_profiles WHERE phone = ?", (normalized,))
            normalized_match = c.fetchone()
            
            # Get all phone numbers to compare
            c.execute("SELECT phone FROM user_profiles")
            all_phones = [row[0] for row in c.fetchall()]
            
            logger.info(f"  Exact match: {exact_match}")
            logger.info(f"  Normalized match: {normalized_match}")
            logger.info(f"  All phones in DB: {all_phones}")
            
            return {
                'original': phone,
                'normalized': normalized,
                'exact_match': exact_match,
                'normalized_match': normalized_match,
                'all_phones': all_phones
            }
            
    except Exception as e:
        logger.error(f"💥 Phone debug error: {e}")
        return None

def check_existing_user_before_onboarding(sender):
    """Check if user exists before starting onboarding"""
    # Check whitelist first
    whitelist = load_whitelist()
    if sender not in whitelist:
        logger.info(f"📝 {sender} not in whitelist, this should be a new user")
        return False
    
    # Debug database state
    debug_state = debug_database_state()
    if debug_state and debug_state['profile_count'] > 0:
        logger.info(f"📊 Database has {debug_state['profile_count']} profiles, investigating...")
        
        # Check if this specific user exists
        profile_debug = debug_phone_lookup(sender)
        if profile_debug:
            logger.info(f"🔍 Phone lookup results: {profile_debug}")
    
    return False

# === Intent Detection ===
@dataclass
class IntentResult:
    type: str
    entities: Dict[str, Any]
    confidence: float = 1.0

def detect_news_intent(text: str) -> Optional[IntentResult]:
    """Enhanced news intent detection with better pattern matching"""
    news_patterns = [
        # Direct news requests
        r'\b(latest|current|recent|new|breaking)\s+(news|headlines)\b',
        r'\bheadlines?\b',
        r'\bnews\s+(about|on|regarding)\b',
        r'\bwhat\'?s\s+(happening|going\s+on|new)\b',
        r'\btoday\'?s\s+news\b',
        r'\bbreaking\s+news\b',
        
        # Topic-specific news
        r'\bnews\s+(from|about)\s+(.+)',
        r'\b(headlines|stories)\s+(from|about|on)\s+(.+)',
        r'\blatest\s+(.+)\s+news\b',
        
        # Source-specific requests
        r'\b(google|cnn|bbc|reuters|ap|associated\s+press)\s+news\b',
        r'\bnews\s+from\s+(google|cnn|bbc|reuters|ap)\b',
        
        # Current events
        r'\bwhat\'?s\s+happening\s+(in|with|about)\s+(.+)',
        r'\bcurrent\s+events\b',
        r'\btoday\'?s\s+(top\s+)?stories\b'
    ]
    
    text_lower = text.lower().strip()
    
    for pattern in news_patterns:
        match = re.search(pattern, text_lower, re.I)
        if match:
            # Extract topic or source if specified
            topic = None
            source = None
            
            # Look for specific sources
            source_patterns = {
                'google': r'\b(google)\s+news\b',
                'cnn': r'\b(cnn)\b',
                'bbc': r'\b(bbc)\b', 
                'reuters': r'\b(reuters)\b',
                'ap': r'\b(ap|associated\s+press)\b'
            }
            
            for src, src_pattern in source_patterns.items():
                if re.search(src_pattern, text_lower):
                    source = src
                    break
            
            # Extract topic from various patterns
            topic_patterns = [
                r'news\s+(?:about|on|regarding)\s+(.+?)(?:\s+from|\s+on|\?|$)',
                r'latest\s+(.+?)\s+news',
                r'headlines?\s+(?:about|on)\s+(.+?)(?:\?|$)',
                r'what\'?s\s+happening\s+(?:in|with|about)\s+(.+?)(?:\?|$)'
            ]
            
            for topic_pattern in topic_patterns:
                topic_match = re.search(topic_pattern, text_lower)
                if topic_match:
                    topic = topic_match.group(1).strip()
                    # Clean up common words
                    topic = re.sub(r'\b(the|a|an|in|on|at|from)\b', '', topic).strip()
                    break
            
            logger.info(f"📰 News intent detected - Topic: {topic}, Source: {source}")
            
            return IntentResult("news", {
                "topic": topic,
                "source": source,
                "original_query": text,
                "requires_search": True
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
    """Enhanced intent detection with better news support"""
    
    # Check for news intent first since it's a common request
    news_intent = detect_news_intent(text)
    if news_intent:
        return news_intent
    
    # Check for weather intent
    weather_intent = detect_weather_intent(text)
    if weather_intent:
        return weather_intent
    
    # Add other intent detections here...
    
    return None

# === Enhanced News Functions ===
def get_google_news(query=None, num_results=5):
    """
    Fetch news specifically from Google News using SerpAPI
    """
    if not SERPAPI_API_KEY:
        return "News search unavailable - service not configured."
    
    # Default to general news if no specific query
    if not query:
        query = "latest news headlines"
    
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google_news",  # Specific Google News engine
        "q": query,
        "num": min(num_results, 10),
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
        "gl": "us",
        "tbm": "nws"  # News tab
    }
    
    try:
        logger.info(f"📰 Fetching Google News for: {query}")
        
        response = requests.get(url, params=params, timeout=15)
        
        if response.status_code != 200:
            logger.error(f"❌ Google News API error: {response.status_code}")
            return f"News service temporarily unavailable (Error: {response.status_code})"
            
        data = response.json()
        
        # Check for API errors
        if 'error' in data:
            logger.error(f"❌ SerpAPI Google News error: {data['error']}")
            return "News service error. Please try again later."
        
        logger.info(f"📊 Google News response keys: {list(data.keys())}")
        
        # Process Google News results
        news_results = data.get("news_results", [])
        
        if not news_results:
            logger.warning(f"⚠️ No news results found for: {query}")
            return f"No recent news found for '{query}'. Try a different topic."
        
        # Format the top news stories
        formatted_news = []
        
        for i, article in enumerate(news_results[:3]):  # Top 3 stories
            title = article.get('title', '')
            source = article.get('source', '')
            date = article.get('date', '')
            snippet = article.get('snippet', '')
            
            # Build news item
            news_item = f"📰 {title}"
            
            if source:
                news_item += f" — {source}"
            
            if date:
                # Clean up date format if needed
                clean_date = date.replace(' ago', '').replace('hours', 'hrs').replace('minutes', 'min')
                news_item += f" ({clean_date})"
            
            if snippet and len(news_item) < 400:  # Add snippet if there's room
                snippet_preview = snippet[:100] + "..." if len(snippet) > 100 else snippet
                news_item += f"\n{snippet_preview}"
            
            formatted_news.append(news_item)
        
        # Combine all news items
        if len(formatted_news) == 1:
            result = formatted_news[0]
        else:
            result = "\n\n".join(formatted_news)
        
        # Ensure it fits SMS limits
        if len(result) > 1500:
            result = result[:1497] + "..."
        
        logger.info(f"✅ Formatted {len(formatted_news)} Google News articles")
        return result
        
    except Exception as e:
        logger.error(f"💥 Google News fetch error: {e}")
        return "News service temporarily unavailable. Please try again later."

def get_topic_news(topic, source=None, num_results=3):
    """
    Get news about a specific topic, optionally from a specific source
    """
    if not SERPAPI_API_KEY:
        return "News search unavailable - service not configured."
    
    # Build search query
    if source:
        if source.lower() == 'google':
            # For Google News, use the dedicated function
            return get_google_news(topic, num_results)
        else:
            query = f"{topic} site:{source}.com"
    else:
        query = f"{topic} news"
    
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google",
        "q": query,
        "num": min(num_results, 5),
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
        "gl": "us",
        "tbm": "nws"  # News search
    }
    
    try:
        logger.info(f"📰 Searching news for topic: {topic}, source: {source}")
        
        response = requests.get(url, params=params, timeout=15)
        
        if response.status_code != 200:
            logger.error(f"❌ News search API error: {response.status_code}")
            return f"News search temporarily unavailable"
            
        data = response.json()
        
        # Process news results
        news_results = data.get("news_results", [])
        
        if not news_results:
            # Try regular search if news results are empty
            organic_results = data.get("organic_results", [])
            if organic_results:
                # Filter for news-like results
                news_results = [
                    result for result in organic_results 
                    if any(indicator in result.get('source', '').lower() 
                          for indicator in ['news', 'cnn', 'bbc', 'reuters', 'ap', 'times'])
                ]
        
        if not news_results:
            return f"No recent news found about '{topic}'. Try a different topic or check the spelling."
        
        # Format the results
        if len(news_results) == 1:
            article = news_results[0]
            title = article.get('title', '')
            source_name = article.get('source', '')
            snippet = article.get('snippet', '')
            
            result = f"📰 {title}"
            if source_name:
                result += f" — {source_name}"
            if snippet:
                result += f"\n{snippet}"
        else:
            # Multiple articles - create a summary
            headlines = []
            for article in news_results[:3]:
                title = article.get('title', '')
                source_name = article.get('source', '')
                if title:
                    headline = f"• {title}"
                    if source_name:
                        headline += f" ({source_name})"
                    headlines.append(headline)
            
            result = f"📰 Latest news about {topic}:\n\n" + "\n\n".join(headlines)
        
        # Ensure SMS length limits
        if len(result) > 1500:
            result = result[:1497] + "..."
        
        logger.info(f"✅ Found news about '{topic}' from {len(news_results)} sources")
        return result
        
    except Exception as e:
        logger.error(f"💥 Topic news search error: {e}")
        return f"Unable to fetch news about '{topic}' right now. Please try again later."

def get_breaking_news():
    """
    Get breaking/trending news stories
    """
    if not SERPAPI_API_KEY:
        return "Breaking news unavailable - service not configured."
    
    # Search for breaking news
    breaking_queries = [
        "breaking news today",
        "trending news headlines", 
        "top news stories today"
    ]
    
    for query in breaking_queries:
        try:
            result = get_google_news(query, num_results=3)
            if "unavailable" not in result.lower() and "no recent news" not in result.lower():
                return f"🚨 {result}"
        except:
            continue
    
    return "Unable to fetch breaking news right now. Please try again later."

# === Enhanced Web Search ===
def enhanced_web_search(q, num=3, search_type="general"):
    """Enhanced web search with better news handling"""
    
    if not SERPAPI_API_KEY:
        return "Search unavailable - service not configured."
    
    q = q.strip()
    if len(q) < 2:
        return "Search query too short."
    
    # Handle news searches specifically
    if search_type == "news":
        # Check if it's a general news request
        if any(keyword in q.lower() for keyword in ['latest news', 'headlines', 'breaking news', 'current events']):
            return get_google_news()
        else:
            # Topic-specific news
            return get_topic_news(q)
    
    # For other search types, use the existing logic
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google",
        "q": q,
        "num": min(num, 5),
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
        "gl": "us",
    }
    
    if search_type == "local":
        params["engine"] = "google_maps"
    elif search_type == "news":
        params["tbm"] = "nws"
    
    try:
        logger.info(f"🔍 Enhanced search: {q} (type: {search_type})")
        r = requests.get(url, params=params, timeout=15)
        
        if r.status_code != 200:
            return f"Search error (status {r.status_code})"
            
        data = r.json()
        
        # Process results based on search type
        if search_type == "news":
            news_results = data.get("news_results", [])
            if news_results:
                top_article = news_results[0]
                title = top_article.get('title', '')
                source = top_article.get('source', '')
                snippet = top_article.get('snippet', '')
                
                result = f"📰 {title}"
                if source:
                    result += f" — {source}"
                if snippet:
                    result += f"\n{snippet}"
                return result[:500]
        
        # Default processing for other types
        organic_results = data.get("organic_results", [])
        if organic_results:
            top = organic_results[0]
            title = top.get("title", "")
            snippet = top.get("snippet", "")
            
            result = f"{title}"
            if snippet:
                result += f" — {snippet}"
            return result[:500]
        
        return f"No results found for '{q}'."
        
    except Exception as e:
        logger.error(f"Enhanced search error: {e}")
        return "Search service temporarily unavailable."

# === User Profile & Onboarding System ===
def get_user_profile(phone):
    """Enhanced get_user_profile with debugging and phone format flexibility"""
    try:
        # First try the original phone number
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT first_name, location, onboarding_step, onboarding_completed
                FROM user_profiles
                WHERE phone = ?
            """, (phone,))
            result = c.fetchone()
            
            if result:
                logger.info(f"✅ Found profile for {phone} (exact match)")
                return {
                    'first_name': result[0],
                    'location': result[1],
                    'onboarding_step': result[2],
                    'onboarding_completed': bool(result[3])
                }
            
            # If not found, try normalized phone number
            normalized = normalize_phone_number(phone)
            if normalized != phone:
                c.execute("""
                    SELECT first_name, location, onboarding_step, onboarding_completed
                    FROM user_profiles
                    WHERE phone = ?
                """, (normalized,))
                result = c.fetchone()
                
                if result:
                    logger.info(f"✅ Found profile for {phone} (normalized as {normalized})")
                    return {
                        'first_name': result[0],
                        'location': result[1],
                        'onboarding_step': result[2],
                        'onboarding_completed': bool(result[3])
                    }
            
            # If still not found, log debug info for whitelisted users
            whitelist = load_whitelist()
            if phone in whitelist or normalized in whitelist:
                logger.warning(f"❌ Whitelisted user {phone} has no profile - may need recovery")
                debug_phone_lookup(phone)
            
            return None
            
    except Exception as e:
        logger.error(f"Error getting user profile for {phone}: {e}")
        return None

def create_user_profile(phone):
    """Create new user profile for onboarding with enhanced debugging"""
    try:
        original_phone = phone
        phone = normalize_phone_number(phone)
        
        logger.info(f"📝 Creating user profile - Original: {original_phone}, Normalized: {phone}")
        
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            
            # Check if profile already exists
            c.execute("SELECT phone, onboarding_completed FROM user_profiles WHERE phone = ?", (phone,))
            existing = c.fetchone()
            
            if existing:
                logger.info(f"👤 Profile already exists for {phone}: completed={existing[1]}")
                return True
            
            # Create new profile
            c.execute("""
                INSERT INTO user_profiles 
                (phone, onboarding_step, onboarding_completed, created_date, updated_date)
                VALUES (?, 1, FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (phone,))
            
            rows_affected = c.rowcount
            conn.commit()
            
            # Verify creation
            c.execute("SELECT id, phone FROM user_profiles WHERE phone = ?", (phone,))
            created = c.fetchone()
            
            logger.info(f"📝 Profile creation result - Rows affected: {rows_affected}, Created: {created}")
            
            if created:
                logger.info(f"✅ Successfully created user profile for {phone} (ID: {created[0]})")
                return True
            else:
                logger.error(f"❌ Failed to create profile for {phone} - no record found after insert")
                return False
            
    except Exception as e:
        logger.error(f"💥 Error creating user profile for {phone}: {e}")
        # Log database state for debugging
        try:
            with closing(sqlite3.connect(DB_PATH)) as conn:
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM user_profiles")
                count = c.fetchone()[0]
                logger.error(f"📊 Current profile count in database: {count}")
        except:
            logger.error("📊 Could not check database state")
        return False

def update_user_profile(phone, first_name=None, location=None, onboarding_step=None, onboarding_completed=None):
    """Update user profile information"""
    try:
        phone = normalize_phone_number(phone)
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
        phone = normalize_phone_number(phone)
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

def log_whitelist_event(phone, action):
    """Log whitelist addition/removal events"""
    try:
        phone = normalize_phone_number(phone)
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
            
            # User profiles table for onboarding
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
            
            # Other tables...
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
            CREATE TABLE IF NOT EXISTS usage_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                intent_type TEXT,
                success BOOLEAN,
                response_time_ms INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """)
            
            conn.commit()
            
            # Check for existing data and log detailed info
            c.execute("SELECT COUNT(*) FROM user_profiles")
            user_count = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM messages")  
            message_count = c.fetchone()[0]
            
            logger.info(f"📊 Database initialized successfully")
            logger.info(f"📊 Found {user_count} user profiles and {message_count} messages")
            
            # If we have data, show some samples for debugging
            if user_count > 0:
                c.execute("""
                    SELECT phone, first_name, location, onboarding_completed, created_date 
                    FROM user_profiles 
                    ORDER BY created_date DESC 
                    LIMIT 5
                """)
                recent_users = c.fetchall()
                logger.info(f"📊 Recent users: {recent_users}")
            
    except Exception as e:
        logger.error(f"💥 Database initialization error: {e}")
        raise

# === SMS Functions ===
def send_sms(to_number, message, bypass_quota=False):
    if not CLICKSEND_USERNAME or not CLICKSEND_API_KEY:
        logger.error("ClickSend credentials not configured")
        return {"error": "SMS service not configured"}
    
    to_number = normalize_phone_number(to_number)
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
    phone = normalize_phone_number(phone)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO sms_delivery_log (phone, message_content, clicksend_response, delivery_status, message_id)
            VALUES (?, ?, ?, ?, ?)
        """, (phone, message_content, json.dumps(clicksend_response), delivery_status, message_id))
        conn.commit()

def save_message(phone, role, content, intent_type=None, response_time_ms=None):
    """Save message with enhanced debugging"""
    try:
        original_phone = phone
        phone = normalize_phone_number(phone)
        
        logger.info(f"💬 Saving message - Phone: {original_phone} -> {phone}, Role: {role}, Content: {content[:50]}...")
        
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO messages (phone, role, content, intent_type, response_time_ms) 
                VALUES (?, ?, ?, ?, ?)
            """, (phone, role, content, intent_type, response_time_ms))
            
            rows_affected = c.rowcount
            message_id = c.lastrowid
            conn.commit()
            
            logger.info(f"💬 Message saved - ID: {message_id}, Rows affected: {rows_affected}")
            
            # Verify save
            c.execute("SELECT id FROM messages WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,))
            verify = c.fetchone()
            
            if verify:
                logger.info(f"✅ Message verification successful - Latest message ID: {verify[0]}")
            else:
                logger.error(f"❌ Message verification failed - no messages found for {phone}")
                
    except Exception as e:
        logger.error(f"💥 Error saving message for {phone}: {e}")
        # Log database state
        try:
            with closing(sqlite3.connect(DB_PATH)) as conn:
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM messages")
                count = c.fetchone()[0]
                logger.error(f"📊 Current message count in database: {count}")
        except:
            logger.error("📊 Could not check message count")

def load_history(phone, limit=4):
    phone = normalize_phone_number(phone)
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
    phone = normalize_phone_number(phone)
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
    
    phone = normalize_phone_number(phone)
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
- Keep responses under 700 characters when possible for SMS
- Be friendly and helpful  
- You DO have access to web search capabilities
- For specific information requests, provide direct answers when you have knowledge, or respond with "Let me search for [specific topic]" if you need current information
- Never make up detailed information - always offer to search for accurate, current details when uncertain
- Be conversational and helpful
- Don't mention searching unless you actually need to search
- Don't add location context unless the query specifically relates to location"""
        
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
        
        if len(reply) > 700:
            reply = reply[:697] + "..."
            
        response_time = int((time.time() - start_time) * 1000)
        log_usage_analytics(phone, "claude_chat", True, response_time)
        
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
    
    # Normalize sender phone number
    sender = normalize_phone_number(sender)
    
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
    
    # Enhanced user profile checking with debugging
    profile = get_user_profile(sender)
    logger.info(f"👤 User profile for {sender}: {profile}")
    
    # Special case: If user is in whitelist but has no profile, it might be a recovery case
    if not profile:
        logger.warning(f"🔍 User {sender} in whitelist but no profile found - investigating...")
        
        # Enhanced debugging for this specific case
        debug_state = debug_database_state()
        debug_phone_lookup(sender)
        
        # Check database permissions and connectivity
        try:
            with closing(sqlite3.connect(DB_PATH)) as conn:
                c = conn.cursor()
                c.execute("SELECT 1")
                logger.info(f"✅ Database connection test successful")
                
                # Check if we can write to the database (using valid role)
                c.execute("INSERT INTO messages (phone, role, content) VALUES (?, 'user', 'connection_test')", (sender,))
                test_id = c.lastrowid
                c.execute("DELETE FROM messages WHERE id = ?", (test_id,))
                conn.commit()
                logger.info(f"✅ Database write test successful")
                
        except Exception as db_error:
            logger.error(f"💥 Database connectivity issue: {db_error}")
            
            # Send error message and return
            error_msg = "Sorry, I'm having database issues. Please contact support."
            try:
                send_sms(sender, error_msg, bypass_quota=True)
                return jsonify({"message": "Database error sent"}), 500
            except Exception as sms_error:
                logger.error(f"Failed to send database error message: {sms_error}")
                return jsonify({"error": "Database connectivity failed"}), 500
        
        # If we have other users in the database, this might be a lookup issue
        if debug_state and debug_state['profile_count'] > 0:
            logger.warning(f"📊 Database has {debug_state['profile_count']} profiles, but lookup failed for {sender}")
        
        # Try to create new profile with enhanced logging
        logger.info(f"📝 Attempting to create new profile for {sender}")
        creation_success = create_user_profile(sender)
        
        if not creation_success:
            logger.error(f"💥 Failed to create profile for {sender}")
            error_msg = "Sorry, I'm having trouble setting up your account. Please contact support."
            try:
                send_sms(sender, error_msg, bypass_quota=True)
                return jsonify({"message": "Profile creation failed"}), 500
            except Exception as sms_error:
                logger.error(f"Failed to send profile creation error: {sms_error}")
                return jsonify({"error": "Profile creation failed"}), 500
        
        # Try to get the profile again
        profile = get_user_profile(sender)
        if not profile:
            logger.error(f"💥 Profile creation appeared successful but still can't retrieve for {sender}")
            error_msg = "Sorry, I'm having trouble with your account setup. Please contact support."
            try:
                send_sms(sender, error_msg, bypass_quota=True)
                return jsonify({"message": "Profile retrieval failed"}), 500
            except Exception as sms_error:
                logger.error(f"Failed to send profile retrieval error: {sms_error}")
                return jsonify({"error": "Profile retrieval failed"}), 500
        
        logger.info(f"✅ Successfully created and retrieved profile for {sender}")
        
        try:
            send_sms(sender, ONBOARDING_NAME_MSG, bypass_quota=True)
            save_message(sender, "assistant", ONBOARDING_NAME_MSG, "onboarding_start", 0)
            return jsonify({"message": "Onboarding started"}), 200
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
    
    # User is onboarded, process normal queries with enhanced news support
    intent = detect_intent(body, sender)
    intent_type = intent.type if intent else "general"
    
    # Get user context for personalized responses
    user_context = get_user_context_for_queries(sender)
    
    try:
        # Enhanced processing based on intent
        if intent and intent.type == "news":
            # Handle news requests
            entities = intent.entities
            topic = entities.get("topic")
            source = entities.get("source")
            
            logger.info(f"📰 Processing news request - Topic: {topic}, Source: {source}")
            
            if source == "google" or "google news" in body.lower():
                # Specific Google News request
                if topic:
                    response_msg = get_google_news(topic)
                else:
                    response_msg = get_google_news()
            elif topic and source:
                # Topic + specific source
                response_msg = get_topic_news(topic, source)
            elif topic:
                # Just topic, any source
                response_msg = get_topic_news(topic)
            elif "breaking" in body.lower():
                # Breaking news request
                response_msg = get_breaking_news()
            else:
                # General news request
                response_msg = get_google_news()
            
            # Personalize if user is onboarded
            if user_context['personalized']:
                first_name = user_context['first_name']
                response_msg = f"Hi {first_name}! " + response_msg
        
        elif intent and intent.type == "weather":
            # Use user's location if no city specified and user is onboarded
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
            # Use Claude for general queries with user context
            if user_context['personalized']:
                # Only add location context for location-specific queries
                location_keywords = ['near', 'in', 'around', 'restaurant', 'weather', 'business', 'store']
                if any(keyword in body.lower() for keyword in location_keywords):
                    personalized_msg = f"User's name is {user_context['first_name']} and they live in {user_context['location']}. " + body
                else:
                    personalized_msg = f"User's name is {user_context['first_name']}. " + body
                response_msg = ask_claude(sender, personalized_msg)
            else:
                response_msg = ask_claude(sender, body)
            
            # If Claude suggests a search, perform it
            if "Let me search for" in response_msg:
                search_term = body
                
                # Check if it's a news-related search
                if any(keyword in body.lower() for keyword in ['news', 'headlines', 'breaking', 'current events']):
                    # Detect if it's news and route accordingly
                    news_intent_check = detect_news_intent(body)
                    if news_intent_check:
                        if news_intent_check.entities.get("topic"):
                            response_msg = get_topic_news(news_intent_check.entities["topic"])
                        else:
                            response_msg = get_google_news()
                    else:
                        response_msg = enhanced_web_search(search_term, search_type="news")
                else:
                    # Only add location context for location-relevant searches
                    location_keywords = ['near', 'in', 'around', 'restaurant', 'weather', 'business', 'store']
                    if (user_context['personalized'] and 
                        any(keyword in body.lower() for keyword in location_keywords) and 
                        not any(keyword in body.lower() for keyword in ['in ', 'near ', 'at '])):
                        search_term += f" in {user_context['location']}"
                    response_msg = enhanced_web_search(search_term, search_type="general")
        
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

# === Debug and Admin Endpoints ===
@app.route('/debug/user/<phone>', methods=['GET'])
def debug_user(phone):
    """Debug endpoint to check a specific user"""
    try:
        phone = normalize_phone_number(phone)
        
        # Get debug info
        db_state = debug_database_state()
        phone_debug = debug_phone_lookup(phone)
        profile = get_user_profile(phone)
        
        return jsonify({
            'phone': phone,
            'database_state': db_state,
            'phone_debug': phone_debug,
            'profile': profile,
            'in_whitelist': phone in load_whitelist()
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/recover-user', methods=['POST'])
def recover_user():
    """Recover a user who lost their profile"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        first_name = data.get('first_name', 'User')
        location = data.get('location', 'Unknown')
        
        if not phone:
            return jsonify({'error': 'Phone required'}), 400
        
        phone = normalize_phone_number(phone)
        
        # Add to whitelist if not already there
        add_to_whitelist(phone, send_welcome=False)
        
        # Create or update profile as completed
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO user_profiles 
                (phone, first_name, location, onboarding_step, onboarding_completed, created_date, updated_date)
                VALUES (?, ?, ?, 3, TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (phone, first_name, location))
            conn.commit()
        
        logger.info(f"🔧 Recovered user {phone}: {first_name} in {location}")
        
        return jsonify({
            'success': True,
            'message': f'Recovered user {phone}',
            'phone': phone,
            'first_name': first_name,
            'location': location
        })
        
    except Exception as e:
        logger.error(f"Error recovering user: {e}")
        return jsonify({'error': str(e)}), 500

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
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT 
                    up.phone, 
                    up.first_name, 
                    up.location, 
                    up.onboarding_step,
                    up.onboarding_completed,
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
                    'created_date': row[5]
                })
            
            return jsonify({
                'total_users': len(users),
                'users': users
            })
            
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return jsonify({"error": str(e)}), 500

# === Test Endpoints ===
@app.route('/test/news', methods=['GET'])
def test_news():
    """Test endpoint to verify news functionality"""
    try:
        query = request.args.get('q', 'latest news')
        search_type = request.args.get('type', 'google')
        
        logger.info(f"🧪 Testing news with query: {query}, type: {search_type}")
        
        if search_type == 'google':
            result = get_google_news(query)
        elif search_type == 'topic':
            result = get_topic_news(query)
        elif search_type == 'breaking':
            result = get_breaking_news()
        else:
            result = enhanced_web_search(query, search_type="news")
        
        return jsonify({
            "query": query,
            "type": search_type,
            "result": result,
            "length": len(result)
        })
        
    except Exception as e:
        logger.error(f"Error in news test: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/test/intent', methods=['POST'])
def test_intent():
    """Test endpoint to verify intent detection"""
    try:
        data = request.get_json()
        text = data.get('text', '')
        
        if not text:
            return jsonify({"error": "Text required"}), 400
        
        intent = detect_intent(text)
        
        return jsonify({
            "text": text,
            "intent": {
                "type": intent.type if intent else None,
                "entities": intent.entities if intent else {},
                "confidence": intent.confidence if intent else 0
            }
        })
        
    except Exception as e:
        logger.error(f"Error in intent test: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/debug/database-test/<phone>', methods=['POST'])
def test_database_operations(phone):
    """Test database operations for a specific phone number"""
    try:
        phone = normalize_phone_number(phone)
        results = {}
        
        # Test 1: Basic database connection
        try:
            with closing(sqlite3.connect(DB_PATH)) as conn:
                c = conn.cursor()
                c.execute("SELECT 1")
                results['connection'] = "✅ Success"
        except Exception as e:
            results['connection'] = f"❌ Failed: {e}"
            return jsonify(results)
        
        # Test 2: Create user profile
        try:
            success = create_user_profile(phone)
            results['create_profile'] = "✅ Success" if success else "❌ Failed"
        except Exception as e:
            results['create_profile'] = f"❌ Exception: {e}"
        
        # Test 3: Retrieve user profile
        try:
            profile = get_user_profile(phone)
            results['get_profile'] = f"✅ Found: {profile}" if profile else "❌ Not found"
        except Exception as e:
            results['get_profile'] = f"❌ Exception: {e}"
        
        # Test 4: Save message
        try:
            save_message(phone, "user", "test message")
            results['save_message'] = "✅ Success"
        except Exception as e:
            results['save_message'] = f"❌ Exception: {e}"
        
        # Test 5: Load history
        try:
            history = load_history(phone, 1)
            results['load_history'] = f"✅ Found {len(history)} messages"
        except Exception as e:
            results['load_history'] = f"❌ Exception: {e}"
        
        return jsonify({
            'phone': phone,
            'tests': results,
            'database_path': DB_PATH
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Initialize database on startup
init_db()

if __name__ == "__main__":
    logger.info(f"🚀 Starting Hey Alex SMS Assistant v{APP_VERSION}")
    logger.info(f"📋 Latest changes: {CHANGELOG[APP_VERSION]}")
    
    # Test news functionality on startup if SERPAPI_API_KEY is available
    if SERPAPI_API_KEY:
        try:
            test_result = get_google_news("tech news", 1)
            if "unavailable" not in test_result.lower():
                logger.info("✅ News functionality test passed")
            else:
                logger.warning("⚠️ News functionality test returned unavailable")
        except Exception as e:
            logger.error(f"❌ News functionality test failed: {e}")
    else:
        logger.warning("⚠️ SERPAPI_API_KEY not set - news functionality will be limited")
    
    # Debug startup state
    logger.info("🔍 Startup Debug Information:")
    debug_database_state()
    
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
