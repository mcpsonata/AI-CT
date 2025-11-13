# Optimized Windows Server Core Dockerfile for Azure Container Registry Build
# This version is designed to work with modern Docker best practices

FROM mcr.microsoft.com/windows/servercore:ltsc2022

# Set working directory
WORKDIR C:/app

# Use PowerShell as default shell and optimize for pipeline
SHELL ["powershell", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]

# Set labels for better metadata
LABEL maintainer="Microsoft"
LABEL version="1.0"
LABEL description="AICT with ADOMD.NET support"

# Download and install Python 3.11.9 in separate steps for better caching
RUN Write-Host 'Downloading Python 3.11.9...'; \
    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile 'python-installer.exe' -UseBasicParsing; \
    Write-Host 'Installing Python...'; \
    Start-Process -FilePath 'python-installer.exe' -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'TargetDir=C:\Python311' -Wait; \
    Remove-Item 'python-installer.exe' -Force; \
    # Verify Python installation and upgrade pip in the same layer
    C:\Python311\python.exe --version; \
    C:\Python311\python.exe -m pip install --upgrade pip; \
    # Set Python to not create bytecode to reduce container size
    [Environment]::SetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', '1', 'Machine')

# Download and install .NET 8.0 SDK
RUN Write-Host 'Downloading .NET 8.0 SDK...'; \
    Invoke-WebRequest -Uri 'https://dotnetcli.azureedge.net/dotnet/Sdk/8.0.403/dotnet-sdk-8.0.403-win-x64.exe' -OutFile 'dotnet-sdk-installer.exe' -UseBasicParsing; \
    Write-Host 'Installing .NET 8.0 SDK...'; \
    Start-Process -FilePath 'dotnet-sdk-installer.exe' -ArgumentList '/quiet', '/norestart' -Wait; \
    Remove-Item 'dotnet-sdk-installer.exe' -Force; \
    # Verify .NET installation
    Write-Host 'Verifying .NET installation...'; \
    & 'C:\Program Files\dotnet\dotnet.exe' --version

# Download and install PowerShell Core 7.4.3
RUN Write-Host 'Downloading PowerShell Core 7.4.3...'; \
    Invoke-WebRequest -Uri 'https://github.com/PowerShell/PowerShell/releases/download/v7.4.3/PowerShell-7.4.3-win-x64.msi' -OutFile 'PowerShell.msi' -UseBasicParsing; \
    Write-Host 'Installing PowerShell Core...'; \
    Start-Process -FilePath 'msiexec.exe' -ArgumentList '/i', 'PowerShell.msi', '/quiet', '/norestart' -Wait; \
    Remove-Item 'PowerShell.msi' -Force; \
    # Verify PowerShell Core installation
    Write-Host 'Verifying PowerShell Core installation...'; \
    & 'C:\Program Files\PowerShell\7\pwsh.exe' -version

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies using separate RUN commands for better error visibility
RUN C:\Python311\python.exe -m pip install --upgrade pip
RUN C:\Python311\python.exe -m pip install -r requirements.txt --no-cache-dir

# Copy ADOMD.NET libraries and ensure they're in a specific layer
COPY lib/ ./lib/

# Copy templates for web application
COPY templates/ ./templates/

# Copy webapp folder with all Python application files
COPY webapp/ ./webapp/

# Copy KnowledgeBase folder
COPY KnowledgeBase/ ./KnowledgeBase/

# Copy src folder for server modules
COPY src/ ./src/

# Copy authentication and deployment scripts
COPY auth.ps1 ./

# Set environment variables
ENV PYTHONPATH="C:/app/lib" \
    FLASK_DEBUG=0 \
    HOST=0.0.0.0 \
    PORT=443 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PATH="C:/Program Files/PowerShell/7;C:/Program Files/dotnet;C:/Program Files (x86)/Microsoft SDKs/Azure/CLI2/wbin;C:/Python311;C:/Python311/Scripts;${PATH}"

CMD ["C:\\Python311\\python.exe", "webapp/app.py"]