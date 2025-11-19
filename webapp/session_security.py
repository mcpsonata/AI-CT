"""
Session Security Middleware for AICT Web Application
Provides session validation, isolation, and security features.
"""

import hashlib
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify, g
import logging

logger = logging.getLogger(__name__)

class SessionSecurityManager:
    """Enhanced session security manager for AICT"""
    
    def __init__(self):
        self.session_registry = {}  # Track active sessions with metadata
        self.session_tokens = {}    # Map session_id to security token
        self.rate_limits = {}       # Rate limiting per session
        
    def generate_secure_session_id(self, user_identifier: str = None) -> str:
        """Generate a cryptographically secure session ID"""
        timestamp = str(int(time.time()))
        random_bytes = secrets.token_hex(16)
        
        # Include user identifier if provided for better tracking
        if user_identifier:
            base_string = f"{user_identifier}_{timestamp}_{random_bytes}"
        else:
            base_string = f"anonymous_{timestamp}_{random_bytes}"
        
        # Create hash for session ID
        session_id = hashlib.sha256(base_string.encode()).hexdigest()[:32]
        
        # Register session with metadata
        self.session_registry[session_id] = {
            "created": datetime.now(),
            "last_active": datetime.now(),
            "user_identifier": user_identifier or "anonymous",
            "request_count": 0,
            "ip_address": request.remote_addr if request else "unknown",
            "user_agent": request.headers.get('User-Agent', 'unknown') if request else "unknown"
        }
        
        # Generate security token for this session
        self.session_tokens[session_id] = secrets.token_hex(32)
        
        logger.info(f"Generated secure session ID: {session_id} for user: {user_identifier or 'anonymous'}")
        return session_id
    
    def create_session(self, session_id: str, user_identifier: str = None) -> bool:
        """Create a new session with the provided session ID"""
        try:
            if session_id in self.session_registry:
                logger.info(f"Session {session_id} already exists, updating it")
            
            # Create session data
            session_data = {
                "created": datetime.now(),
                "last_active": datetime.now(),
                "user_identifier": user_identifier or f"user_{session_id[-8:]}",
                "request_count": 0,
                "ip_address": None,  # Will be set by request context if available
                "user_agent": None   # Will be set by request context if available
            }
            
            # Store session
            self.session_registry[session_id] = session_data
            
            # Initialize rate limiting
            self.rate_limits[session_id] = []
            
            logger.info(f"Created session: {session_id} for user: {user_identifier or 'anonymous'}")
            return True
            
        except Exception as e:
            logger.error(f"Error creating session {session_id}: {e}")
            return False
    
    def validate_session(self, session_id: str) -> bool:
        """Validate if a session ID is legitimate and active"""
        if not session_id or session_id not in self.session_registry:
            logger.warning(f"Invalid session ID attempted: {session_id}")
            return False
        
        session_data = self.session_registry[session_id]
        
        # Check if session has expired (24 hours)
        if session_data["last_active"] < datetime.now() - timedelta(hours=24):
            logger.info(f"Session expired: {session_id}")
            self.cleanup_session(session_id)
            return False
        
        # Update last active time
        session_data["last_active"] = datetime.now()
        session_data["request_count"] += 1
        
        return True
    
    def check_rate_limit(self, session_id: str, max_requests: int = 100) -> bool:
        """Check if session is within rate limits (requests per minute)"""
        current_time = time.time()
        
        if session_id not in self.rate_limits:
            self.rate_limits[session_id] = []
        
        # Clean old requests (older than 1 minute)
        self.rate_limits[session_id] = [
            req_time for req_time in self.rate_limits[session_id] 
            if current_time - req_time < 60
        ]
        
        # Check if under limit
        if len(self.rate_limits[session_id]) >= max_requests:
            logger.warning(f"Rate limit exceeded for session: {session_id}")
            return False
        
        # Add current request
        self.rate_limits[session_id].append(current_time)
        return True
    
    def cleanup_session(self, session_id: str):
        """Clean up a specific session's data"""
        if session_id in self.session_registry:
            del self.session_registry[session_id]
        if session_id in self.session_tokens:
            del self.session_tokens[session_id]
        if session_id in self.rate_limits:
            del self.rate_limits[session_id]
        
        logger.info(f"Cleaned up session: {session_id}")
    
    def cleanup_expired_sessions(self):
        """Clean up all expired sessions"""
        current_time = datetime.now()
        expired_sessions = []
        
        for session_id, session_data in self.session_registry.items():
            if session_data["last_active"] < current_time - timedelta(hours=24):
                expired_sessions.append(session_id)
        
        for session_id in expired_sessions:
            self.cleanup_session(session_id)
        
        logger.info(f"Cleaned up {len(expired_sessions)} expired sessions")
        return len(expired_sessions)
    
    def get_session_info(self, session_id: str) -> dict:
        """Get session information for monitoring"""
        if session_id not in self.session_registry:
            return None
        
        session_data = self.session_registry[session_id].copy()
        session_data["session_id"] = session_id
        session_data["active_duration"] = str(datetime.now() - session_data["created"])
        
        return session_data
    
    def get_all_sessions(self) -> list:
        """Get information about all active sessions (admin function)"""
        sessions = []
        for session_id in self.session_registry:
            sessions.append(self.get_session_info(session_id))
        return sessions

