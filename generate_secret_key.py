"""
Generate secure SECRET_KEY for Flask application
Run this script to generate a secure random key for production use
"""

import secrets

def generate_secret_key():
    """Generate a cryptographically secure random key"""
    return secrets.token_hex(32)

if __name__ == '__main__':
    secret_key = generate_secret_key()
    
    print("\n" + "="*70)
    print("🔐 GENERATED SECURE SECRET_KEY FOR FLASK")
    print("="*70)
    print(f"\n{secret_key}\n")
    print("="*70)
    print("\n📋 INSTRUCTIONS:")
    print("\n1. Copy the key above")
    print("\n2. Set it in Azure Container Instance:")
    print(f"   az container create --secure-environment-variables SECRET_KEY={secret_key}")
    print("\n3. OR store in Azure Key Vault:")
    print(f"   az keyvault secret set --vault-name MCPKeyValut --name Flask-Secret-Key --value \"{secret_key}\"")
    print("\n⚠️  NEVER commit this key to Git or share it publicly!")
    print("="*70)
    print()
