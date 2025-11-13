# Deploy Windows Server Core AICT using Azure Container Registry Build
param(
    [string]$ResourceGroupName = "BITRG",
    [string]$Location = "West US",
    [string]$ContainerName = "aictinstance",
    [string]$RegistryName = "aictregistry",
    [string]$UserAssignedManagedIdentity = "BIT-UAMI",
    [string]$UserAssignedManagedIdentityClientId = "2c0599ac-a195-4f6b-938c-966484fff2cd",
    [string]$UserAssignedManagedIdentityObjectId = "d1106a79-97b0-40fe-9964-fd4fe521dbd4",
    [string]$LogAnalyticsWorkspaceName = "AICTLogAnalytics",
    [switch]$EnableLogAnalytics = $true
)

Write-Host "Deploying AICT (Windows Server Core) using Azure Container Registry Build" -ForegroundColor Green

# Check Azure login
Write-Host "Checking Azure authentication..." -ForegroundColor Cyan
$account = az account show --output json 2>$null | ConvertFrom-Json
if (-not $account) {
    Write-Host "Please login to Azure: az login" -ForegroundColor Red
    exit 1
}
Write-Host "Logged in as: $($account.user.name)" -ForegroundColor Green

# Create resource group
Write-Host "Ensuring resource group exists..." -ForegroundColor Cyan
$rgExists = az group exists --name $ResourceGroupName --only-show-errors
if ($rgExists -eq "false") {
    Write-Host "Creating resource group..." -ForegroundColor Yellow
    az group create --name $ResourceGroupName --location $Location --only-show-errors
}
Write-Host "Resource group ready" -ForegroundColor Green

# Create Azure Container Registry with Standard SKU (required for ACR Tasks)
Write-Host "Setting up Azure Container Registry..." -ForegroundColor Cyan
$acr = az acr show --name $RegistryName --resource-group $ResourceGroupName --only-show-errors 2>$null
if (-not $acr) {
    Write-Host "Creating ACR with Standard SKU for ACR Tasks..." -ForegroundColor Yellow
    az acr create --name $RegistryName --resource-group $ResourceGroupName --location $Location --sku Standard --admin-enabled true --only-show-errors
} else {
    # Update to Standard SKU if it's Basic
    Write-Host "Updating ACR to Standard SKU if needed..." -ForegroundColor Yellow
    az acr update --name $RegistryName --resource-group $ResourceGroupName --sku Standard --only-show-errors
}

$acrInfo = az acr show --name $RegistryName --resource-group $ResourceGroupName --output json --only-show-errors | ConvertFrom-Json
$loginServer = $acrInfo.loginServer
Write-Host "ACR ready: $loginServer" -ForegroundColor Green

# Build image using Azure Container Registry Build (ACR Tasks)
Write-Host "Building Windows image using Azure Container Registry Tasks..." -ForegroundColor Cyan
Write-Host "This builds the image in Azure cloud, avoiding local Docker issues..." -ForegroundColor Yellow
Write-Host "Showing verbose output including Dockerfile processing..." -ForegroundColor Yellow

# Make sure Dockerfile exists
if (-not (Test-Path ".\Dockerfile")) {
    Write-Host "Error: Dockerfile not found in the current directory." -ForegroundColor Red
    Write-Host "Current directory: $(Get-Location)" -ForegroundColor Red
    exit 1
}

# Run the ACR build directly (not using a job)
Write-Host "Starting build process..." -ForegroundColor Yellow
Write-Host "Using Dockerfile in: $(Get-Location)" -ForegroundColor Cyan

