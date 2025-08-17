from flask import Flask, jsonify
import os
import logging
import sys

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    """Home page with links to debug endpoints"""
    return '''
    <h1>Hey Alex Debug Server</h1>
    <p><a href="/debug">🔍 Full Debug Info</a></p>
    <p><a href="/health">❤️ Health Check</a></p>
    <p><a href="/test-postgres">🗄️ Test PostgreSQL</a></p>
    '''

@app.route('/debug', methods=['GET'])
def debug():
    """Debug all imports and environment"""
    debug_info = {
        "python_version": sys.version,
        "environment_variables": {},
        "import_tests": {},
        "installed_packages": []
    }
    
    # Environment variables
    env_vars = ["DATABASE_URL", "RENDER", "RENDER_SERVICE_NAME", "PORT"]
    for var in env_vars:
        value = os.getenv(var)
        if var == "DATABASE_URL" and value:
            debug_info["environment_variables"][var] = value[:30] + "..."
        else:
            debug_info["environment_variables"][var] = value
    
    # Test imports
    imports_to_test = [
        "sqlite3",
        "psycopg2",
        "psycopg2.extras", 
        "flask",
        "requests"
    ]
    
    for module_name in imports_to_test:
        try:
            module = __import__(module_name)
            debug_info["import_tests"][module_name] = {
                "status": "✅ SUCCESS",
                "file": getattr(module, '__file__', 'unknown'),
                "version": getattr(module, '__version__', 'unknown')
            }
        except ImportError as e:
            debug_info["import_tests"][module_name] = {
                "status": f"❌ FAILED: {str(e)}"
            }
    
    # Get installed packages
    try:
        import pkg_resources
        installed_packages = [f"{d.project_name}=={d.version}" for d in pkg_resources.working_set]
        postgres_related = [p for p in installed_packages if 'psycopg' in p.lower() or 'postgres' in p.lower()]
        debug_info["postgres_packages"] = postgres_related
        debug_info["total_packages"] = len(installed_packages)
        
        # Show first 20 packages for debugging
        debug_info["sample_packages"] = sorted(installed_packages)[:20]
        
    except Exception as e:
        debug_info["package_error"] = str(e)
    
    return jsonify(debug_info)

@app.route('/test-postgres', methods=['GET'])
def test_postgres():
    """Test PostgreSQL connection specifically"""
    result = {"test": "PostgreSQL Connection"}
    
    try:
        import psycopg2
        result["psycopg2_import"] = "✅ SUCCESS"
        result["psycopg2_version"] = getattr(psycopg2, '__version__', 'unknown')
        result["psycopg2_file"] = getattr(psycopg2, '__file__', 'unknown')
        
        # Test DATABASE_URL
        database_url = os.getenv("DATABASE_URL")
        if database_url:
            result["database_url"] = "✅ SET"
            
            try:
                # Test connection
                conn = psycopg2.connect(database_url)
                result["connection"] = "✅ SUCCESS"
                
                # Test query
                with conn.cursor() as cursor:
                    cursor.execute("SELECT version()")
                    version = cursor.fetchone()[0]
                    result["postgres_version"] = version[:100] + "..." if len(version) > 100 else version
                
                # Test table creation
                with conn.cursor() as cursor:
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS test_table (
                            id SERIAL PRIMARY KEY,
                            name VARCHAR(100),
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    conn.commit()
                    result["table_creation"] = "✅ SUCCESS"
                
                # Test data insertion
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO test_table (name) VALUES (%s) RETURNING id
                    """, ("test_user",))
                    new_id = cursor.fetchone()[0]
                    conn.commit()
                    result["data_insertion"] = f"✅ SUCCESS (ID: {new_id})"
                
                # Test data retrieval
                with conn.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM test_table")
                    count = cursor.fetchone()[0]
                    result["data_retrieval"] = f"✅ SUCCESS ({count} records)"
                
                conn.close()
                
            except Exception as e:
                result["connection_error"] = f"❌ {str(e)}"
        else:
            result["database_url"] = "❌ NOT SET"
            
    except ImportError as e:
        result["psycopg2_import"] = f"❌ FAILED: {str(e)}"
        
        # Try to diagnose the import issue
        try:
            import sys
            result["python_path"] = sys.path[:5]  # First 5 paths
            
            # Check if the package files exist
            try:
                import pkg_resources
                dist = pkg_resources.get_distribution('psycopg2-binary')
                result["psycopg2_binary_location"] = dist.location
            except:
                pass
                
        except Exception as debug_error:
            result["debug_error"] = str(debug_error)
    
    return jsonify(result)

@app.route('/health', methods=['GET'])
def health():
    """Simple health check"""
    return jsonify({"status": "healthy", "message": "Hey Alex Debug Server is running"})

if __name__ == "__main__":
    logger.info("🚀 Starting Hey Alex Debug Server")
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
