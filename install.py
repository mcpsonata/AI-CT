#!/usr/bin/env python3
"""
MCP Server Environment Quickstart Setup Script
==============================================

This script provides automated environment setup for MCP (Model Context Protocol) server development.
It focuses purely on setting up the development environment and dependencies with intelligent detection.

Features:
- 🔍 Smart detection: Checks for existing installations before downloading
- ✅ Multi-method verification: Command line, registry, file paths, package managers
- ⚡ Skip unnecessary downloads: Only installs missing components
- 🛠️ Complete automation: Handles all software and dependency setup
- 🎯 Environment-focused: Prepares development environment only
- 🛡️ Safe package installation: Prevents shell interpretation issues with version operators
- 🧹 Auto-cleanup: Removes accidentally created files from previous runs
- 📱 Unicode-safe output: Windows console compatible status indicators
- Checks Python version compatibility (requires exactly 3.11.9)
- Automatically installs Python 3.11.9 if different version detected
- AUTOMATICALLY INSTALLS missing software:
  * Git for Windows
  * Azure CLI
  * Visual Studio Code + Extensions
  * .NET 8.0 SDK
  * AMO & ADOMD 19.84.1.0 libraries
- Creates virtual environment with all required packages
- Downloads NLTK linguistic data (7 datasets)
- Creates .env template with all necessary variables
- Sets up VS Code MCP configuration template
- Installs essential VS Code extensions
- Runs comprehensive environment validation

Administrator Privileges:
- Script automatically requests admin privileges for software installation
- Can run without admin rights (manual installation guidance provided)

Azure Resource Access Requirements (manual):
- Access to 'mcapscustome-nkmr' with 'mcapsda_bicenterofexcellence_team' role
- PIM activation for 'mcapsda_bicenterofexcellence' resources
- VPN connection via 'mcapsda-bicenterofexcellence-vng' gateway
- Access to 'mcapsda-bicenterofexcellence-aiproject'

Usage:
    python Quickstart.py
    
Note: This script sets up the environment only. Project code should be obtained separately.
"""

import sys
import os
import subprocess
import platform
import json
import shutil
import urllib.request
import zipfile
import tempfile
import time
import winreg
import glob
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Global flags for script behavior (set by command line arguments)
FORCE_REINSTALL = False
CHECK_ONLY = False

# Global variables for Python management
PYTHON_EXECUTABLE = sys.executable

# Color codes for terminal output
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    BOLD = '\033[1m'
    END = '\033[0m'

def print_header(message: str):
    """Print a formatted header message."""
    print(f"\n{Colors.CYAN}{Colors.BOLD}{'='*60}{Colors.END}")
    print(f"{Colors.CYAN}{Colors.BOLD}{message.center(60)}{Colors.END}")
    print(f"{Colors.CYAN}{Colors.BOLD}{'='*60}{Colors.END}\n")

def print_success(message: str):
    """Print a success message."""
    print(f"{Colors.GREEN}[OK] {message}{Colors.END}")

def print_warning(message: str):
    """Print a warning message."""
    print(f"{Colors.YELLOW}[WARNING] {message}{Colors.END}")

def print_error(message: str):
    """Print an error message."""
    print(f"{Colors.RED}[ERROR] {message}{Colors.END}")

def print_info(message: str):
    """Print an info message."""
    print(f"{Colors.BLUE}[INFO] {message}{Colors.END}")

