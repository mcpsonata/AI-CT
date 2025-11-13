"""
Authentication Manager for Azure services
Handles Azure token management and authentication for Power BI and Fabric operations.
"""

import os
import threading
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient
import openai
import subprocess
import requests

# Load environment
load_dotenv()

# Setup logger
logger = logging.getLogger(__name__)


class AuthenticationManager:
    """Centralized authentication manager for Azure tokens - Singleton Pattern"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """Ensure only one instance exists (thread-safe singleton)"""
        if cls._instance is None:
            with cls._lock:
                # Double-check locking pattern
                if cls._instance is None:
                    cls._instance = super(AuthenticationManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, auth_script_path: str = None):
        # Set default path relative to this module's location
        if auth_script_path is None:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            auth_script_path = os.path.join(os.path.dirname(current_dir), "auth.ps1")
        self.skip_create_tokens = os.getenv("Skip_Create_Tokens", "False").lower() == "true"
        self.auth_script_path = auth_script_path
        self._initialized = True
        self.tokens_cache = {}  # Initialize empty cache to avoid circular dependency
        self.token_expiry_date_time = None
        self.logger = logging.getLogger(__name__)
        
        # Set up logging
        logging.basicConfig(level=logging.INFO)
        
        # Verify the auth script exists
        if not os.path.exists(auth_script_path):
            raise FileNotFoundError(f"Authentication script not found: {auth_script_path}")
        
    def get_client_secret(self, secretName: str) -> str:
        """Get the client secret for Azure authentication."""
        with self._lock:
            
            credential = AzureCliCredential()
            vault_url = os.getenv("Key_Vault_URI")
            self._keyvault_client = SecretClient(vault_url=vault_url, credential=credential)
            retrieved_secret = self._keyvault_client.get_secret(secretName)
            return retrieved_secret.value

    def get_azure_token(self, secret_name: str):
        try:
            self.logger.info(f"Retrieving token for secret: {secret_name}")
            
            # Resolve the full path to the auth script
            script_path = os.path.abspath(self.auth_script_path)
            if not os.path.exists(script_path):
                self.logger.error(f"Auth script not found at: {script_path}")
                return None

            # Try PowerShell Core first
            powershell_commands = ["pwsh"]
            result = None
            
            for ps_cmd in powershell_commands:
                try:
                    cmd = [ps_cmd, "-File", script_path, "-secretName", secret_name]
                    self.logger.debug(f"Executing command: {' '.join(cmd)}")
                    
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=30,  # 30 second timeout
                        check=False  # Don't raise exception on non-zero exit
                    )
                    break  # If successful, break out of the loop
                    
                except FileNotFoundError:
                    self.logger.debug(f"{ps_cmd} not found, trying next...")
                    continue
            
            if result is None:
                self.logger.error("Neither pwsh nor powershell executable found")
                return None
            
            # Log the full output for debugging
            self.logger.debug(f"PowerShell stdout: {result.stdout}")
            self.logger.debug(f"PowerShell stderr: {result.stderr}")
            self.logger.debug(f"PowerShell return code: {result.returncode}")
            
            if result.returncode != 0:
                self.logger.error(f"PowerShell script failed with return code {result.returncode}")
                self.logger.error(f"Error output: {result.stderr}")
                return None
            
            # Parse the output to extract the token
            output_lines = result.stdout.strip().split('\n')
            
            # Look for the token in multiple ways
            token = None
            
            # Method 1: Look for "Token:" prefix
            for line in output_lines:
                line = line.strip()
                if "Token:" in line:
                    token = line.split("Token:", 1)[1].strip()
                    break
            
            # Method 2: If no "Token:" found, look for the last non-empty line that doesn't look like a status message
            if not token:
                status_prefixes = [
                    "Attempting", "Key Vault", "Retrieving", "Secret", 
                    "Using", "Azure CLI", "Managed Identity", "Please",
                    "Alternative format", "successfully", "failed"
                ]
                
                for line in reversed(output_lines):
                    line = line.strip()
                    if line and not any(line.startswith(prefix) for prefix in status_prefixes):
                        # Check if it looks like a token (long string, possibly with special characters)
                        if len(line) > 20:  # Tokens are usually longer than 20 characters
                            token = line
                            break
            
            # Method 3: If still no token, try to find any line that looks like a base64 encoded string or JWT
            if not token:
                import re
                for line in output_lines:
                    line = line.strip()
                    # Look for JWT pattern (starts with eyJ) or long base64-like strings
                    if re.match(r'^[A-Za-z0-9+/=._-]{50,}$', line) or line.startswith('eyJ'):
                        token = line
                        break
            
            if token:
                self.logger.info(f"Successfully retrieved token for {secret_name}")
                # Don't log the actual token for security reasons
                self.logger.debug(f"Token length: {len(token)} characters")
                return token
            else:
                self.logger.error(f"Could not extract token from PowerShell output")
                self.logger.error(f"Full output: {result.stdout}")
                self.logger.error(f"Full stderr: {result.stderr}")
                return None
                
        except subprocess.TimeoutExpired:
            self.logger.error(f"PowerShell script timed out while retrieving {secret_name}")
            return None
        except Exception as e:
            self.logger.error(f"Error retrieving token for {secret_name}: {str(e)}")
            return None
        
    def get_functionapp_key(self) -> str:
        """Get Function App key from Key Vault."""
        if os.getenv("Use_Local_Auth") == "1":
            return self.get_client_secret(os.getenv("Function_App_Secret_Name"))
        else:
            return self.get_azure_token(os.getenv("Function_App_Secret_Name"))
    
    def is_token_valid(self):
        if self.token_expiry_date_time is None:
            return False  
        elif self.token_expiry_date_time <= datetime.now():
            return False  # Token has expired
        else:
            return True  # Token is still valid

    def create_access_tokens(self):
        """Create access tokens for Power BI and Fabric with retry mechanism."""
        base_url = "https://bit-functionapp-hubhfwabhpdbehg4.westus-01.azurewebsites.net/api/SPN_FIC_REST_API_Auth" 
        url_with_key = f"{base_url}?code={self.get_functionapp_key()}"
        
        # Add timeout to prevent hanging
        function_response = requests.get(url_with_key, timeout=30)
        
        # Create token expiry date time (current time + 12 hours)
        current_date_time = datetime.now()
        self.token_expiry_date_time = current_date_time + timedelta(hours=12)
        
        # Clear existing cache to force fresh token retrieval
        self.tokens_cache.clear()
        
        if function_response.status_code == 200:
            logger.info("Successfully retrieved access tokens from Function App")
            return "Successfully retrieved access tokens from Function App"
        else:
            logger.error(f"Failed to retrieve access tokens: {function_response.status_code} - {function_response.text}")
            return f"Failed to retrieve access tokens: {function_response.status_code} - {function_response.text}"
                        
                       
    def get_powerbi_access_token(self) -> str:
        logger.info("🔑 [TOKEN-STEP 1/5] get_powerbi_access_token() called")
        
        # First check: If token is not valid and Skip_Create_Tokens == false, create new tokens
        if not self.is_token_valid() and not self.skip_create_tokens:
            logger.info("🔑 [TOKEN-STEP 2/5] Token not valid, creating new access tokens...")
            token_create_start = datetime.now()
            function_app_response = self.create_access_tokens()
            token_create_time = (datetime.now() - token_create_start).total_seconds()
            logger.info(f"🔑 [TOKEN-STEP 2/5] Token creation completed in {token_create_time:.2f}s: {function_app_response}")
        else:
            logger.info(f"🔑 [TOKEN-STEP 2/5] Token validation - Valid: {self.is_token_valid()}, Skip_Create: {self.skip_create_tokens}")
        
        # Check if we have a cached token and it's valid
        if "PowerBI" in self.tokens_cache and self.is_token_valid():
            logger.info("✅ [TOKEN-STEP 3/5] Using cached PowerBI token (valid)")
            cached_token = self.tokens_cache.get("PowerBI")
            logger.info(f"✅ [TOKEN-STEP 5/5] Returning cached token (length: {len(cached_token) if cached_token else 0})")
            return cached_token
        
        # If cache is empty, directly get the token
        logger.info("🔑 [TOKEN-STEP 3/5] No cached token, retrieving fresh token...")
        if "PowerBI" not in self.tokens_cache:
            use_local_auth = os.getenv("Use_Local_Auth") == "1"
            logger.info(f"🔑 [TOKEN-STEP 4/5] Use_Local_Auth={use_local_auth}")
            
            token_retrieval_start = datetime.now()
            if use_local_auth:
                secret_name = os.getenv("Power_BI_Secret_Name")
                logger.info(f"🔑 [TOKEN-STEP 4/5] Calling get_client_secret('{secret_name}')...")
                token = self.get_client_secret(secret_name)
            else:
                secret_name = os.getenv("Power_BI_Secret_Name")
                logger.info(f"🔑 [TOKEN-STEP 4/5] Calling get_azure_token('{secret_name}')...")
                token = self.get_azure_token(secret_name)
            
            token_retrieval_time = (datetime.now() - token_retrieval_start).total_seconds()
            logger.info(f"🔑 [TOKEN-STEP 4/5] Token retrieval completed in {token_retrieval_time:.2f}s")
            
            # Cache the token
            if token:
                logger.info(f"✅ [TOKEN-STEP 4/5] Token retrieved successfully (length: {len(token)}), caching...")
                self.tokens_cache["PowerBI"] = token
                if self.token_expiry_date_time is None:
                    self.token_expiry_date_time = datetime.now() + timedelta(hours=12)
                    logger.info(f"✅ Token expiry set to: {self.token_expiry_date_time}")
            else:
                logger.error("❌ [TOKEN-STEP 4/5] Token retrieval returned None or empty!")
        
        final_token = self.tokens_cache.get("PowerBI")
        logger.info(f"🔑 [TOKEN-STEP 5/5] Returning token (exists: {final_token is not None}, length: {len(final_token) if final_token else 0})")
        return final_token
            
        
    def get_fabric_access_token(self) -> str:
        # First check: If token is not valid and Skip_Create_Tokens == false, create new tokens
        if not self.is_token_valid() and not self.skip_create_tokens:
            logger.info("Creating new access tokens.")
            function_app_response = self.create_access_tokens()
            logger.info(function_app_response)
        
        # Check if we have a cached token and it's valid
        if "Fabric" in self.tokens_cache and self.is_token_valid():
            return self.tokens_cache.get("Fabric")
        
        # If cache is empty, directly get the token
        if "Fabric" not in self.tokens_cache:
            if os.getenv("Use_Local_Auth") == "1":
                token = self.get_client_secret(os.getenv("Fabric_Secret_Name"))
            else:
                token = self.get_azure_token(os.getenv("Fabric_Secret_Name"))
            
            # Cache the token
            if token:
                self.tokens_cache["Fabric"] = token
                if self.token_expiry_date_time is None:
                    self.token_expiry_date_time = datetime.now() + timedelta(hours=12)
        
        return self.tokens_cache.get("Fabric")

    def get_openai_access_token(self) -> str:
        # First check: If token is not valid and Skip_Create_Tokens == false, create new tokens
        if not self.is_token_valid() and not self.skip_create_tokens:
            logger.info("Creating new access tokens.")
            function_app_response = self.create_access_tokens()
            logger.info(function_app_response)
        
        # Check if we have a cached token and it's valid
        if "OpenAI" in self.tokens_cache and self.is_token_valid():
            return self.tokens_cache.get("OpenAI")
        
        # If cache is empty, directly get the token
        if "OpenAI" not in self.tokens_cache:
            if os.getenv("Use_Local_Auth") == "1":
                token = self.get_client_secret(os.getenv("Cognitive_Service_Secret_Name"))
            else:
                token = self.get_azure_token(os.getenv("Cognitive_Service_Secret_Name"))
            
            # Cache the token
            if token:
                self.tokens_cache["OpenAI"] = token
                if self.token_expiry_date_time is None:
                    self.token_expiry_date_time = datetime.now() + timedelta(hours=12)
        
        return self.tokens_cache.get("OpenAI")

    def get_openai_client(self) -> openai.AzureOpenAI:
        """Get initialized Azure OpenAI client with API key authentication."""
        return openai.AzureOpenAI(
            azure_ad_token_provider=lambda: self.get_openai_access_token(),
            azure_endpoint=os.getenv("PROJECT_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION")
        )
    
    @classmethod
    def get_instance(cls) -> 'AuthenticationManager':
        """Get the singleton instance of AuthenticationManager"""
        return cls()
    
    def get_status(self) -> dict:
        """Get information about the authentication manager (for debugging)"""
        with self._lock:
            return {
                "auth_script_path": self.auth_script_path,
                "auth_script_exists": os.path.exists(self.auth_script_path),
                "instance_id": id(self),
                "singleton_instance": True,
                "initialized": getattr(self, '_initialized', False)
            }