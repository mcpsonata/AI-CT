"""
Security headers middleware for Flask application
Adds important security headers to all responses
"""

from flask import Flask

def add_security_headers(app: Flask):
    """
    Add security headers to all Flask responses
    
    Args:
        app: Flask application instance
    """
    
    @app.after_request
    def set_security_headers(response):
        """Set security headers on all responses"""
        
        # Prevent clickjacking attacks
        response.headers['X-Frame-Options'] = 'DENY'
        
        # Prevent MIME type sniffing
        response.headers['X-Content-Type-Options'] = 'nosniff'
        
        # Enable XSS protection (legacy browsers)
        response.headers['X-XSS-Protection'] = '1; mode=block'
        
        # Strict Transport Security (HSTS)
        # Only enable if using HTTPS
        if app.config.get('PREFERRED_URL_SCHEME') == 'https':
            # max-age=31536000 (1 year), includeSubDomains
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        
        # Content Security Policy
        # Adjusted to allow Bootstrap, fonts, source maps, and Microsoft login
        csp_policy = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://code.jquery.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://cdn.jsdelivr.net https://login.microsoftonline.com https://*.cognitiveservices.azure.com https://*.ai.azure.com; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self' https://login.microsoftonline.com;"
        )
        response.headers['Content-Security-Policy'] = csp_policy
        
        # Referrer Policy
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        
        # Permissions Policy (formerly Feature Policy)
        response.headers['Permissions-Policy'] = (
            "geolocation=(), "
            "microphone=(), "
            "camera=(), "
            "payment=(), "
            "usb=(), "
            "magnetometer=(), "
            "gyroscope=(), "
            "accelerometer=()"
        )
        
        return response
    
    return app