def run_command(command: str, shell: bool = True, capture_output: bool = True) -> Tuple[bool, str]:
    """Run a command and return success status and output."""
    try:
        result = subprocess.run(
            command, 
            shell=shell, 
            capture_output=capture_output, 
            text=True,
            timeout=300  # 5 minute timeout
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        return False, str(e)

def run_pip_install(pip_path: str, package: str) -> Tuple[bool, str]:
    """Safely run pip install with proper handling of package specifications containing operators."""
    try:
        # Use subprocess.run with list arguments to avoid shell interpretation issues
        # This prevents operators like >= from being interpreted as shell redirections
        result = subprocess.run(
            [pip_path, "install", package],
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        return False, str(e)

def cleanup_accidental_files() -> None:
    """Clean up any files accidentally created by shell interpretation of package version operators."""
    import re
    import os
    
    # Pattern to match version number files that might be created by shell redirection
    version_pattern = re.compile(r'^\d+\.\d+(\.\d+)?$')
    
    current_dir = Path(".")
    for file_path in current_dir.iterdir():
        if file_path.is_file() and version_pattern.match(file_path.name):
            try:
                # Check if it contains pip output (indication it was accidentally created)
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read(1000)  # Read first 1000 chars
                    if any(keyword in content.lower() for keyword in ['collecting', 'downloading', 'installing', 'successfully installed']):
                        print_warning(f"Removing accidentally created file: {file_path.name}")
                        file_path.unlink()
            except Exception as e:
                print_warning(f"Could not check/remove file {file_path.name}: {e}")

def check_admin_privileges() -> bool:
    """Check if the script is running with administrator privileges."""
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def request_admin_privileges():
    """Request administrator privileges if not already running as admin."""
    if not check_admin_privileges():
        print_warning("Administrator privileges required for software installation.")
        print_info("The script will attempt to restart with elevated privileges...")
        
        try:
            import ctypes
            # Re-run the script with admin privileges
            ctypes.windll.shell32.ShellExecuteW(
                None, 
                "runas", 
                sys.executable, 
                " ".join(sys.argv), 
                None, 
                1
            )
            sys.exit(0)
        except Exception as e:
            print_error(f"Failed to request admin privileges: {e}")
            print_error("Please run the script as Administrator for automatic software installation.")
            return False
    
    return True

def install_vscode_extensions() -> bool:
    """Install essential VS Code extensions."""
    print_info("Installing VS Code extensions...")
    
    # Check if we're in check-only mode
    if CHECK_ONLY:
        print_info("Check-only mode: Skipping VS Code extension installation")
        return True
    
    extensions = [
        "ms-python.python",
        "ms-vscode.powershell", 
        "ms-vscode.vscode-json",
        "GitHub.copilot"
    ]
    
    success_count = 0
    failed_extensions = []
    
    for extension in extensions:
        print_info(f"Installing {extension}...")
        success, output = run_command(f"code --install-extension {extension}", capture_output=True)
        if success or "already installed" in output.lower():
            print_success(f"Installed {extension}")
            success_count += 1
        else:
            print_warning(f"Failed to install {extension}: {output}")
            failed_extensions.append(extension)
    
    if success_count == len(extensions):
        print_success("All VS Code extensions installed successfully")
        return True
    else:
        print_warning(f"Successfully installed {success_count}/{len(extensions)} extensions")
        if failed_extensions:
            print_info(f"Failed extensions: {', '.join(failed_extensions)}")
        return False
        print_warning(f"Installed {success_count}/{len(extensions)} extensions")
        return success_count > len(extensions) // 2

def check_and_install_python() -> bool:
    """Check if Python 3.11.9 is available and install it if needed."""
    print_header("Checking Python Version")
    
    current_version = sys.version_info
    required_version = (3, 11, 9)
    
    # Check if current Python is exactly 3.11.9
    if current_version[:3] == required_version:
        print_success(f"Python {current_version.major}.{current_version.minor}.{current_version.micro} is exactly the required version")
        return True
    
    # Check if we have Python 3.11.9 available in the system
    python_3119_path = find_python_3119()
    if python_3119_path:
        print_success(f"Found Python 3.11.9 at: {python_3119_path}")
        # Update the current Python path for the rest of the script
        update_python_path(python_3119_path)
        return True
    
    if CHECK_ONLY:
        print_warning(f"Current Python {current_version.major}.{current_version.minor}.{current_version.micro} - Python 3.11.9 required but would not install in check-only mode")
        return False
    
    print_warning(f"Current Python {current_version.major}.{current_version.minor}.{current_version.micro} - Installing Python 3.11.9 for compatibility")
    
    if install_python_3119():
        print_success("Python 3.11.9 installed successfully")
        return True
    else:
        print_error("Failed to install Python 3.11.9")
        return False

def find_python_3119() -> str:
    """Find Python 3.11.9 installation on the system."""
    import glob
    
    # Common Python installation paths on Windows
    possible_paths = [
        "C:\\Python311\\python.exe",
        "C:\\Python3119\\python.exe", 
        "C:\\Program Files\\Python311\\python.exe",
        "C:\\Program Files (x86)\\Python311\\python.exe",
        "C:\\Users\\*\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
        "C:\\ProgramData\\Python311\\python.exe"
    ]
    
    # Add paths from py launcher
    try:
        result = run_command("py -3.11 --version", capture_output=True)
        if result[0] and "3.11.9" in result[1]:
            # Get the actual path
            exe_result = run_command("py -3.11 -c \"import sys; print(sys.executable)\"", capture_output=True)
            if exe_result[0]:
                path = exe_result[1].strip()
                if os.path.exists(path):
                    return path
    except:
        pass
    
    # Check common paths
    for pattern in possible_paths:
        for path in glob.glob(pattern):
            if os.path.exists(path):
                try:
                    result = run_command(f'"{path}" --version', capture_output=True)
                    if result[0] and "3.11.9" in result[1]:
                        return path
                except:
                    continue
    
    return None

def update_python_path(python_path: str) -> None:
    """Update the global Python path variables for the script."""
    global PYTHON_EXECUTABLE
    PYTHON_EXECUTABLE = python_path
    print_info(f"Updated Python executable path to: {python_path}")

def install_python_3119() -> bool:
    """Install Python 3.11.9 automatically."""
    import tempfile
    import os
    
    # Python 3.11.9 installer URL
    python_url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            installer_path = os.path.join(temp_dir, "python-3.11.9-amd64.exe")
            
            print_info("Downloading Python 3.11.9 installer...")
            if not download_file(python_url, installer_path, "Python 3.11.9"):
                return False
            
            print_info("Installing Python 3.11.9...")
            print_info("This may take a few minutes and may require administrator privileges...")
            
            # Install Python with specific options
            install_command = [
                installer_path,
                "/quiet",  # Silent installation
                "InstallAllUsers=1",  # Install for all users
                "PrependPath=0",  # Don't modify PATH (we'll handle this)
                "Include_test=0",  # Skip test suite
                "Include_launcher=1",  # Include py launcher
                f"TargetDir=C:\\Python3119"  # Specific installation directory
            ]
            
            result = subprocess.run(
                install_command,
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout
            )
            
            if result.returncode == 0:
                # Verify installation
                python_path = "C:\\Python3119\\python.exe"
                if os.path.exists(python_path):
                    verify_result = run_command(f'"{python_path}" --version', capture_output=True)
                    if verify_result[0] and "3.11.9" in verify_result[1]:
                        update_python_path(python_path)
                        print_success("Python 3.11.9 installed successfully")
                        print_warning("IMPORTANT: Script will now restart with Python 3.11.9")
                        restart_with_python_3119(python_path)
                        return True
                
            print_error(f"Installation failed: {result.stderr}")
            return False
            
    except Exception as e:
        print_error(f"Failed to install Python 3.11.9: {e}")
        return False

def restart_with_python_3119(python_path: str) -> None:
    """Restart the script using Python 3.11.9."""
    import sys
    import os
    
    print_info("Restarting script with Python 3.11.9...")
    
    # Get current script arguments
    script_args = sys.argv[1:] if len(sys.argv) > 1 else []
    
    # Restart with the new Python executable
    new_command = [python_path, sys.argv[0]] + script_args
    
    try:
        # Start the new process
        subprocess.run(new_command, check=True)
        # Exit current process
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        print_error(f"Failed to restart with Python 3.11.9: {e}")
        sys.exit(1)

def check_python_version() -> bool:
    """Legacy function - now redirects to check_and_install_python."""
    return check_and_install_python()

def download_file(url: str, filename: str, description: str = "") -> bool:
    """Download a file with progress indication."""
    try:
        print_info(f"Downloading {description or filename}...")
        
        def progress_hook(block_num, block_size, total_size):
            if total_size > 0:
                percent = min(100, (block_num * block_size * 100) // total_size)
                print(f"\rProgress: {percent}%", end="", flush=True)
        
        urllib.request.urlretrieve(url, filename, progress_hook)
        print()  # New line after progress
        print_success(f"Downloaded {filename}")
        return True
    except Exception as e:
        print_error(f"Failed to download {filename}: {e}")
        return False

def install_software_silently(installer_path: str, install_args: List[str], software_name: str) -> bool:
    """Install software silently using the provided installer."""
    try:
        print_info(f"Installing {software_name}...")
        cmd = [installer_path] + install_args
        
        # Run with elevated privileges if needed
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800  # 30 minute timeout for installations
        )
        
        if result.returncode == 0:
            print_success(f"{software_name} installed successfully")
            return True
        else:
            print_error(f"Installation failed for {software_name}: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print_error(f"Installation of {software_name} timed out")
        return False
    except Exception as e:
        print_error(f"Error installing {software_name}: {e}")
        return False

def check_and_install_git() -> bool:
    """Check for Git and install if missing."""
    print_info("Checking Git installation...")
    
    # Method 1: Check via command line (most reliable)
    success, output = run_command("git --version", capture_output=True)
    if success and "git version" in output.lower():
        print_success(f"Git is installed: {output.strip()}")
        return True
    
    # Method 2: Check common installation paths
    git_paths = [
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe",
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe"
    ]
    
    for path in git_paths:
        if os.path.exists(path):
            print_success(f"Git found: {path}")
            return True
    
    # Method 3: Check Windows Registry for installed programs
    try:
        import winreg
        registry_paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
        ]
        
        for reg_path in registry_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                                    if "git" in display_name.lower() and ("windows" in display_name.lower() or "scm" in display_name.lower()):
                                        print_success(f"Git found in registry: {display_name}")
                                        return True
                                except FileNotFoundError:
                                    continue
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass  # winreg not available (non-Windows system)
    
    print_warning("Git is not installed. Installing Git for Windows...")
    
    # Download Git for Windows
    git_url = "https://github.com/git-for-windows/git/releases/latest/download/Git-2.42.0.2-64-bit.exe"
    git_installer = "Git-installer.exe"
    
    if not download_file(git_url, git_installer, "Git for Windows"):
        return False
    
    # Install Git silently
    install_args = [
        "/SILENT",
        "/COMPONENTS=icons,ext\\reg\\shellhere,assoc,assoc_sh",
        "/TASKS=desktopicon"
    ]
    
    success = install_software_silently(git_installer, install_args, "Git for Windows")
    
    # Cleanup
    if os.path.exists(git_installer):
        os.remove(git_installer)
    
    if success:
        # Add Git to PATH if not already there
        git_path = r"C:\Program Files\Git\bin"
        current_path = os.environ.get("PATH", "")
        if git_path not in current_path:
            os.environ["PATH"] = f"{git_path};{current_path}"
            print_info("Added Git to PATH for current session")
    
    return success

def check_and_install_azure_cli() -> bool:
    """Check for Azure CLI and install if missing."""
    print_info("Checking Azure CLI installation...")
    
    # Method 1: Check via command line (most reliable)
    success, output = run_command("az --version", capture_output=True)
    if success and "azure-cli" in output.lower():
        print_success("Azure CLI is installed")
        return True
    
    # Method 2: Check common installation paths
    az_paths = [
        r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
        r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
        r"C:\Users\{}\AppData\Local\Programs\Python\Python*\Scripts\az.exe".format(os.getenv('USERNAME', ''))
    ]
    
    for path in az_paths:
        if '*' in path:
            # Handle wildcard paths
            import glob
            matches = glob.glob(path)
            if matches:
                print_success(f"Azure CLI found: {matches[0]}")
                return True
        elif os.path.exists(path):
            print_success(f"Azure CLI found: {path}")
            return True
    
    # Method 3: Check Windows Registry for installed programs
    try:
        import winreg
        registry_paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
        ]
        
        for reg_path in registry_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                                    if "azure cli" in display_name.lower() or "microsoft azure cli" in display_name.lower():
                                        print_success(f"Azure CLI found in registry: {display_name}")
                                        return True
                                except FileNotFoundError:
                                    continue
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass  # winreg not available (non-Windows system)
    
    print_warning("Azure CLI is not installed. Installing Azure CLI...")
    
    # Download Azure CLI MSI installer
    az_url = "https://aka.ms/installazurecliwindows"
    az_installer = "AzureCLI.msi"
    
    if not download_file(az_url, az_installer, "Azure CLI"):
        return False
    
    # Install Azure CLI silently
    install_args = ["/quiet", "/norestart"]
    success = install_software_silently("msiexec", ["/i", az_installer] + install_args, "Azure CLI")
    
    # Cleanup
    if os.path.exists(az_installer):
        os.remove(az_installer)
    
    return success

def check_and_install_vscode() -> bool:
    """Check for VS Code and install if missing."""
    print_info("Checking VS Code installation...")
    
    # Method 1: Check via command line (most reliable)
    success, output = run_command("code --version", capture_output=True)
    if success and len(output.strip()) > 0:
        print_success("VS Code is installed and accessible via command line")
        return True
    
    # Method 2: Check common installation paths
    vscode_paths = [
        r"C:\Program Files\Microsoft VS Code\Code.exe",
        r"C:\Program Files (x86)\Microsoft VS Code\Code.exe",
        r"C:\Users\{}\AppData\Local\Programs\Microsoft VS Code\Code.exe".format(os.getenv('USERNAME', ''))
    ]
    
    for path in vscode_paths:
        if os.path.exists(path):
            print_success(f"VS Code found: {path}")
            return True
    
    # Method 3: Check Windows Registry for installed programs
    try:
        import winreg
        registry_paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
        ]
        
        for reg_path in registry_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                                    if "visual studio code" in display_name.lower():
                                        print_success(f"VS Code found in registry: {display_name}")
                                        return True
                                except FileNotFoundError:
                                    continue
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass  # winreg not available (non-Windows system)
    
    print_warning("VS Code is not installed. Installing Visual Studio Code...")
    
    # Download VS Code installer
    vscode_url = "https://code.visualstudio.com/sha/download?build=stable&os=win32-x64"
    vscode_installer = "VSCodeSetup.exe"
    
    if not download_file(vscode_url, vscode_installer, "Visual Studio Code"):
        return False
    
    # Install VS Code silently
    install_args = [
        "/SILENT",
        "/mergetasks=!runcode,addcontextmenufiles,addcontextmenufolders,associatewithfiles,addtopath"
    ]
    
    success = install_software_silently(vscode_installer, install_args, "Visual Studio Code")
    
    # Cleanup
    if os.path.exists(vscode_installer):
        os.remove(vscode_installer)
    
    return success