# Global instance
session_security = SessionSecurityManager()

def require_valid_session(f):
    """Decorator to require valid session for API endpoints"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Extract session_id from URL path or request body
        session_id = None
        
        # Try to get from URL path parameter
        if 'session_id' in kwargs:
            session_id = kwargs['session_id']
        
        # Try to get from request JSON body
        if not session_id and request.is_json:
            data = request.get_json()
            if data and 'session_id' in data:
                session_id = data['session_id']
        
        # Try to get from query parameters
        if not session_id:
            session_id = request.args.get('session_id')
        
        if not session_id:
            return jsonify({
                "error": "Session ID required",
                "code": "MISSING_SESSION_ID"
            }), 400
        
        # Validate session
        if not session_security.validate_session(session_id):
            return jsonify({
                "error": "Invalid or expired session",
                "code": "INVALID_SESSION"
            }), 401
        
        # Check rate limiting
        if not session_security.check_rate_limit(session_id):
            return jsonify({
                "error": "Rate limit exceeded",
                "code": "RATE_LIMIT_EXCEEDED"
            }), 429
        
        # Store session info in Flask's g object for use in the request
        g.session_id = session_id
        g.session_info = session_security.get_session_info(session_id)
        
        return f(*args, **kwargs)
    
    return decorated_function

def session_isolation_check(f):
    """Decorator to ensure session data isolation"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Ensure session_id is available
        if not hasattr(g, 'session_id'):
            return jsonify({
                "error": "Session isolation check failed",
                "code": "SESSION_ISOLATION_FAILED"
            }), 500
        
        # Log session activity for security monitoring
        logger.info(f"Session {g.session_id} accessing {request.endpoint}")
        
        return f(*args, **kwargs)
    
    return decorated_function

def get_isolated_session_data(global_data_dict: dict, session_id: str, default_factory=dict):
    """Helper function to get session-isolated data from global dictionaries"""
    if session_id not in global_data_dict:
        global_data_dict[session_id] = default_factory()
    return global_data_dict[session_id]

def validate_session_access_to_resource(session_id: str, resource_type: str, resource_id: str) -> bool:
    """Validate that a session has access to a specific resource"""
    # This is a placeholder for more advanced access control
    # In a production system, you might check:
    # - User permissions for the resource
    # - Resource ownership
    # - Role-based access control
    
    logger.info(f"Session {session_id} requesting access to {resource_type}:{resource_id}")
    return True  # For now, allow access if session is valid

# Example usage decorators for AICT endpoints:

def secure_chat_endpoint(f):
    """Composite decorator for chat endpoints with full security"""
    @wraps(f)
    @require_valid_session
    @session_isolation_check
    def decorated_function(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated_function

def secure_data_endpoint(f):
    """Composite decorator for data access endpoints"""
    @wraps(f)
    @require_valid_session
    @session_isolation_check
    def decorated_function(*args, **kwargs):
        # Additional data access validation could go here
        return f(*args, **kwargs)
    return decorated_function

# Session security monitoring functions

def log_security_event(session_id: str, event_type: str, details: str):
    """Log security-related events for monitoring"""
    logger.warning(f"SECURITY EVENT: Session {session_id} - {event_type}: {details}")

def detect_suspicious_activity(session_id: str) -> bool:
    """Detect potentially suspicious session activity"""
    session_info = session_security.get_session_info(session_id)
    if not session_info:
        return False
    
    # Check for suspicious patterns
    suspicious_indicators = []
    
    # High request rate
    if session_info["request_count"] > 1000:
        suspicious_indicators.append("high_request_count")
    
    # Session duration too long
    created_time = session_info["created"]
    if datetime.now() - created_time > timedelta(hours=12):
        suspicious_indicators.append("long_session_duration")
    
    if suspicious_indicators:
        log_security_event(session_id, "suspicious_activity", 
                          f"Indicators: {', '.join(suspicious_indicators)}")
        return True
    
    return False

# Background cleanup task (should be called periodically)
def periodic_security_cleanup():
    """Periodic cleanup task for session security"""
    try:
        expired_count = session_security.cleanup_expired_sessions()
        logger.info(f"Security cleanup completed. Removed {expired_count} expired sessions.")
        
        # Check for suspicious activity across all sessions
        suspicious_sessions = []
        for session_id in session_security.session_registry:
            if detect_suspicious_activity(session_id):
                suspicious_sessions.append(session_id)
        
        if suspicious_sessions:
            logger.warning(f"Detected suspicious activity in {len(suspicious_sessions)} sessions")
            
    except Exception as e:
        logger.error(f"Error during security cleanup: {e}")