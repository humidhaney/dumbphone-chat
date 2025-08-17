from flask import Flask, jsonify
import os
import logging
import sys

# Database imports - try psycopg3 first, then psycopg2
POSTGRES_AVAILABLE = False
psycopg = None
RealDictCursor = None

try:
    # Try psycopg3 first (better Python 3.13 support)
    import psycopg
    from psycopg.rows import dict_row
    POSTGRES_AVAILABLE = True
    PSYCOPG_VERSION = 3
    logger = logging.getLogger(__name__)
    logger.info("✅ psycopg3 imported successfully")
except ImportError:
    try:
        # Fallback to psycopg2
        import psycopg2 as psycopg
        from psycopg2.extras import RealDictCursor
        POSTGRES_AVAILABLE = True
        PSYCOPG_VERSION = 2
        logger = logging.getLogger(__name__)
        logger.info("✅ psycopg2 imported successfully")
    except ImportError as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"❌ No PostgreSQL driver available: {e}")

# SQLite fallback
import sqlite3

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

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

def get_db_connection():
    """Get database connection based on environment"""
    if USE_POSTGRES:
        if PSYCOPG_VERSION == 3:
            # psycopg3 syntax
            return psycopg.connect(DATABASE_URL)
        else:
            # psycopg2 syntax
            return psycopg.connect(DATABASE_URL)
    else:
        return sqlite3.connect(DB_PATH)

def execute_query(query, params=None, fetch=False, fetchall=False, fetchone=False):
    """Execute database query with proper connection handling"""
    try:
        if USE_POSTGRES:
            with get_db_connection() as conn:
                if PSYCOPG_VERSION == 3:
                    # psycopg3 syntax
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
                    # psycopg2 syntax
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
            # SQLite
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
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_phone ON user_profiles(phone);",
                "CREATE INDEX IF NOT EXISTS idx_messages_phone_ts ON messages(phone, ts DESC);"
            ]
            
            for table_sql in tables:
                execute_query(table_sql)
            
            logger.info("✅ PostgreSQL tables created successfully")
            
        else:
            # SQLite table creation
            tables = [
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
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_phone ON user_profiles(phone);",
                "CREATE INDEX IF NOT EXISTS idx_messages_phone_ts ON messages(phone, ts DESC);"
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

@app.route('/', methods=['GET'])
def home():
    """Home page"""
    return '''
    <h1>Hey Alex SMS Assistant</h1>
    <p><a href="/health">❤️ Health Check</a></p>
    <p><a href="/test-db">🗄️ Test Database</a></p>
    '''

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
        "version": "2.8",
        "database": {
            "type": f"PostgreSQL (psycopg{PSYCOPG_VERSION})" if USE_POSTGRES else "SQLite",
            "status": db_status,
            "user_count": user_count
        },
        "environment": {
            "python_version": sys.version,
            "postgres_available": POSTGRES_AVAILABLE,
            "database_url_set": bool(DATABASE_URL)
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
        
        return jsonify({
            "status": "success",
            "database_type": f"PostgreSQL (psycopg{PSYCOPG_VERSION})" if USE_POSTGRES else "SQLite",
            "test_profile": dict(profile) if USE_POSTGRES else {"first_name": profile[0], "location": profile[1], "onboarding_completed": bool(profile[2])},
            "test_history": [dict(h) if USE_POSTGRES else {"role": h[0], "content": h[1]} for h in history]
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
    logger.info(f"🚀 Starting Hey Alex SMS Assistant")
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