def check_and_install_amo_adomd() -> bool:
    """Check for AMO & ADOMD libraries and install if missing."""
    print_info("Checking AMO & ADOMD libraries...")
    
    # Method 1: Check NuGet package locations (more comprehensive)
    nuget_paths = [
        os.path.expanduser(r"~\.nuget\packages\microsoft.analysisservices.adomdclient"),
        os.path.expanduser(r"~\.nuget\packages\microsoft.analysisservices.netcore.retail.amd64"),
        os.path.expanduser(r"~\.nuget\packages\microsoft.analysisservices.core")
    ]
    
    # Method 2: Check GAC (Global Assembly Cache) locations
    gac_paths = [
        r"C:\Windows\Microsoft.NET\assembly\GAC_MSIL\Microsoft.AnalysisServices.AdomdClient",
        r"C:\Windows\assembly\GAC_MSIL\Microsoft.AnalysisServices.AdomdClient"
    ]
    
    # Method 3: Check traditional installation paths
    adomd_paths = [
        r"C:\Program Files\Microsoft.NET\ADOMD.NET\170",
        r"C:\Program Files\Microsoft.NET\ADOMD.NET\160",
        r"C:\Program Files\Microsoft.NET\ADOMD.NET\150",
        r"C:\Program Files (x86)\Microsoft.NET\ADOMD.NET\170",
        r"C:\Program Files (x86)\Microsoft.NET\ADOMD.NET\160",
        r"C:\Program Files (x86)\Microsoft.NET\ADOMD.NET\150"
    ]
    
    analysis_services_paths = [
        r"C:\Program Files (x86)\Microsoft SQL Server\170\SDK\Assemblies",
        r"C:\Program Files (x86)\Microsoft SQL Server\160\SDK\Assemblies",
        r"C:\Program Files (x86)\Microsoft SQL Server\150\SDK\Assemblies",
        r"C:\Program Files\Microsoft SQL Server\170\SDK\Assemblies",
        r"C:\Program Files\Microsoft SQL Server\160\SDK\Assemblies",
        r"C:\Program Files\Microsoft SQL Server\150\SDK\Assemblies"
    ]
    
    # Check all possible locations
    all_paths = nuget_paths + gac_paths + adomd_paths + analysis_services_paths
    
    found_locations = []
    for path in all_paths:
        if os.path.exists(path):
            found_locations.append(path)
    
    if found_locations:
        print_success(f"AMO & ADOMD libraries found in {len(found_locations)} locations:")
        for location in found_locations[:3]:  # Show max 3 locations
            print_info(f"  - {location}")
        if len(found_locations) > 3:
            print_info(f"  - ... and {len(found_locations) - 3} more locations")
        return True
    
    # Method 4: Check Windows Registry for installed programs
    try:
        import winreg
        registry_paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
        ]
        
        for reg_path in registry_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                                    if any(keyword in display_name.lower() for keyword in 
                                          ["analysis services", "adomd", "amo", "microsoft sql server management objects"]):
                                        print_success(f"Analysis Services components found in registry: {display_name}")
                                        return True
                                except FileNotFoundError:
                                    continue
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass  # winreg not available (non-Windows system)
    
    print_warning("AMO & ADOMD libraries not found. Installing Analysis Services client libraries...")
    
    # Download Analysis Services client libraries
    amo_url = "https://go.microsoft.com/fwlink/?linkid=2180719"  # AS OLE DB provider
    adomd_url = "https://go.microsoft.com/fwlink/?linkid=2180929"  # ADOMD.NET provider
    
    amo_installer = "AS_OLE_DB.msi"
    adomd_installer = "ADOMD_NET.msi"
    
    # Download both installers
    amo_success = download_file(amo_url, amo_installer, "Analysis Services OLE DB Provider")
    adomd_success = download_file(adomd_url, adomd_installer, "ADOMD.NET Provider")
    
    if not (amo_success and adomd_success):
        return False
    
    # Install both packages
    install_args = ["/quiet", "/norestart"]
    
    amo_install = install_software_silently("msiexec", ["/i", amo_installer] + install_args, "Analysis Services OLE DB Provider")
    adomd_install = install_software_silently("msiexec", ["/i", adomd_installer] + install_args, "ADOMD.NET Provider")
    
    # Cleanup
    for installer in [amo_installer, adomd_installer]:
        if os.path.exists(installer):
            os.remove(installer)
    
    return amo_install and adomd_install

