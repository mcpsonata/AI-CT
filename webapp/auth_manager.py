"""
Session-Isolated MSAL Authentication Manager for Flask Application
Handles Microsoft authentication with proper session isolation
"""

import os
import logging
import msal
from flask import session, redirect, url_for, request
from functools import wraps
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class MSALAuthManager:
    """Manages MSAL authentication with session isolation"""
    
    # Microsoft tenant and client configuration
    TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
    CLIENT_ID = "1624d147-d626-4d7f-942d-bd8f58beeabb"
    AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
    SCOPES = ["user.read"]
        
    # Whitelist of allowed user emails (temporary solution until app roles are configured)
    ALLOWED_USERS = {
        "v-psanjay@microsoft.com",
        "v-aadithya@microsoft.com",
        "v-manjmc@microsoft.com",
        "v-aevin@microsoft.com",
        "ahughes@microsoft.com",
        "singhami@microsoft.com",
        "ankita.bhatnagar@microsoft.com",
        "asdi@microsoft.com",
        "Brittany.Lewis@microsoft.com",
        "carya@microsoft.com",
        "dasharma@microsoft.com",
        "devansharma@microsoft.com",
        "Dhawal.Bhatia@microsoft.com",
        "eddielee@microsoft.com",
        "Eric.Ligman@microsoft.com",
        "mullereric@microsoft.com",
        "erubi@microsoft.com",
        "gupadhyay@microsoft.com",
        "gregje@microsoft.com",
        "Hima.Yenigalla@microsoft.com",
        "jalliston@microsoft.com",
        "Jason.Yang@microsoft.com",
        "jeannieho@microsoft.com",
        "jillcha@microsoft.com",
        "jfunke@microsoft.com",
        "juliesam@microsoft.com",
        "kerlys@microsoft.com",
        "Krisztian.Sandor@microsoft.com",
        "madefant@microsoft.com",
        "markim@microsoft.com",
        "mtoomey@microsoft.com",
        "myuan@microsoft.com",
        "muralivelaga@microsoft.com",
        "Neeraj.Kumar@microsoft.com",
        "niteenmuley@microsoft.com",
        "nnardella@microsoft.com",
        "pschontzler@microsoft.com",
        "v-paadapala@microsoft.com",
        "rashenoy@microsoft.com",
        "rebeccao@microsoft.com",
        "v-saranyand@microsoft.com",
        "savvyh@microsoft.com",
        "Sean.Oliver@microsoft.com",
        "sharatp@microsoft.com",
        "sishetty@microsoft.com",
        "srkana@microsoft.com"
    }
    def __init__(self, app=None):
        """Initialize the auth manager"""
        self.app = app
        if app:
            self.init_app(app)
    
    def init_app(self, app):
        """Initialize with Flask app"""
        # Configure Flask session
        # SECURITY: SECRET_KEY must be set in production via environment variable
        secret_key = os.getenv('SECRET_KEY')
        
        if not secret_key:
            # Check if this is production environment
            flask_env = os.getenv('FLASK_ENV', 'production')
            if flask_env == 'production':
                # CRITICAL: Refuse to start in production without SECRET_KEY
                error_msg = (
                    "❌ SECURITY ERROR: SECRET_KEY environment variable is required in production!\n"
                    "Generate a secure key with: python -c \"import secrets; print(secrets.token_hex(32))\"\n"
                    "Then set it as an environment variable in Azure Container Instance."
                )
                logger.error(error_msg)
                raise ValueError(error_msg)
            else:
                # Development only - generate a random key per session (not persistent)
                import secrets
                secret_key = secrets.token_hex(32)
                logger.warning("⚠️ Using temporary SECRET_KEY for DEVELOPMENT only!")
        
        app.config['SECRET_KEY'] = secret_key
        app.config['SESSION_TYPE'] = 'filesystem'
        app.config['SESSION_PERMANENT'] = True
        app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # 1 hour
        
        # Determine environment once
        flask_env = os.getenv('FLASK_ENV', 'production')
        is_development = flask_env == 'development'
        
        # Use temp directory in production (more reliable in containers)
        if is_development:
            session_dir = os.path.join(os.path.dirname(__file__), 'flask_session')
        else:
            import tempfile
            session_dir = os.path.join(tempfile.gettempdir(), 'flask_session')
        
        app.config['SESSION_FILE_DIR'] = session_dir
        
        # Session cookie security - adapt based on environment
        # In production with Azure Front Door, the external connection is HTTPS
        # but internal (Front Door -> ACI) is HTTP
        # ProxyFix middleware ensures Flask sees the correct HTTPS scheme
        
        app.config['SESSION_COOKIE_SECURE'] = not is_development  # Require HTTPS in production
        app.config['SESSION_COOKIE_HTTPONLY'] = True  # Prevent JavaScript access (XSS protection)
        app.config['SESSION_COOKIE_SAMESITE'] = 'None' if not is_development else 'Lax'  # None required for OAuth redirects in production
        
        # Set cookie domain only in production with Azure Front Door
        # In development, leave it unset so it works with localhost
        if not is_development:
            frontend_domain = os.getenv('FRONTEND_DOMAIN')  # e.g., 'myapp.azurefd.net'
            if frontend_domain:
                app.config['SESSION_COOKIE_DOMAIN'] = frontend_domain
                logger.info(f"✅ Session cookie domain set to: {frontend_domain}")
            else:
                logger.warning("⚠️ FRONTEND_DOMAIN not set. Consider setting it for proper cookie scoping.")
        else:
            # Development mode - no cookie domain (works with localhost)
            logger.info("✅ Development mode - session cookies will work with localhost")
        
        # Create session directory - don't let this crash the app
        session_dir = app.config['SESSION_FILE_DIR']
        try:
            os.makedirs(session_dir, exist_ok=True)
            logger.info(f"✅ Session directory ready: {session_dir}")
        except Exception as e:
            logger.warning(f"⚠️ Could not create session directory {session_dir}: {e}")
            # Try fallback to /tmp
            try:
                fallback_dir = '/tmp/flask_session'
                os.makedirs(fallback_dir, exist_ok=True)
                app.config['SESSION_FILE_DIR'] = fallback_dir
                logger.info(f"✅ Using fallback session directory: {fallback_dir}")
            except Exception as e2:
                logger.error(f"❌ Fallback also failed: {e2}. Sessions may not work properly.")
        
        logger.info("✅ MSAL Auth Manager initialized")
    
    def get_msal_app(self) -> msal.PublicClientApplication:
        """Get MSAL application instance"""
        return msal.PublicClientApplication(
            self.CLIENT_ID,
            authority=self.AUTHORITY
        )
    
    def get_auth_url(self, redirect_uri: str) -> str:
        """
        Get authorization URL for interactive login
        
        Args:
            redirect_uri: URL to redirect after authentication
            
        Returns:
            Authorization URL
        """
        msal_app = self.get_msal_app()
        
        # Generate auth URL with prompt=select_account to force account selection
        auth_url = msal_app.get_authorization_request_url(
            scopes=self.SCOPES,
            redirect_uri=redirect_uri,
            prompt='select_account'  # Force account selection every time
        )
        
        return auth_url
    
    def acquire_token_by_auth_code(self, auth_code: str, redirect_uri: str) -> Optional[Dict[str, Any]]:
        """
        Acquire token using authorization code
        
        Args:
            auth_code: Authorization code from callback
            redirect_uri: Redirect URI used in initial request
            
        Returns:
            Token response dict or None if failed
        """
        msal_app = self.get_msal_app()
        
        try:
            result = msal_app.acquire_token_by_authorization_code(
                code=auth_code,
                scopes=self.SCOPES,
                redirect_uri=redirect_uri
            )
            
            if "access_token" in result:
                logger.info("✅ Successfully acquired token by auth code")
                return result
            else:
                error = result.get("error", "Unknown error")
                error_desc = result.get("error_description", "No description")
                logger.error(f"❌ Failed to acquire token: {error} - {error_desc}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Exception acquiring token: {e}")
            return None
    
    def acquire_token_silent(self) -> Optional[Dict[str, Any]]:
        """
        Try to acquire token silently from cache
        
        Returns:
            Token response dict or None if no cached token
        """
        if 'user' not in session:
            return None
        
        msal_app = self.get_msal_app()
        accounts = msal_app.get_accounts()
        
        if not accounts:
            logger.info("No accounts found in cache")
            return None
        
        # Use the account stored in session
        user_account = session.get('user')
        matching_account = None
        
        for account in accounts:
            if account.get('username') == user_account.get('username'):
                matching_account = account
                break
        
        if not matching_account:
            logger.warning(f"No matching account found for {user_account.get('username')}")
            return None
        
        try:
            result = msal_app.acquire_token_silent(
                scopes=self.SCOPES,
                account=matching_account
            )
            
            if result and "access_token" in result:
                logger.info(f"✅ Token acquired silently for {user_account.get('username')}")
                return result
            else:
                logger.info("No cached token available, need interactive login")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error acquiring token silently: {e}")
            return None
        
    def validate_user_access(self, token_response: Dict[str, Any]) -> bool:
        """
        Validate that the user is in the allowed users whitelist
        
        Args:
            token_response: Token response from MSAL
            
        Returns:
            True if user is allowed, False otherwise
        """
        try:
            id_token_claims = token_response.get("id_token_claims", {})
            username = id_token_claims.get('preferred_username', id_token_claims.get('email', 'Unknown'))
            
            logger.info(f"🔍 Validating access for user: {username}")
            
            if username.lower() in {user.lower() for user in self.ALLOWED_USERS}:
                logger.info(f"✅ User {username} is in the allowed users list")
                return True
            else:
                logger.error(f"🚨 SECURITY: Access denied - user not in whitelist")
                logger.error(f"   User: {username}")
                logger.error(f"   Allowed users: {', '.join(self.ALLOWED_USERS)}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error validating user access: {e}")
            # Fail closed - deny access on error
            return False
    
    
    def store_user_session(self, token_response: Dict[str, Any]):
        """
        Store user information in Flask session
        
        Args:
            token_response: Token response from MSAL
        """
        # Extract user info from token response
        id_token_claims = token_response.get("id_token_claims", {})
        
        user_info = {
            'username': id_token_claims.get('preferred_username', id_token_claims.get('email', 'Unknown')),
            'name': id_token_claims.get('name', 'Unknown User'),
            'oid': id_token_claims.get('oid', ''),  # Object ID
            'tid': id_token_claims.get('tid', '')   # Tenant ID
        }
        
        session['user'] = user_info
        session['authenticated'] = True
        session['access_token'] = token_response.get('access_token')
        
        logger.info(f"✅ User session stored: {user_info['username']}")
    
    def clear_session(self):
        """Clear user session"""
        session.clear()
        logger.info("🔄 User session cleared")
    
    def is_authenticated(self) -> bool:
        """
        Check if current session is authenticated
        
        Returns:
            True if authenticated, False otherwise
        """
        return session.get('authenticated', False) and 'user' in session
    
    def get_user_info(self) -> Optional[Dict[str, str]]:
        """
        Get current user information
        
        Returns:
            User info dict or None if not authenticated
        """
        return session.get('user')


def require_auth(f):
    """
    Decorator to require authentication for routes
    Returns JSON error for API calls, redirects to login for page requests
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from flask import jsonify, current_app
        
        # Check if user is authenticated by checking session directly
        is_authenticated = session.get('authenticated', False) and 'user' in session
        
        if not is_authenticated:
            logger.warning(f"⚠️ Unauthorized access attempt to {request.path}")
            logger.debug(f"🔍 Session state: authenticated={session.get('authenticated')}, has_user={'user' in session}")
            logger.debug(f"🔍 Session cookie: {request.cookies.get('session')}")
            
            # Check if this is an API request (based on path or accept header)
            is_api_request = (
                request.path.startswith('/api/') or 
                request.accept_mimetypes.accept_json and 
                not request.accept_mimetypes.accept_html
            )
            
            if is_api_request:
                # Return JSON error for API requests
                return jsonify({
                    'error': 'Authentication required',
                    'authenticated': False,
                    'redirect_to': '/login_page'
                }), 401
            else:
                # Redirect to login page for regular page requests
                return redirect(url_for('login_page'))
        
        return f(*args, **kwargs)
    
    return decorated_function