# Run the command directly - gives better visibility of build progress
# Use timestamp to force fresh build and avoid caching issues
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
Write-Host "Building with timestamp: $timestamp to ensure fresh build" -ForegroundColor Yellow
Write-Host ""
Write-Host "Docker Build Progress Indicators:" -ForegroundColor Green
Write-Host "Base image download" -ForegroundColor Gray
Write-Host "Python 3.11.9 installation" -ForegroundColor Gray  
Write-Host ".NET 8.0 SDK installation" -ForegroundColor Gray
Write-Host "PowerShell Core installation" -ForegroundColor Gray
Write-Host "Requirements.txt copy..." -ForegroundColor Yellow
Write-Host "Python packages installation..." -ForegroundColor Yellow
Write-Host "Application files copy..." -ForegroundColor Yellow
Write-Host ""
$buildResult = az acr build --registry $RegistryName --image "aict:$timestamp" --file Dockerfile --platform windows --build-arg BUILD_DATE=$timestamp .

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Build Completed Successfully!" -ForegroundColor Green
    Write-Host "Requirements.txt copied" -ForegroundColor Green
    Write-Host "Python packages installed" -ForegroundColor Green
    Write-Host "Lib folder copied" -ForegroundColor Green
    Write-Host "Templates copied" -ForegroundColor Green
    Write-Host "WebApp copied" -ForegroundColor Green
    Write-Host "KnowledgeBase copied" -ForegroundColor Green
    Write-Host "Src folder copied" -ForegroundColor Green
    Write-Host "Auth scripts copied" -ForegroundColor Green
    Write-Host ""
    Write-Host "Image built successfully in Azure!" -ForegroundColor Green
    $imageName = "$loginServer/aict:$timestamp"
} else {
    Write-Host ""
    Write-Host "Azure build failed with exit code: $LASTEXITCODE" -ForegroundColor Red
    Write-Host "Check ACR Tasks and platform support." -ForegroundColor Red
    Write-Host $buildResult -ForegroundColor Gray
    exit 1
}

# Get ACR credentials
$acrCreds = az acr credential show --name $RegistryName --output json | ConvertFrom-Json
$acrUsername = $acrCreds.username
$acrPassword = $acrCreds.passwords[0].value

# Delete existing container if it exists
Write-Host "Checking for existing container..." -ForegroundColor Cyan
$existing = az container show --name $ContainerName --resource-group $ResourceGroupName 2>$null
if ($existing) {
    Write-Host "Deleting existing container..." -ForegroundColor Yellow
    az container delete --name $ContainerName --resource-group $ResourceGroupName --yes
    Start-Sleep -Seconds 15
}

# Deploy to Azure Container Instances (Windows)
Write-Host ""
Write-Host "Starting Container Deployment..." -ForegroundColor Cyan
Write-Host "Deployment Configuration:" -ForegroundColor Yellow
Write-Host "   • Container Name: $ContainerName" -ForegroundColor White
Write-Host "   • Image: $imageName" -ForegroundColor White
Write-Host "   • CPU: 2 cores" -ForegroundColor White
Write-Host "   • Memory: 4 GB" -ForegroundColor White
Write-Host "   • Ports: 80, 443 (HTTP/HTTPS - for Application Gateway backend)" -ForegroundColor White
Write-Host "   • OS: Windows Server Core" -ForegroundColor White
Write-Host "   • Identity: $UserAssignedManagedIdentity" -ForegroundColor White
Write-Host ""

# Get the full resource ID of the user-assigned managed identity
$identityResourceId = az identity show --name $UserAssignedManagedIdentity --resource-group $ResourceGroupName --query id -o tsv

if (-not $identityResourceId) {
    Write-Host "Error: Could not find the specified User-Assigned Managed Identity." -ForegroundColor Red
    Write-Host "Please ensure the identity '$UserAssignedManagedIdentity' exists in resource group '$ResourceGroupName'." -ForegroundColor Red
    exit 1
}

# Setup Log Analytics workspace if enabled
$logAnalyticsWorkspaceId = $null
$logAnalyticsWorkspaceKey = $null