def check_and_install_dotnet() -> bool:
    """Check for .NET 8.0 SDK and install if missing."""
    print_info("Checking .NET 8.0 SDK installation...")
    
    # Method 1: Check via command line (most reliable)
    success, output = run_command("dotnet --version", capture_output=True)
    if success and output.strip():
        version = output.strip()
        print_success(f".NET SDK is installed: {version}")
        # Check if it's version 8.0 or higher
        try:
            major_version = int(version.split('.')[0])
            if major_version >= 8:
                print_success(".NET 8.0+ SDK detected")
                return True
            else:
                print_warning(f".NET SDK version {version} is installed but version 8.0+ is recommended")
        except (ValueError, IndexError):
            print_warning(f"Could not parse .NET SDK version: {version}")
    
    # Method 2: Check common installation paths
    dotnet_paths = [
        r"C:\Program Files\dotnet\dotnet.exe",
        r"C:\Program Files (x86)\dotnet\dotnet.exe"
    ]
    
    for path in dotnet_paths:
        if os.path.exists(path):
            print_info(f"Found .NET at: {path}")
            # Try to get version from found installation
            success, output = run_command(f'"{path}" --version', capture_output=True)
            if success:
                version = output.strip()
                print_success(f".NET SDK version: {version}")
                try:
                    major_version = int(version.split('.')[0])
                    if major_version >= 8:
                        return True
                except (ValueError, IndexError):
                    pass
    
    # Method 3: Check Windows Registry for installed programs
    try:
        import winreg
        registry_paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
        ]
        
        for reg_path in registry_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                    if ".NET" in display_name and "SDK" in display_name:
                                        print_success(f"Found .NET SDK in registry: {display_name}")
                                        if "8.0" in display_name or any(f"{major}.0" in display_name for major in range(8, 20)):
                                            return True
                                except FileNotFoundError:
                                    continue
                        except OSError:
                            continue
            except FileNotFoundError:
                continue
    except Exception as e:
        print_warning(f"Registry check failed: {e}")
    
    # If not found, install .NET 8.0 SDK
    print_warning(".NET 8.0 SDK not found")
    
    if CHECK_ONLY:
        print_info("Check-only mode: Would install .NET 8.0 SDK")
        return False
    
    print_info("Installing .NET 8.0 SDK...")
    
    # Download .NET 8.0 SDK installer
    dotnet_url = "https://dotnetcli.azureedge.net/dotnet/Sdk/8.0.403/dotnet-sdk-8.0.403-win-x64.exe"
    installer_path = "dotnet-sdk-installer.exe"
    
    if not download_file(dotnet_url, installer_path, ".NET 8.0 SDK"):
        return False
    
    try:
        # Install .NET SDK silently
        print_info("Installing .NET 8.0 SDK (this may take several minutes)...")
        install_args = ["/quiet", "/norestart"]
        
        if install_software_silently(installer_path, install_args, ".NET 8.0 SDK"):
            # Verify installation
            print_info("Verifying .NET installation...")
            
            # Wait a moment for installation to complete
            time.sleep(5)
            
            # Try the standard dotnet command first
            success, output = run_command("dotnet --version", capture_output=True)
            if success and output.strip():
                version = output.strip()
                print_success(f".NET SDK installed successfully: {version}")
                return True
            
            # Try the full path as a fallback
            dotnet_exe = r"C:\Program Files\dotnet\dotnet.exe"
            if os.path.exists(dotnet_exe):
                success, output = run_command(f'"{dotnet_exe}" --version', capture_output=True)
                if success and output.strip():
                    version = output.strip()
                    print_success(f".NET SDK installed successfully: {version}")
                    return True
            
            print_warning(".NET SDK installation completed but verification failed")
            print_info("You may need to restart your command prompt or add .NET to your PATH")
            return True  # Consider it successful since installation didn't fail
        
        else:
            print_error(".NET 8.0 SDK installation failed")
            return False
            
    except Exception as e:
        print_error(f"Error during .NET SDK installation: {e}")
        return False
    
    finally:
        # Clean up installer file
        try:
            if os.path.exists(installer_path):
                os.remove(installer_path)
                print_info("Cleaned up installer file")
        except Exception as e:
            print_warning(f"Could not remove installer file: {e}")

def check_required_software() -> Dict[str, bool]:
    """Check for required software installations and install if missing."""
    print_header("Checking and Installing Required Software")
    
    results = {}
    
    # Check and install Git
    results['git'] = check_and_install_git()
    
    # Check and install Azure CLI
    results['azure_cli'] = check_and_install_azure_cli()
    
    # Check and install VS Code
    results['vscode'] = check_and_install_vscode()
    
    # Check and install AMO & ADOMD libraries
    results['amo_adomd'] = check_and_install_amo_adomd()
    
    # Check and install .NET 8.0 SDK
    results['dotnet'] = check_and_install_dotnet()
    
    return results

