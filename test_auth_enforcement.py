"""
Quick test to verify session creation requires authentication
"""
import requests
import json

def test_session_creation():
    """Test that creating a session requires authentication"""
    print("🧪 Testing session creation endpoint...")
    print("=" * 60)
    
    base_url = "http://localhost:80"
    
    try:
        # Try to create a session without authentication
        response = requests.post(
            f"{base_url}/api/sessions/create",
            json={},
            allow_redirects=False  # Don't follow redirects
        )
        
        print(f"Response Status: {response.status_code}")
        print(f"Response Headers: {dict(response.headers)}")
        
        if response.status_code == 401:
            print("✅ PASS: Session creation requires authentication (401 Unauthorized)")
            try:
                data = response.json()
                print(f"Response Body: {json.dumps(data, indent=2)}")
                if data.get('authenticated') == False:
                    print("✅ PASS: Response indicates authentication required")
                    return True
            except:
                print("⚠️  Response body is not JSON")
                return True
        elif response.status_code == 302:
            print("✅ PASS: Session creation redirects to login (302 Redirect)")
            print(f"Redirect Location: {response.headers.get('Location')}")
            return True
        elif response.status_code == 200:
            print("❌ FAIL: Session created without authentication!")
            print(f"Response: {response.text[:200]}")
            return False
        else:
            print(f"⚠️  Unexpected status code: {response.status_code}")
            print(f"Response: {response.text[:200]}")
            return False
            
    except requests.exceptions.ConnectionError:
        print("❌ ERROR: Could not connect to server")
        print("   Make sure the server is running: python webapp/app.py")
        return None
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return None

if __name__ == '__main__':
    print("=" * 60)
    print("Testing Authentication Enforcement")
    print("=" * 60)
    print("\n⚠️  Make sure the server is running first!")
    print("   Run in another terminal: python webapp/app.py\n")
    
    input("Press Enter when server is ready...")
    
    result = test_session_creation()
    
    print("\n" + "=" * 60)
    if result is True:
        print("🎉 SUCCESS: Authentication is properly enforced!")
        print("   The '+' button will require login.")
    elif result is False:
        print("⚠️  WARNING: Authentication may not be working correctly.")
        print("   Check the server logs for errors.")
    else:
        print("❌ Could not complete test. Check server status.")
    print("=" * 60)