if ($EnableLogAnalytics) {
    Write-Host "Setting up Log Analytics for container monitoring..." -ForegroundColor Cyan
    
    # Check if Log Analytics workspace exists, if not, create one
    Write-Host "Checking for existing Log Analytics workspace..." -ForegroundColor Cyan
    $workspace = az monitor log-analytics workspace list --resource-group $ResourceGroupName --query "[?name=='$LogAnalyticsWorkspaceName']" -o json | ConvertFrom-Json
    
    if ($workspace.Count -eq 0) {
        Write-Host "Creating Log Analytics workspace '$LogAnalyticsWorkspaceName'..." -ForegroundColor Yellow
        az monitor log-analytics workspace create `
            --resource-group $ResourceGroupName `
            --workspace-name $LogAnalyticsWorkspaceName `
            --location $Location
    
        # Get workspace details
        $workspace = az monitor log-analytics workspace show `
            --resource-group $ResourceGroupName `
            --workspace-name $LogAnalyticsWorkspaceName -o json | ConvertFrom-Json
    } else {
        $workspace = $workspace[0]
        Write-Host "Using existing Log Analytics workspace '$LogAnalyticsWorkspaceName'..." -ForegroundColor Green
    }
    
    # Get workspace ID and key
    $logAnalyticsWorkspaceId = az monitor log-analytics workspace show `
        --resource-group $ResourceGroupName `
        --workspace-name $LogAnalyticsWorkspaceName `
        --query customerId -o tsv
    
    $logAnalyticsWorkspaceKey = az monitor log-analytics workspace get-shared-keys `
        --resource-group $ResourceGroupName `
        --workspace-name $LogAnalyticsWorkspaceName `
        --query primarySharedKey -o tsv
    
    Write-Host "Log Analytics workspace ready: $LogAnalyticsWorkspaceName" -ForegroundColor Green
}

Write-Host "Identity Resource ID: $identityResourceId" -ForegroundColor Green

# Read all environment variables from .env file
Write-Host "Reading environment variables from .env file..." -ForegroundColor Yellow
$envVariables = @{}
if (Test-Path ".\.env") {
    Get-Content ".\.env" | ForEach-Object {
        if (-not [string]::IsNullOrWhiteSpace($_) -and -not $_.StartsWith("#")) {
            $key, $value = $_ -split '=', 2
            if ($key -and $value) {
                $envVariables[$key] = $value.Trim()
                Write-Host "Found environment variable: $key" -ForegroundColor Gray
            }
        }
    }
    Write-Host "Successfully loaded $(($envVariables.Keys).Count) environment variables" -ForegroundColor Green
} else {
    Write-Host "Warning: .env file not found. Only using script parameters." -ForegroundColor Yellow
}

Write-Host "Creating environment variables array for container..." -ForegroundColor Cyan
$envVarArray = @(
    "HOST=0.0.0.0",
    "PORT=80",
    "FLASK_ENV=production", 
    "PYTHONPATH=C:/app/lib",
    "PYTHONIOENCODING=utf-8",
    "FLASK_DEBUG=1",
    "DEBUG_MODE=1",
    "ENABLE_HTTPS=true",
    "LOGGING_LEVEL=DEBUG",
    "AZURE_IDENTITY_LOG_LEVEL=DEBUG",
    "USE_SYSTEM_IDENTITY=true",
    "USER_ASSIGNED_IDENTITY_CLIENT_ID=$UserAssignedManagedIdentityClientId",
    "AZURE_IDENTITY_DISABLE_SF=true",
    "CONTAINER_TYPE=ACI",
    "Resource_Group_Name=$ResourceGroupName",
    "Power_BI_Secret_Name=powerbiaccesstoken",
    "Cognitive_Service_Secret_Name=cognitiveserviceaccesstoken",
    "Key_Vault_URI=https://MCPKeyValut.vault.azure.net",
    "PROJECT_ENDPOINT=https://bit-aifoundry.cognitiveservices.azure.com",
    "MODEL_DEPLOYMENT_NAME=gpt-4.1",
    "User_Assigned_Managed_Identity=$UserAssignedManagedIdentity",
    "Location=$Location",
    "User_Assigned_Managed_Identity_Client_Id=$UserAssignedManagedIdentityClientId",
    "Key_Vault_Name=MCPKeyValut",
    "Fabric_Secret_Name=fabricaccesstoken",
    "User_Assigned_Managed_Identity_Object_Id=$UserAssignedManagedIdentityObjectId",
    "ACR_Name=$RegistryName",
    "AZURE_OPENAI_API_VERSION=2025-01-01-preview",
    "Use_Local_Auth=0",
    "Skip_Create_Tokens=False"
)

# Override with values from .env file if they exist
# Exclude critical authentication variables that should not be overridden
$excludeFromOverride = @("Use_Local_Auth", "Skip_Create_Tokens")

foreach ($key in $envVariables.Keys) {
    # Skip variables that should not be overridden
    if ($excludeFromOverride -contains $key) {
        Write-Host "Skipping override for critical variable: $key (keeping hardcoded value)" -ForegroundColor Yellow
        continue
    }
    
    # Find matching environment variable in the array
    $index = $null
    for ($i = 0; $i -lt $envVarArray.Count; $i++) {
        if ($envVarArray[$i] -match "^$key=") {
            $index = $i
            break
        }
    }
    
    # If found, replace the value with the one from .env
    if ($index -ne $null) {
        $envVarArray[$index] = "$key=$($envVariables[$key])"
    } 
    # If not found, add it
    else {
        $envVarArray += "$key=$($envVariables[$key])"
    }
}

# Environment variables should be passed as individual key=value pairs to Azure CLI
Write-Host "Passing $(($envVarArray).Count) environment variables to container" -ForegroundColor Cyan

# Execute container creation command directly using backticks, the same way as setup_logging.ps1
Write-Host "Container Configuration Steps:" -ForegroundColor Cyan
Write-Host "   Registry authentication configured" -ForegroundColor Green
Write-Host "   System & User-assigned identities configured" -ForegroundColor Green
Write-Host "   Environment variables prepared ($(($envVarArray).Count) variables)" -ForegroundColor Green
Write-Host "   Log Analytics workspace ready" -ForegroundColor Green
Write-Host ""
Write-Host "Executing container creation..." -ForegroundColor Yellow

# Build environment variables as individual parameters
$envVarParams = @()
foreach ($envVar in $envVarArray) {
    $envVarParams += $envVar
}

# Create the container using direct Azure CLI command with proper parameter formatting
$createResult = az container create `
    --name $ContainerName `
    --resource-group $ResourceGroupName `
    --location $Location `
    --image $imageName `
    --registry-login-server $loginServer `
    --registry-username $acrUsername `
    --registry-password $acrPassword `
    --cpu 2 `
    --memory 4 `
    --os-type Windows `
    --ports 80 443 `
    --dns-name-label $ContainerName `
    --assign-identity '[system]' $identityResourceId `
    --log-analytics-workspace $logAnalyticsWorkspaceId `
    --log-analytics-workspace-key $logAnalyticsWorkspaceKey `
    --environment-variables $envVarParams `
    --only-show-errors `
    --output json

Write-Host "Container creation completed with exit code: $LASTEXITCODE" -ForegroundColor Cyan

if ($LASTEXITCODE -eq 0) {
    Write-Host "Deployment successful!" -ForegroundColor Green
    
    # Get container details
    Start-Sleep -Seconds 10
    $containerInfo = az container show --name $ContainerName --resource-group $ResourceGroupName --output json | ConvertFrom-Json
    
    # Set up diagnostic settings for container logs if Log Analytics is enabled
    if ($EnableLogAnalytics) {
        Write-Host "Configuring diagnostic settings for container instance..." -ForegroundColor Cyan
        
        # Get container group resource ID
        $containerResourceId = az container show `
            --name $ContainerName `
            --resource-group $ResourceGroupName `
            --query id -o tsv
        
        # Create JSON files for logs and metrics configuration
        $logsJson = '[{"category": "ContainerInstanceLog", "enabled": true}, {"category": "ContainerEvent", "enabled": true}]'
        $metricsJson = '[{"category": "AllMetrics", "enabled": true}]'
        
        $logsFile = "$env:TEMP\container-logs.json"
        $metricsFile = "$env:TEMP\container-metrics.json"
        $logsJson | Out-File -FilePath $logsFile -Encoding utf8
        $metricsJson | Out-File -FilePath $metricsFile -Encoding utf8
        
        # Create diagnostic settings
        $workspaceResourceId = az monitor log-analytics workspace show `
            --resource-group $ResourceGroupName `
            --workspace-name $LogAnalyticsWorkspaceName `
            --query id -o tsv
            
        az monitor diagnostic-settings create `
            --name "ACI-Diagnostics" `
            --resource $containerResourceId `
            --workspace $workspaceResourceId `
            --logs "@$logsFile" `
            --metrics "@$metricsFile"
            
        Write-Host "Diagnostic settings configured for container logs" -ForegroundColor Green
    }
    
    if ($containerInfo.ipAddress.fqdn) {
        $fqdn = $containerInfo.ipAddress.fqdn
        Write-Host ""
        Write-Host "AICT (Windows) is deployed as HTTP backend!" -ForegroundColor Green
        Write-Host "==========================================" -ForegroundColor Green
        Write-Host "Backend Application (HTTP): http://$fqdn" -ForegroundColor Cyan
        Write-Host "Backend Application (HTTPS): https://$fqdn" -ForegroundColor Cyan
        Write-Host "Health Check (HTTP): http://$fqdn/api/health" -ForegroundColor Cyan
        Write-Host "Health Check (HTTPS): https://$fqdn/api/health" -ForegroundColor Cyan
        Write-Host "NOTE: Container configured for HTTPS with Application Gateway SSL termination" -ForegroundColor Yellow
        Write-Host "Upload your SSL certificate to Application Gateway for secure public access" -ForegroundColor Yellow
        
        Write-Host ""
        Write-Host "AZURE CLI AUTHENTICATION REQUIRED" -ForegroundColor Yellow
        Write-Host "=======================================" -ForegroundColor Yellow
        Write-Host "To enable Azure CLI authentication in the container:" -ForegroundColor White
        Write-Host ""
        Write-Host "1. Connect to the container:" -ForegroundColor Cyan
        Write-Host "   az container exec --resource-group $ResourceGroupName --name $ContainerName --exec-command cmd" -ForegroundColor White
        Write-Host ""
        Write-Host "2. In the container, run Azure CLI login:" -ForegroundColor Cyan
        Write-Host "   az login" -ForegroundColor White
        Write-Host ""
        Write-Host "3. Follow the device code authentication" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "4. Verify login with:" -ForegroundColor Cyan
        Write-Host "   az account show" -ForegroundColor White
        Write-Host ""
        Write-Host "After authentication, refresh your browser and try logging in!" -ForegroundColor Green
        
        Write-Host ""
        Write-Host "Features:" -ForegroundColor Yellow
        Write-Host "- Windows Server Core container" -ForegroundColor White
        Write-Host "- Python 3.11.9" -ForegroundColor White
        Write-Host "- Azure CLI integrated" -ForegroundColor White
        Write-Host "- ADOMD.NET libraries included" -ForegroundColor White
        Write-Host "- Flask web application" -ForegroundColor White
        Write-Host "- User-Assigned Managed Identity: $UserAssignedManagedIdentity" -ForegroundColor White
        Write-Host "- Managed Identity Client ID: $UserAssignedManagedIdentityClientId" -ForegroundColor White
        
        Write-Host ""
        Write-Host "Management:" -ForegroundColor Yellow
        Write-Host "View logs: az container logs --resource-group $ResourceGroupName --name $ContainerName --follow" -ForegroundColor White
        Write-Host "Restart: az container restart --resource-group $ResourceGroupName --name $ContainerName" -ForegroundColor White
        Write-Host "Connect: az container exec --resource-group $ResourceGroupName --name $ContainerName --exec-command cmd" -ForegroundColor White
        
        if ($EnableLogAnalytics) {
            Write-Host ""
            Write-Host "Log Analytics:" -ForegroundColor Yellow
            Write-Host "Workspace: $LogAnalyticsWorkspaceName" -ForegroundColor White
            Write-Host "Example KQL query: ContainerInstanceLog_CL | where ContainerInstanceName_s == '$ContainerName' | order by TimeGenerated desc" -ForegroundColor White
            Write-Host "View in Azure Portal: https://portal.azure.com/#blade/Microsoft_OperationalInsights/WorkspaceViewerBlade/overview/resourceId/%2Fsubscriptions%2F$($account.id)%2FresourceGroups%2F$ResourceGroupName%2Fproviders%2FMicrosoft.OperationalInsights%2Fworkspaces%2F$LogAnalyticsWorkspaceName" -ForegroundColor White
        }
    }
} else {
    Write-Host "Deployment failed" -ForegroundColor Red
    Write-Host "Check the container logs for details:" -ForegroundColor Yellow
    Write-Host "az container logs --resource-group $ResourceGroupName --name $ContainerName" -ForegroundColor White
    exit 1
}