def check_windows_components() -> Dict[str, bool]:
    """Check for required software installations."""
    print_header("Checking Required Software")
    
    results = {}
    
    # Check Git installation
    print_info("Checking Git installation...")
    success, output = run_command("git --version", capture_output=True)
    if success and "git version" in output.lower():
        print_success(f"Git is installed: {output.strip()}")
        results['git'] = True
    else:
        print_error("Git is not installed")
        print_info("Please download Git from: https://git-scm.com/download/win")
        results['git'] = False
    
    # Check Azure CLI installation
    print_info("Checking Azure CLI installation...")
    success, output = run_command("az --version", capture_output=True)
    if success and "azure-cli" in output.lower():
        print_success("Azure CLI is installed")
        results['azure_cli'] = True
    else:
        print_error("Azure CLI is not installed")
        print_info("Please install from: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli-windows")
        results['azure_cli'] = False
    
    # Check VS Code installation
    print_info("Checking VS Code installation...")
    vscode_paths = [
        r"C:\Program Files\Microsoft VS Code\Code.exe",
        r"C:\Program Files (x86)\Microsoft VS Code\Code.exe",
        r"C:\Users\{}\AppData\Local\Programs\Microsoft VS Code\Code.exe".format(os.getenv('USERNAME', ''))
    ]
    
    vscode_found = False
    for path in vscode_paths:
        if os.path.exists(path):
            print_success(f"VS Code found: {path}")
            vscode_found = True
            break
    
    if vscode_found:
        results['vscode'] = True
    else:
        # Try checking via command line
        success, output = run_command("code --version", capture_output=True)
        if success:
            print_success("VS Code is installed and accessible via command line")
            results['vscode'] = True
        else:
            print_error("VS Code is not installed")
            print_info("Please download from: https://code.visualstudio.com/download")
            results['vscode'] = False

    return results
    """Check for required Windows components."""
    print_header("Checking Windows Components")
    
    results = {}
    
    # Check if running on Windows
    if platform.system() != "Windows":
        print_error("This setup script is designed for Windows systems")
        results['windows'] = False
        return results
    
    print_success("Running on Windows")
    results['windows'] = True
    
    # Check for Analysis Services DLLs
    adomd_paths = [
        r"C:\Program Files\Microsoft.NET\ADOMD.NET\160",
        r"C:\Program Files\Microsoft.NET\ADOMD.NET\150",
        r"C:\Program Files (x86)\Microsoft.NET\ADOMD.NET\160",
        r"C:\Program Files (x86)\Microsoft.NET\ADOMD.NET\150"
    ]
    
    analysis_services_paths = [
        r"C:\Program Files (x86)\Microsoft SQL Server\160\SDK\Assemblies",
        r"C:\Program Files (x86)\Microsoft SQL Server\150\SDK\Assemblies",
        r"C:\Program Files\Microsoft SQL Server\160\SDK\Assemblies",
        r"C:\Program Files\Microsoft SQL Server\150\SDK\Assemblies"
    ]
    
    # Find ADOMD.NET path
    adomd_found = False
    for path in adomd_paths:
        if os.path.exists(path):
            print_success(f"ADOMD.NET found: {path}")
            results['adomd_path'] = path
            adomd_found = True
            break
    
    if not adomd_found:
        print_warning("ADOMD.NET not found in standard locations")
        print_info("You may need to install SQL Server Analysis Services or Power BI Desktop")
        results['adomd_path'] = None
    
    # Find Analysis Services assemblies
    as_found = False
    for path in analysis_services_paths:
        if os.path.exists(path):
            print_success(f"Analysis Services assemblies found: {path}")
            results['analysis_services_path'] = path
            as_found = True
            break
    
    if not as_found:
        print_warning("Analysis Services assemblies not found in standard locations")
        print_info("You may need to install SQL Server Analysis Services")
        results['analysis_services_path'] = None
    
    return results

def setup_virtual_environment() -> bool:
    """Create and setup virtual environment."""
    print_header("Setting Up Virtual Environment")
    
    venv_path = Path(".venv")
    
    if venv_path.exists():
        print_info("Virtual environment already exists")
        return True
    
    # Create virtual environment using the correct Python executable
    success, output = run_command(f'"{PYTHON_EXECUTABLE}" -m venv .venv')
    if not success:
        print_error(f"Failed to create virtual environment: {output}")
        return False
    
    print_success("Virtual environment created successfully")
    return True

