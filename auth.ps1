
param(
    [Parameter(Mandatory=$true)]
    [string]$secretName
)

# Key Vault details from .env file
$keyVaultName = "MCPKeyValut"
$keyVaultUri = "https://MCPKeyValut.vault.azure.net"
$principalId = "d1106a79-97b0-40fe-9964-fd4fe521dbd4"

Write-Host "Attempting authentication with Principal ID: $principalId"
Write-Host "Retrieving secret: $secretName"

# Check if running in Azure Container Instance (ACI) with Managed Identity
$identityEndpoint = $env:IDENTITY_ENDPOINT
$identityHeader = $env:IDENTITY_HEADER

if ($identityEndpoint -and $identityHeader) {
    Write-Host "Using Managed Identity authentication (Container environment)"
    try {
        # Get access token for Key Vault using Managed Identity
        $resource = "https://vault.azure.net"
        $tokenResponse = Invoke-RestMethod -Uri "$identityEndpoint" -Method Get -Headers @{secret = $identityHeader} -Body @{resource = $resource; principalId = $principalId} -ContentType "application/x-www-form-urlencoded"
        Write-Host "Key Vault authentication successful"
        
        $accessToken = $tokenResponse.access_token
    }
    catch {
        Write-Host "Managed Identity authentication failed: $_"
        Write-Host "Error Details: $($_.Exception.Message)"
        return $null
    }
}
else {
    Write-Host "Using Azure CLI authentication (Local environment)"
    try {
        # Check if Azure CLI is logged in
        $account = az account show --output json 2>$null | ConvertFrom-Json
        if (-not $account) {
            Write-Host "Please login to Azure CLI: az login" -ForegroundColor Red
            return $null
        }
        
        # Get access token using Azure CLI
        $tokenResult = az account get-access-token --resource "https://vault.azure.net" --output json | ConvertFrom-Json
        $accessToken = $tokenResult.accessToken
        Write-Host "Azure CLI authentication successful"
    }
    catch {
        Write-Host "Azure CLI authentication failed: $_"
        Write-Host "Error Details: $($_.Exception.Message)"
        Write-Host "Please ensure you are logged in with: az login" -ForegroundColor Yellow
        return $null
    }
}

# Set up headers for Key Vault API call
$authHeaders = @{
    'Authorization' = "Bearer $accessToken"
    'Content-Type' = 'application/json'
}

Write-Host "`nRetrieving secret from Key Vault..."

# Retrieve the specified secret with correct API version
try {
    # Use API version 7.3 (more stable than 7.4)
    $secretUrl = "$keyVaultUri/secrets/$secretName"
    $secretResponse = Invoke-RestMethod -Uri $secretUrl -Method Get -Headers $authHeaders -Body @{'api-version' = '7.3'}
    
    Write-Host "Secret '$secretName' retrieved successfully:"
    Write-Host "Token: $($secretResponse.value)"
    
    # Return the token value for use in other scripts
    return $secretResponse.value
}
catch {
    Write-Host "Failed to retrieve secret '$secretName': $_"
    Write-Host "Error Details: $($_.Exception.Message)"
    if ($_.Exception.Response) {
        Write-Host "HTTP Status: $($_.Exception.Response.StatusCode)"
        Write-Host "Response Content: $($_.Exception.Response.Content)" -ForegroundColor Gray
    }
    
    # Try alternative API call format
    Write-Host "`nTrying alternative API call format..." -ForegroundColor Yellow
    try {
        $secretUrl = "$keyVaultUri/secrets/$secretName" + "?api-version=7.3"
        $secretResponse = Invoke-RestMethod -Uri $secretUrl -Method Get -Headers $authHeaders
        
        Write-Host "Secret '$secretName' retrieved successfully with alternative format:"
        Write-Host "Token: $($secretResponse.value)"
        return $secretResponse.value
    }
    catch {
        Write-Host "Alternative format also failed: $_"
        Write-Host "Please check:"
        Write-Host "1. Secret name '$secretName' exists in Key Vault '$keyVaultName'"
        Write-Host "2. You have 'Get' permissions on the Key Vault secrets"
        Write-Host "3. Key Vault URI is correct: $keyVaultUri"
        return $null
    }
}