Write-Host ""
Write-Host "Setting up Key Vault access for both Managed Identities (User-assigned and System-assigned)..." -ForegroundColor Cyan

# Wait a bit longer to ensure the container is fully registered
Write-Host "Waiting for container identity to be fully registered..." -ForegroundColor Yellow
Start-Sleep -Seconds 30

# Get the system-assigned identity principal ID with retries
Write-Host "Getting the system-assigned identity principal ID..." -ForegroundColor Cyan
$maxRetries = 3
$retryCount = 0
$systemIdentityPrincipalId = $null

while ($retryCount -lt $maxRetries -and -not $systemIdentityPrincipalId) {
    try {
        $systemIdentityPrincipalId = az container show `
            --resource-group $ResourceGroupName `
            --name $ContainerName `
            --query "identity.principalId" -o tsv
        
        if ([string]::IsNullOrWhiteSpace($systemIdentityPrincipalId)) {
            throw "Identity not yet available"
        }
    }
    catch {
        $retryCount++
        if ($retryCount -lt $maxRetries) {
            Write-Host "Identity information not yet available. Retrying in 10 seconds..." -ForegroundColor Yellow
            Start-Sleep -Seconds 10
        }
        else {
            Write-Host "Could not retrieve system identity after $maxRetries attempts." -ForegroundColor Red
            Write-Host "This is not fatal, but system-assigned identity may not be configured properly." -ForegroundColor Yellow
        }
    }
}

# Check if a Key Vault exists in the resource group
$keyVaults = az keyvault list --resource-group $ResourceGroupName --query "[].name" -o tsv

if ($keyVaults) {
    # If there are multiple key vaults, split the output into an array
    $keyVaultArray = $keyVaults -split "\s+"
    
    foreach ($keyVaultName in $keyVaultArray) {
        if ($keyVaultName) {
            # 1. Grant permissions to user-assigned identity
            Write-Host "Granting the User-Assigned Managed Identity 'get' and 'list' permissions to Key Vault: $keyVaultName" -ForegroundColor Yellow
            
            az keyvault set-policy `
                --name $keyVaultName `
                --resource-group $ResourceGroupName `
                --object-id $UserAssignedManagedIdentityObjectId `
                --secret-permissions get list
                
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Access policy added successfully for User-Assigned Identity to Key Vault: $keyVaultName" -ForegroundColor Green
            } else {
                Write-Host "Failed to set access policy for User-Assigned Identity to Key Vault: $keyVaultName" -ForegroundColor Red
            }
            
            # 2. Grant comprehensive permissions to system-assigned identity if it exists
            if ($systemIdentityPrincipalId) {
                Write-Host "Granting the System-Assigned Managed Identity comprehensive permissions to Key Vault: $keyVaultName" -ForegroundColor Yellow
                
                az keyvault set-policy `
                    --name $keyVaultName `
                    --resource-group $ResourceGroupName `
                    --object-id $systemIdentityPrincipalId `
                    --secret-permissions get list set delete backup restore recover purge `
                    --certificate-permissions get list update create import delete recover backup restore managecontacts manageissuers getissuers listissuers setissuers deleteissuers `
                    --key-permissions get list update create import delete recover backup restore
                    
                if ($LASTEXITCODE -eq 0) {
                    Write-Host "Access policy added successfully for System-Assigned Identity to Key Vault: $keyVaultName" -ForegroundColor Green
                } else {
                    Write-Host "Failed to set access policy for System-Assigned Identity to Key Vault: $keyVaultName" -ForegroundColor Red
                }
            } else {
                Write-Host "No system-assigned identity found for container. Skipping system identity access policy." -ForegroundColor Yellow
            }
        }
    }
} else {
    Write-Host "No Key Vaults found in resource group $ResourceGroupName" -ForegroundColor Yellow
    Write-Host "If you have a Key Vault in another resource group, please set access policy manually:" -ForegroundColor Yellow
    Write-Host "az keyvault set-policy --name YOUR_KEYVAULT_NAME --resource-group KEYVAULT_RESOURCE_GROUP --object-id $UserAssignedManagedIdentityObjectId --secret-permissions get list" -ForegroundColor White
    
    if ($systemIdentityPrincipalId) {
        Write-Host "And for system-assigned identity:" -ForegroundColor Yellow
        Write-Host "az keyvault set-policy --name YOUR_KEYVAULT_NAME --resource-group KEYVAULT_RESOURCE_GROUP --object-id $systemIdentityPrincipalId --secret-permissions get list set delete backup restore recover purge" -ForegroundColor White
    }
}

Write-Host ""
Write-Host "Deployment completed successfully!" -ForegroundColor Green