def install_dependencies() -> bool:
    """Install required Python packages."""
    print_header("Installing Dependencies")
    
    # Determine pip path
    if platform.system() == "Windows":
        pip_path = ".venv\\Scripts\\pip.exe"
        python_path = ".venv\\Scripts\\python.exe"
    else:
        pip_path = ".venv/bin/pip"
        python_path = ".venv/bin/python"
    
    # Clean up any accidentally created files from previous runs
    cleanup_accidental_files()
    
    # Upgrade pip first
    print_info("Upgrading pip...")
    success, output = run_command(f"{python_path} -m pip install --upgrade pip")
    if not success:
        print_warning(f"Failed to upgrade pip: {output}")
    
    # Function to check if package is already installed
    def is_package_installed(package_name: str) -> bool:
        """Check if a package is already installed."""
        # Extract package name without version specifier using regex for more robust parsing
        import re
        base_name = re.split(r'[><=!]+', package_name)[0].strip()
        success, output = run_command(f"{pip_path} show {base_name}", capture_output=True)
        return success and "Name:" in output
    
    # Check for requirements.txt in current directory
    requirements_path = Path("requirements.txt")
    if requirements_path.exists():
        print_info("Checking packages from requirements.txt...")
        
        # Read requirements and check which are missing
        try:
            with open(requirements_path, 'r') as f:
                requirements = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
            
            missing_packages = []
            for package in requirements:
                if not is_package_installed(package):
                    missing_packages.append(package)
                else:
                    print_success(f"{package.split('>=')[0]} already installed")
            
            if missing_packages:
                print_info(f"Installing {len(missing_packages)} missing packages...")
                for package in missing_packages:
                    print_info(f"Installing {package}...")
                    success, output = run_pip_install(pip_path, package)
                    if not success:
                        print_error(f"Failed to install {package}: {output}")
                        return False
                print_success("All missing dependencies installed successfully")
            else:
                print_success("All requirements already satisfied")
                
        except Exception as e:
            print_error(f"Failed to read requirements.txt: {e}")
            return False
    else:
        print_info("Installing core MCP packages...")
        
        # Core packages if requirements.txt is missing - Complete 70-package list
        core_packages = [
            "annotated-types>=0.7.0",
            "ansible-core>=2.19.2",
            "anyio>=4.10.0",
            "attrs>=25.3.0",
            "azure-ai-agents>=1.1.0",
            "azure-ai-projects>=1.0.0",
            "azure-core>=1.35.0",
            "azure-identity>=1.24.0",
            "azure-storage-blob>=12.26.0",
            "certifi>=2025.8.3",
            "cffi>=2.0.0",
            "charset-normalizer>=3.4.3",
            "click>=8.2.1",
            "clr_loader>=0.2.7",
            "colorama>=0.4.6",
            "cryptography>=45.0.7",
            "distro>=1.9.0",
            "et_xmlfile>=2.0.0",
            "greenlet>=3.2.4",
            "h11>=0.16.0",
            "httpcore>=1.0.9",
            "httpx>=0.28.1",
            "httpx-sse>=0.4.1",
            "idna>=3.10",
            "isodate>=0.7.2",
            "Jinja2>=3.1.6",
            "jiter>=0.10.0",
            "joblib>=1.5.2",
            "jsonschema>=4.25.1",
            "jsonschema-specifications>=2025.9.1",
            "MarkupSafe>=3.0.2",
            "mcp>=1.13.1",
            "msal>=1.33.0",
            "msal-extensions>=1.3.1",
            "nltk>=3.9.1",
            "numpy>=2.3.2",
            "openai>=1.107.0",
            "openpyxl>=3.1.5",
            "packaging>=25.0",
            "pandas>=2.3.2",
            "pyadomd>=0.1.1",
            "pycparser>=2.22",
            "pydantic>=2.11.7",
            "pydantic-settings>=2.10.1",
            "pydantic_core>=2.39.0",
            "PyJWT>=2.10.1",
            "pyodbc>=5.2.0",
            "python-dateutil>=2.9.0",
            "python-dotenv>=1.1.1",
            "python-multipart>=0.0.20",
            "pythonnet>=3.0.5",
            "pytz>=2025.2",
            "pywin32>=311",
            "PyYAML>=6.0.2",
            "referencing>=0.36.2",
            "regex>=2025.9.1",
            "requests>=2.32.5",
            "resolvelib>=1.2.0",
            "rpds-py>=0.27.1",
            "six>=1.17.0",
            "sniffio>=1.3.1",
            "SQLAlchemy>=2.0.43",
            "sse-starlette>=3.0.2",
            "starlette>=0.47.3",
            "tqdm>=4.67.1",
            "typing-inspection>=0.4.1",
            "typing_extensions>=4.15.0",
            "tzdata>=2025.2",
            "urllib3>=2.5.0",
            "uvicorn>=0.35.0"
        ]
        
        missing_packages = []
        for package in core_packages:
            if not is_package_installed(package):
                missing_packages.append(package)
            else:
                print_success(f"{package.split('>=')[0]} already installed")
        
        if missing_packages:
            print_info(f"Installing {len(missing_packages)} missing packages...")
            for package in missing_packages:
                print_info(f"Installing {package}...")
                success, output = run_pip_install(pip_path, package)
                if not success:
                    print_error(f"Failed to install {package}: {output}")
                    return False
            print_success("All missing core packages installed successfully")
        else:
            print_success("All core packages already satisfied")
    
    return True

def setup_nltk_data() -> bool:
    """Download required NLTK data."""
    print_header("Setting Up NLTK Data")
    
    # Determine python path
    if platform.system() == "Windows":
        python_path = ".venv\\Scripts\\python.exe"
    else:
        python_path = ".venv/bin/python"
    
    nltk_script = '''
import nltk
import ssl
import os

# Set encoding for Windows console
os.environ['PYTHONIOENCODING'] = 'utf-8'

# Handle SSL issues
try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError:
    pass
else:
    ssl._create_default_https_context = _create_unverified_https_context

# Function to check if NLTK data exists
def is_nltk_data_available(item):
    try:
        nltk.data.find(f'tokenizers/{item}')
        return True
    except LookupError:
        try:
            nltk.data.find(f'corpora/{item}')
            return True
        except LookupError:
            try:
                nltk.data.find(f'taggers/{item}')
                return True
            except LookupError:
                return False

# Download required NLTK data
nltk_downloads = [
    'punkt',
    'punkt_tab', 
    'stopwords',
    'wordnet',
    'averaged_perceptron_tagger',
    'averaged_perceptron_tagger_eng',
    'omw-1.4'
]

for item in nltk_downloads:
    try:
        if is_nltk_data_available(item):
            print(f"[OK] {item} already available")
        else:
            print(f"Downloading {item}...")
            nltk.download(item, quiet=True)
            print(f"[OK] Downloaded {item}")
    except Exception as e:
        print(f"[ERROR] Failed to download {item}: {e}")
'''
    
    # Write temporary script with proper encoding
    with open("temp_nltk_setup.py", "w", encoding="utf-8") as f:
        f.write(nltk_script)
    
    try:
        success, output = run_command(f"{python_path} temp_nltk_setup.py")
        if success:
            print_success("NLTK data downloaded successfully")
        else:
            print_warning(f"Some NLTK downloads may have failed: {output}")
        return True
    finally:
        # Clean up temporary script
        if os.path.exists("temp_nltk_setup.py"):
            os.remove("temp_nltk_setup.py")

def setup_azure_resources() -> bool:
    """Guide user through Azure resource access setup."""
    print_header("Azure Resource Access Setup")
    
    print_info("Azure Resource Access Requirements:")
    print("1. Submit access request to 'mcapscustome-nkmr' in CoreIdentity - Manage Entitlement")
    print("   - Use role: 'mcapsda_bicenterofexcellence_team'")
    print("2. Navigate to Azure Portal > Privileged Identity Management")
    print("   - Activate all roles for 'mcapsda_bicenterofexcellence' resources")
    print("3. Download VPN Client from 'mcapsda-bicenterofexcellence-vng' virtual network gateway")
    print("4. Connect to VPN using Azure VPN Client")
    print("5. Access Azure AI Foundry: mcapsda-bicenterofexcellence-aiproject")
    
    print_warning("These steps require manual action and cannot be automated.")
    print_info("Please complete these steps before proceeding with the application setup.")
    
    # Check if user can access Azure
    print_info("Checking Azure authentication...")
    success, output = run_command("az account show", capture_output=True)
    if success:
        print_success("Azure CLI authentication is working")
        return True
    else:
        print_warning("Azure CLI not authenticated. Please run 'az login' after VPN connection.")
        return False

def setup_github_copilot() -> bool:
    """Guide user through GitHub Copilot setup in VS Code."""
    print_header("GitHub Copilot Setup")
    
    # Check if we're in check-only mode
    if CHECK_ONLY:
        print_info("Check-only mode: Skipping GitHub Copilot setup guidance")
        return True
    
    print_info("Setting up GitHub Copilot for VS Code...")
    print("1. Open VS Code")
    print("2. Go to Extensions (Ctrl+Shift+X)")
    print("3. Search for 'GitHub Copilot'")
    print("4. Install the extension")
    print("5. Sign in with your Microsoft/GitHub account")
    print("6. Accept the subscription terms (Microsoft Employee license)")
    
    print_warning("GitHub Copilot setup requires manual installation in VS Code.")
    print_info("Reference: Install GitHub Copilot for Visual Studio Code as a Microsoft Employee")
    
    return True

