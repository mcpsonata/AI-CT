"""
Quick test script to verify MSAL authentication setup
Run this to test the authentication flow without starting the full app
"""

import sys
import os

# Add webapp directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'webapp'))

def test_auth_manager():
    """Test that auth manager can be imported and initialized"""
    print("🧪 Testing MSAL Auth Manager...")
    
    try:
        from webapp.auth_manager import MSALAuthManager
        print("✅ Successfully imported MSALAuthManager")
        
        # Test initialization
        auth_manager = MSALAuthManager()
        print(f"✅ Auth manager initialized")
        print(f"   Tenant ID: {auth_manager.TENANT_ID}")
        print(f"   Client ID: {auth_manager.CLIENT_ID}")
        print(f"   Authority: {auth_manager.AUTHORITY}")
        print(f"   Scopes: {auth_manager.SCOPES}")
        
        # Test MSAL app creation
        msal_app = auth_manager.get_msal_app()
        print(f"✅ MSAL application created successfully")
        print(f"   Client ID: {msal_app.client_id}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_login_template():
    """Test that login template exists"""
    print("\n🧪 Testing Login Template...")
    
    template_path = os.path.join(os.path.dirname(__file__), 'webapp', 'templates', 'login.html')
    
    if os.path.exists(template_path):
        print(f"✅ Login template found: {template_path}")
        
        # Read and check content
        with open(template_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        if 'Sign in with Microsoft' in content:
            print("✅ Login button found in template")
        else:
            print("⚠️ Login button not found in template")
            
        return True
    else:
        print(f"❌ Login template not found: {template_path}")
        return False

def test_flask_imports():
    """Test that Flask can be imported"""
    print("\n🧪 Testing Flask Imports...")
    
    try:
        from flask import Flask, session
        print("✅ Flask imported successfully")
        
        import msal
        print(f"✅ MSAL imported successfully (version: {msal.__version__})")
        
        return True
        
    except Exception as e:
        print(f"❌ Import error: {e}")
        return False

if __name__ == '__main__':
    print("=" * 60)
    print("MSAL Authentication Setup Test")
    print("=" * 60)
    
    results = []
    
    # Run tests
    results.append(("Flask Imports", test_flask_imports()))
    results.append(("Auth Manager", test_auth_manager()))
    results.append(("Login Template", test_login_template()))
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {test_name}")
    
    all_passed = all(result for _, result in results)
    
    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 All tests passed! Authentication system is ready.")
        print("\nNext steps:")
        print("1. Start the application: python webapp/app.py")
        print("2. Navigate to: http://localhost:80/")
        print("3. You should see the login page")
    else:
        print("⚠️ Some tests failed. Please review the errors above.")
    print("=" * 60)