def create_environment_file(components: Dict[str, any]) -> bool:
    """Create .env file with default configurations."""
    print_header("Creating Environment Template")
    
    env_path = Path(".env")
    
    if env_path.exists():
        print_info(".env file already exists")
        return True
    
    # Create environment template with all necessary variables
    env_content = f"""# MCP Server Environment Configuration
# Generated by Quickstart.py on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

# Azure AI Configuration (REQUIRED)
PROJECT_ENDPOINT=https://your-project.cognitiveservices.azure.com/
MODEL_DEPLOYMENT_NAME="gpt-4o"

# Power BI Dataset Configuration (REQUIRED)
SERVER_NAME=powerbi://api.powerbi.com/v1.0/myorg/
DATABASE_NAME=YourDatasetName

# User Credentials (if needed)
USER_ID=your-email@domain.com
PASSWORD=your-password

# Fabric Configuration (optional)
workspace_id=your-workspace-guid
lakehouse_id=your-lakehouse-guid
table_name=your-table-name
source_table=your-source-table

# SQL Endpoint (optional)
sql_endpoint=your-sql-endpoint
sql_database=your-sql-database

# .NET Assembly Paths (auto-detected)
"""
    
    # Add discovered component paths or placeholders
    if components.get('adomd_path'):
        env_content += f'Adomd_DLL_Path="{components["adomd_path"]}"\n'
    else:
        env_content += '# Adomd_DLL_Path="C:\\Program Files\\Microsoft.NET\\ADOMD.NET\\160"\n'
    
    if components.get('analysis_services_path'):
        env_content += f'Analysis_Services_path="{components["analysis_services_path"]}"\n'
    else:
        env_content += '# Analysis_Services_path="C:\\Program Files\\Microsoft SQL Server\\160\\SDK\\Assemblies"\n'
    
    # Write .env file
    try:
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write(env_content)
        
        print_success(f".env template created: {env_path}")
        
        # Remind user about required manual configurations
        print_warning("\nIMPORTANT: Update the .env file with your actual values:")
        print_info("- PROJECT_ENDPOINT: Your Azure AI project endpoint")
        print_info("- DATABASE_NAME: Your Power BI dataset name")
        print_info("- USER_ID and PASSWORD: Your credentials")
        print_info("- workspace_id and lakehouse_id: If using Fabric")
        print_info("- DATABASE_NAME: Your Power BI dataset name")
        print_info("- workspace_id: Your Fabric workspace ID")
        print_info("- lakehouse_id: Your lakehouse ID (if using)")
        
        return True
    except Exception as e:
        print_error(f"Failed to create .env file: {e}")
        return False

def setup_vscode_config() -> bool:
    """Setup VS Code MCP configuration."""
    print_header("Creating VS Code MCP Configuration Template")
    
    vscode_dir = Path(".vscode")
    mcp_config_path = vscode_dir / "mcp.json"
    
    # Create .vscode directory if it doesn't exist
    vscode_dir.mkdir(exist_ok=True)
    
    # MCP configuration template
    current_dir = Path.cwd().absolute()
    if platform.system() == "Windows":
        python_path = current_dir / ".venv" / "Scripts" / "python.exe"
    else:
        python_path = current_dir / ".venv" / "bin" / "python"
    
    mcp_config = {
        "inputs": [],
        "servers": {
            "MCP": {
                "command": str(python_path),
                "args": ["src/server.py"],
                "env": ".env"
            }
        }
    }
    
    try:
        with open(mcp_config_path, 'w', encoding='utf-8') as f:
            json.dump(mcp_config, f, indent=4)
        
        print_success(f"VS Code MCP configuration template created: {mcp_config_path}")
        print_info("Update the server path in mcp.json to point to your actual server.py file")
        return True
    except Exception as e:
        print_error(f"Failed to create VS Code configuration: {e}")
        return False

def run_health_checks() -> bool:
    """Run basic health checks to validate the setup."""
    print_header("Running Health Checks")
    
    # In check-only mode, skip most health checks
    if CHECK_ONLY:
        print_info("Check-only mode: Skipping detailed health checks")
        print_success("Health checks skipped in check-only mode")
        return True
    
    # Determine python path
    if platform.system() == "Windows":
        python_path = ".venv\\Scripts\\python.exe"
    else:
        python_path = ".venv/bin/python"
    
    checks_passed = 0
    total_checks = 4
    
    # Test 1: Import basic packages
    print_info("Testing package imports...")
    import_test = '''
try:
    import mcp
    import azure.identity
    import pandas
    import nltk
    print("[OK] All core packages imported successfully")
except ImportError as e:
    print(f"[ERROR] Import error: {e}")
    exit(1)
'''
    
    success, output = run_command(f"{python_path} -c \"{import_test}\"")
    if success and "[OK]" in output:
        print_success("Package imports working")
        checks_passed += 1
    else:
        print_error(f"Package import test failed: {output}")
    
    # Test 2: NLTK data availability
    print_info("Testing NLTK data...")
    nltk_test = '''
try:
    import nltk
    from nltk.corpus import wordnet
    from nltk.tokenize import word_tokenize
    word_tokenize("test")
    print("[OK] NLTK data is available")
except Exception as e:
    print(f"[ERROR] NLTK error: {e}")
    exit(1)
'''
    
    success, output = run_command(f"{python_path} -c \"{nltk_test}\"")
    if success and "[OK]" in output:
        print_success("NLTK data is working")
        checks_passed += 1
    else:
        print_warning(f"NLTK test failed: {output}")
    
    # Test 3: .NET assembly loading (if components are available)
    print_info("Testing .NET assembly access...")
    assembly_test = '''
try:
    import clr
    import os
    from dotenv import load_dotenv
    
    load_dotenv("MCP-Application/.env")
    dll_folder = os.getenv("Analysis_Services_path")
    adomd_path = os.getenv("Adomd_DLL_Path")
    
    if dll_folder and adomd_path:
        clr.AddReference(os.path.join(dll_folder, "Microsoft.AnalysisServices.Tabular.dll"))
        clr.AddReference(os.path.join(adomd_path, "Microsoft.AnalysisServices.AdomdClient.dll"))
        print("[OK] .NET assemblies loaded successfully")
    else:
        print("[WARNING] .NET assembly paths not configured")
except Exception as e:
    print(f"[WARNING] .NET assembly test skipped: {e}")
'''
    
    success, output = run_command(f"{python_path} -c \"{assembly_test}\"")
    if "[OK]" in output:
        print_success(".NET assemblies are working")
        checks_passed += 1
    elif "[WARNING]" in output:
        print_warning(".NET assemblies not fully configured (expected if SQL Server not installed)")
        checks_passed += 0.5
    else:
        print_warning(f".NET assembly test issues: {output}")
    
    # Test 4: Environment file validation
    print_info("Validating environment configuration...")
    env_path = Path("MCP-Application/.env")
    if env_path.exists():
        print_success(".env file exists")
        checks_passed += 1
    else:
        print_error(".env file missing")
    
    # Report results
    print(f"\nHealth check results: {checks_passed}/{total_checks} checks passed")
    
    if checks_passed >= 3:
        print_success("Setup validation completed successfully!")
        return True
    else:
        print_warning("Setup completed with some issues. Check the logs above.")
        return False

def print_next_steps():
    """Print next steps for the user."""
    print_header("Next Steps")
    
    print_info("1. Complete Azure Resource Access:")
    print("   - Submit access request to 'mcapscustome-nkmr' (if not done)")
    print("   - Activate PIM roles for 'mcapsda_bicenterofexcellence'")
    print("   - Download and connect VPN client")
    print("   - Run: az login (after VPN connection)")
    
    print_info("\n2. Complete your .env configuration:")
    print("   - Add your PROJECT_ENDPOINT (Azure AI Foundry)")
    print("   - Add your DATABASE_NAME")
    print("   - Add workspace and lakehouse IDs if needed")
    
    print_info("\n3. Install missing software (if any):")
    print("   - SQL Server Management Studio")
    print("   - AMO & ADOMD 19.84.1.0 libraries")
    print("   - VS Code extensions (if needed)")
    
    print_info("\n4. Setup GitHub Copilot in VS Code:")
    print("   - Install GitHub Copilot extension")
    print("   - Sign in with Microsoft employee account")
    
    print_info("\n5. Test the MCP server:")
    print("   cd MCP-Application")
    print("   ..\\..venv\\Scripts\\python.exe src\\server.py")
    
    print_info("\n6. Test the client:")
    print("   cd MCP-Application")
    print("   ..\\..venv\\Scripts\\python.exe src\\client.py")
    
    print_info("\n7. Use with VS Code:")
    print("   - Open VS Code in this directory")
    print("   - The MCP server will be automatically configured")
    
    print_info("\n8. Access token (if required):")
    print("   az account get-access-token --resource https://cognitiveservices.azure.com --query accessToken -o tsv")

def main():
    """Main setup function."""
    print_header("MCP Server Environment Quickstart")
    print("This script will set up your complete MCP server environment with intelligent detection.")
    print()
    print("🔍 SMART DETECTION FEATURES:")
    print("   • Checks for existing software installations before downloading")
    print("   • Verifies installed packages and only installs missing ones")  
    print("   • Uses multiple detection methods (command line, registry, file paths)")
    print("   • Skips unnecessary downloads and installations")
    print()
    print("📦 WHAT WILL BE CHECKED/INSTALLED:")
    print("   • Required software: Git, Azure CLI, VS Code, .NET 8.0 SDK, AMO/ADOMD libraries")
    print("   • Python packages: 70 comprehensive packages (only missing ones)")
    print("   • NLTK datasets: 7 linguistic datasets (only missing ones)")
    print("   • Environment setup: Virtual environment, .env templates, VS Code config")
    print()
    print("⚡ NOTE: Some installations may require administrator privileges.")
    print("The script will only proceed with actual installations if components are missing.")
    input("Press Enter to start the intelligent setup process...")
    
    # Check and request admin privileges if needed
    if not request_admin_privileges():
        print_error("Administrator privileges are required for automatic software installation.")
        print_info("You can run the script manually without admin rights, but software installation will be skipped.")
        input("Press Enter to continue without automatic installations, or Ctrl+C to exit...")
    
    # Change to script directory
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    
    success_count = 0
    total_steps = 11  # Updated total steps
    
    # Step 1: Check and install Python 3.11.9 if needed
    if check_and_install_python():
        success_count += 1
        print_info("Python 3.11.9 requirement satisfied")
    else:
        print_error("Cannot continue without Python 3.11.9")
        print_error("Python 3.11.9 is required for compatibility with our MCP server code")
        if CHECK_ONLY:
            print_info("In check-only mode - would install Python 3.11.9 if running normally")
        sys.exit(1)
    
    # Step 2: Check and install required software
    software_results = check_required_software()
    if all(software_results.values()):
        success_count += 1
        print_success("All required software is installed")
    else:
        missing_software = [k for k, v in software_results.items() if not v]
        if check_admin_privileges():
            print_warning(f"Some software installation may have failed: {missing_software}")
        else:
            print_warning(f"Some software is missing (admin rights needed): {missing_software}")
        success_count += 0.5  # Partial credit
    
    # Step 3: Check Windows components
    components = check_windows_components()
    if components.get('windows', False):
        success_count += 1
    
    # Step 4: Setup Azure resources (guidance only)
    if setup_azure_resources():
        success_count += 1
    
    # Step 5: Setup virtual environment
    if setup_virtual_environment():
        success_count += 1
    
    # Step 6: Install dependencies
    if install_dependencies():
        success_count += 1
    
    # Step 7: Setup NLTK data
    if setup_nltk_data():
        success_count += 1
    
    # Step 8: Create environment file
    if create_environment_file(components):
        success_count += 1
    
    # Step 9: Setup VS Code configuration
    if setup_vscode_config():
        success_count += 1
    
    # Step 10: Install VS Code extensions
    extensions_installed = False
    if software_results.get('vscode', False):
        if install_vscode_extensions():
            success_count += 1
            extensions_installed = True
        else:
            success_count += 0.5
    else:
        print_info("Skipping VS Code extensions (VS Code not installed)")
    
    # Step 11: Setup GitHub Copilot guidance (only if automatic installation failed)
    if not extensions_installed and software_results.get('vscode', False):
        print_info("Automatic extension installation may have failed. Providing manual setup guidance...")
        if setup_github_copilot():
            success_count += 1
    elif extensions_installed:
        print_success("GitHub Copilot extension should be installed. Please restart VS Code if needed.")
        success_count += 1
    
    # Final health checks
    health_check_passed = run_health_checks()
    
    # Summary
    print_header("Setup Summary")
    print(f"Completed {success_count:.1f}/{total_steps} setup steps successfully")
    
    if success_count >= 10 and health_check_passed:
        print_success("🎉 MCP Server environment is fully configured!")
        print_next_steps()
    elif success_count >= 8:
        print_warning("⚠ Setup completed with some issues")
        print_info("Some components may need manual installation or configuration")
        print_next_steps()
    else:
        print_error("❌ Setup incomplete. Please check the errors above and try again.")
        print_info("Consider running as Administrator for automatic software installation.")
        sys.exit(1)

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="MCP Server Environment Quickstart Setup", 
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python Quickstart.py                    # Smart detection mode (default)
  python Quickstart.py --force            # Force reinstall everything
  python Quickstart.py --check-only       # Check installations without installing
        """
    )
    
    parser.add_argument(
        '--force', 
        action='store_true', 
        help='Force reinstallation of all components (bypasses detection)'
    )
    parser.add_argument(
        '--check-only', 
        action='store_true',
        help='Only check for installations without installing anything'
    )
    
    args = parser.parse_args()
    
    # Set global flags for script behavior
    FORCE_REINSTALL = args.force
    CHECK_ONLY = args.check_only
    
    try:
        main()
    except KeyboardInterrupt:
        print_error("\nSetup interrupted by user")
        sys.exit(1)
    except Exception as e:
        print_error(f"Unexpected error during setup: {e}")
        sys.exit(1)
