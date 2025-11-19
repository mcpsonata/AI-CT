import os
import sys
import json
import logging
import inspect
import datetime
from datetime import timedelta
import tiktoken
import time
from typing import Dict, Any, List, Optional, Union, Callable
import asyncio
import threading
import queue
import signal
import atexit
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, redirect, url_for, session as flask_session
from flask_cors import CORS
from azure.identity import AzureCliCredential
from openai import AzureOpenAI
from dotenv import load_dotenv
import pandas as pd
import difflib
import re
import io
from prompt_manager import PromptManager
from streaming import StreamingManager
from enhanced_streaming import EnhancedStreamingManager
from process_streamer import ProcessStreamManager, ProcessStreamer
from log_streamer import LogCapture
from session_security import session_security, require_valid_session, session_isolation_check, secure_chat_endpoint
from auth_manager import MSALAuthManager, require_auth
from security_headers import add_security_headers

# Create a queue for workflow updates
workflow_updates_queue = {}

# Add parent directory to path to import from src
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from src.server import SQLEndpoint, Fabric, TabularEditor, CopilotDataEvaluator
    from src.authentication_manager import AuthenticationManager
    from src.semantic_model_config_manager import SemanticModelConfigManager
    print("✅ Successfully imported all required classes")
except ImportError as e:
    print(f"❌ Error importing classes: {e}")
    print(f"Current working directory: {os.getcwd()}")
    print(f"Python path includes: {sys.path[-1]}")
    
    # Check if the files exist
    import os
    server_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "server.py")
    auth_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "authentication_manager.py")
    print(f"server.py exists: {os.path.exists(server_path)}")
    print(f"authentication_manager.py exists: {os.path.exists(auth_path)}")
    
    # Re-raise the error to stop execution
    raise e

# Session-aware logging handler that isolates logs per session
class SessionAwareWebLogHandler(logging.Handler):
    """Session-aware log handler that only streams logs to the correct session"""
    def __init__(self, workflow_updates_queue_dict):
        super().__init__()
        self.workflow_updates_queue_dict = workflow_updates_queue_dict  # session_id -> queue mapping
        self._progress_session_map = {}  # tracker_session -> webapp_session mapping
        
    def register_progress_session(self, tracker_session_id: str, webapp_session_id: str):
        """Register a mapping between progress tracker session and webapp session"""
        self._progress_session_map[tracker_session_id] = webapp_session_id
        
    def emit(self, record):
        """Emit a log record only to the appropriate session's queue"""
        try:
            # CRITICAL: Prevent infinite loop by excluding streaming module logs
            if record.name.startswith('streaming') or record.name == '__main__':
                return
            
            # Get session context using our webapp session context function
            current_session_id = get_webapp_session_context()
            
            # Debug logging for ui_progress_tracker
            if record.name.startswith('ui_progress_tracker.'):
                print(f"🐛 DEBUG: ui_progress_tracker log - logger: {record.name}, webapp_session: {current_session_id}, thread: {threading.current_thread().name}")
            
            # If no webapp session context, try thread attribute
            if not current_session_id:
                current_session_id = getattr(threading.current_thread(), 'session_id', None)
            
            # If no session context, try to extract from record if available
            if not current_session_id and hasattr(record, 'session_id'):
                current_session_id = record.session_id
            
            # Special handling for ui_progress_tracker loggers - map to webapp session
            if not current_session_id and record.name.startswith('ui_progress_tracker.'):
                # Extract the progress tracker session ID from logger name
                # Format: ui_progress_tracker.columns_analysis_1761176825
                tracker_session_parts = record.name.split('.')
                if len(tracker_session_parts) > 1:
                    tracker_session_id = tracker_session_parts[1]  # e.g., "columns_analysis_1761176825"
                    
                    # Check if we have this tracker mapped to a webapp session
                    if tracker_session_id in self._progress_session_map:
                        current_session_id = self._progress_session_map[tracker_session_id]
                        print(f"✅ Found mapping: '{tracker_session_id}' -> webapp '{current_session_id}'")
                    else:
                        print(f"⚠️  No mapping found for progress tracker '{tracker_session_id}' - logs will not be streamed")
                        print(f"📋 Available mappings: {list(self._progress_session_map.keys())}")
                        return
            
            # If still no session, don't stream (no session context)
            if not current_session_id:
                return
                
            # Only stream if we have a queue for this session
            if current_session_id not in self.workflow_updates_queue_dict:
                return
                
            from streaming import StreamingManager
            log_message = self.format(record)
            
            # Create log data for streaming
            log_data = {
                "message": log_message,
                "timestamp": int(time.time() * 1000),
                "stream_type": "detailed_log",
                "session_id": current_session_id,
                "level": record.levelname,
                "logger_name": record.name
            }
            
            # Stream the log to the correct session only
            StreamingManager.send_update(
                self.workflow_updates_queue_dict[current_session_id],
                log_data,
                "detailed_logs"
            )
        except Exception:
            pass  # Silently fail to avoid log recursion


# Thread-local session context for webapp
import threading
_webapp_session_context = threading.local()

def set_webapp_session_context(session_id: str):
    """Set the current session ID in thread-local storage for the webapp"""
    _webapp_session_context.session_id = session_id
    # Also set on thread for compatibility
    threading.current_thread().session_id = session_id

def get_webapp_session_context() -> str:
    """Get the current session ID from thread-local storage for the webapp"""
    return getattr(_webapp_session_context, 'session_id', None) or getattr(threading.current_thread(), 'session_id', None)

# Set up session-aware logging - configure for all modules
session_aware_log_handler = None
try:
    logging.basicConfig(
        level=logging.INFO, 
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        force=True  # This ensures we override any existing logging configuration
    )
    logger = logging.getLogger(__name__)

    # Create session-aware web streaming handler (will be initialized later with workflow_updates_queue)
    # This will be set up in the main initialization after workflow_updates_queue is created
    
    # For now, just ensure the loggers have the right levels
    logging.getLogger('src.server').setLevel(logging.INFO)
    logging.getLogger('src').setLevel(logging.INFO)
except Exception as e:
    print(f"Logging setup failed: {e}")
    logger = logging.getLogger(__name__)

# Reduce streaming module log level to avoid spam
logging.getLogger('streaming').setLevel(logging.WARNING)

# Load environment variables from root folder
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

# PowerBI MCP Server wrapper
class PowerBIMCPServer:
    """Wrapper for the MCP Server specific to Power BI operations."""
    def __init__(self):
        try:
            print("🔧 Initializing PowerBIMCPServer...")
            logger.info("Initializing PowerBIMCPServer...")
            from mcp.server import Server
            from mcp.server.models import InitializationOptions
            from mcp.types import ListToolsRequest, ListToolsResult, Tool, ServerResult
            
            # Configure the server
            from mcp.server.models import ServerCapabilities
            
            # Create proper ServerCapabilities instance
            capabilities = ServerCapabilities()
            
            options = InitializationOptions(
                tool_namespace="mcp",
                notification_options=None,
                server_name="Power BI MCP Server",
                server_version="1.0.0",
                capabilities=capabilities
            )
            
            # Create the server instance
            self.server = Server(options)
            
            # Add ListToolsRequest handler directly
            if hasattr(self.server, 'request_handlers') and ListToolsRequest not in self.server.request_handlers:
                logger.info("Adding ListToolsRequest handler to MCP server...")
                
                # Define the handler function
                async def list_tools_handler(request: ListToolsRequest) -> ServerResult:
                    """Handle ListToolsRequest by returning available tools."""
                    logger.info("ListToolsRequest handler called")
                    
                    # Define tools based on our available services
                    tools = [
                        Tool(
                            name="tabulareditor_connect_dataset",
                            description="Connect to a Power BI dataset in a workspace",
                            inputSchema={
                                "type": "object",
                                "properties": {
                                    "workspace_identifier": {"type": "string", "description": "The workspace ID or name"},
                                    "database_name": {"type": "string", "description": "The dataset name"}
                                },
                                "required": ["workspace_identifier", "database_name"]
                            }
                        ),
                        Tool(
                            name="tabulareditor_list_tables",
                            description="List all tables in the connected dataset",
                            inputSchema={
                                "type": "object",
                                "properties": {},
                                "required": []
                            }
                        ),
                        Tool(
                            name="tabulareditor_execute_dax_query",
                            description="Execute a DAX query against the connected dataset",
                            inputSchema={
                                "type": "object",
                                "properties": {
                                    "dax_query": {"type": "string", "description": "The DAX query to execute"}
                                },
                                "required": ["dax_query"]
                            }
                        ),
                        Tool(
                            name="sqlendpoint_execute_sql_query",
                            description="Execute SQL query on the connected SQL endpoint",
                            inputSchema={
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "SQL query to execute"}
                                },
                                "required": ["query"]
                            }
                        )
                    ]
                    
                    # Create and return the result
                    result = ListToolsResult(tools=tools)
                    return ServerResult(root=result)
                
                # Register the handler
                self.server.request_handlers[ListToolsRequest] = list_tools_handler
                logger.info("ListToolsRequest handler registered successfully")
                
                # Log all registered handlers
                handler_names = [handler_type.__name__ for handler_type in self.server.request_handlers]
                logger.info(f"Registered handlers: {', '.join(handler_names)}")
            else:
                logger.info("ListToolsRequest handler already exists or request_handlers not found")
                
            logger.info("PowerBIMCPServer initialized successfully")
            
        except ImportError as e:
            logger.error(f"Failed to import MCP modules: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize PowerBIMCPServer: {e}")
            raise

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# SECURITY: Configure Flask to work behind Azure Front Door proxy
# This ensures Flask generates correct HTTPS URLs even though it receives HTTP requests
# WARNING: Only enable ProxyFix if your ACI is NOT directly exposed to the internet
# Azure Front Door must be the only way to access your application
from werkzeug.middleware.proxy_fix import ProxyFix

# Verify we're in a proxy environment before trusting proxy headers
is_behind_proxy = os.getenv('BEHIND_PROXY', 'true').lower() == 'true'

if is_behind_proxy:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,      # Trust 1 proxy for X-Forwarded-For
        x_proto=1,    # Trust 1 proxy for X-Forwarded-Proto (https)
        x_host=1,     # Trust 1 proxy for X-Forwarded-Host
        x_port=1,     # Trust 1 proxy for X-Forwarded-Port
        x_prefix=1    # Trust 1 proxy for X-Forwarded-Prefix
    )
    logger.info("✅ ProxyFix middleware enabled - trusting Azure Front Door headers")
else:
    logger.warning("⚠️ ProxyFix disabled - running without proxy (development mode)")

# Set preferred URL scheme from environment (defaults to https for production)
app.config['PREFERRED_URL_SCHEME'] = os.getenv('PREFERRED_URL_SCHEME', 'https')

# Add security headers to all responses
add_security_headers(app)

# Initialize MSAL Authentication (this sets up session config)
auth_manager = MSALAuthManager(app)

# Initialize Flask-Session for proper session management (after config is set)
from flask_session import Session
Session(app)

# Get Azure OpenAI credentials from environment variables
azure_endpoint = os.getenv("PROJECT_ENDPOINT")
azure_deployment = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o")
api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

# Initialize Azure OpenAI client - will be set up using authentication manager
client = None
encoding = None

def initialize_azure_openai():
    """Initialize Azure OpenAI client using the shared authentication manager."""
    global client, encoding
    try:
        # Use the shared authentication manager's OpenAI client
        if tool_manager and tool_manager.auth_manager:
            client = tool_manager.auth_manager.get_openai_client()
            logger.info(f"Successfully initialized Azure OpenAI client using shared AuthenticationManager for endpoint {azure_endpoint}")
        else:
            logger.warning("Shared AuthenticationManager not available, cannot initialize Azure OpenAI client")
            client = None
        
        # Initialize the tiktoken encoder for GPT-4o
        try:
            # For Azure OpenAI, we need to use the cl100k_base encoding for GPT-4o
            encoding = tiktoken.get_encoding("cl100k_base")
            logger.info("Successfully initialized tiktoken encoder for token counting")
        except Exception as e:
            logger.error(f"Error initializing tiktoken encoder: {e}")
            encoding = None
            
    except Exception as e:
        logger.error(f"Error initializing Azure OpenAI client: {e}")
        client = None
        encoding = None
        # Don't raise here - let the app start without Azure OpenAI if needed

class MCPToolManager:
    """Manager for MCP tools that dynamically loads methods from different classes."""
    
    def __init__(self):
        """Initialize the tool manager with all necessary components."""
        self.available_tools = {}
        self.auth_manager = None  # Shared auth manager is OK - handles token management
        # REMOVED SHARED INSTANCES - these will be created per session:
        # self.sql_endpoint = None
        # self.fabric = None  
        # self.tabular_editor = None
        # self.copilot_evaluator = None
        self.mcp_server = None
        
        # Track connection state - MOVED TO SESSION-BASED
        # SECURITY FIX: No longer use global connection state
        # self.connected_dataset = None  # REMOVED - was security vulnerability
        # self.connected_workspace = None  # REMOVED - was security vulnerability
        
        # Track session-specific data for stop functionality AND connections
        self.session_data = {}
        
        # SESSION-ISOLATED connection state - each session has its own connections
        self.session_connections = {}  # session_id -> {"dataset": "name", "workspace": "name"}
        
        # SESSION-ISOLATED instances - each session gets its own connection objects
        self.session_instances = {}  # session_id -> {"tabular_editor": obj, "sql_endpoint": obj, "fabric": obj, "evaluator": obj}
        
        # CRITICAL FIX: Track active connections per session to prevent COM conflicts
        self.active_com_connections = {}  # session_id -> {"workspace": str, "dataset": str, "tabular_editor_id": str}
        
        # Thread-safe stop flags for each session
        self.stop_flags = {}
        
        # Cancellation events for each session - proper threading mechanism
        self.cancellation_events = {}
        
        # Track running processes per session
        self.running_processes = {}
        
        # Initialize enhanced streaming manager for Run All coordination
        self.enhanced_streaming = EnhancedStreamingManager()
        
        # Initialize step by step to avoid hanging
        self._initialize_components()
    
    def _initialize_components(self):
        """Initialize components step by step with proper error handling."""
        try:
            # Import the authentication manager directly
            from src.authentication_manager import AuthenticationManager
            
            logger.info("Initializing shared authentication manager...")
            # Create a single shared authentication manager instance for token management
            self.auth_manager = AuthenticationManager()
            logger.info("✅ Shared authentication manager initialized successfully")
            
            # Initialize semantic model configuration manager (replaces database config manager)
            logger.info("Initializing semantic model configuration manager...")
            self.db_config_manager = SemanticModelConfigManager(self.auth_manager)
            logger.info("✅ Semantic model configuration manager initialized successfully")
 
            # SESSION-ISOLATED: Don't create shared instances here anymore
            # Individual instances will be created per session when needed
            logger.info("✅ Component initialization completed - session instances will be created on-demand")
            
            try:
                logger.info("Initializing MCP server...")
                # Initialize MCP server connection
                from mcp.server import Server
                from mcp.types import ServerResult
                self.mcp_server = PowerBIMCPServer()
                logger.info("✅ MCP server initialized successfully")
            except Exception as mcp_err:
                logger.warning(f"Failed to initialize MCP server: {mcp_err}")
                self.mcp_server = None
                
            # Now that core components are ready, initialize Azure OpenAI
            logger.info("Initializing Azure OpenAI...")
            initialize_azure_openai()
            
            # Load available tools
            self._load_available_tools()
            logger.info("🎉 MCPToolManager initialization completed successfully!")
            
        except Exception as e:
            logger.error(f"Failed to initialize core components: {e}")
            raise RuntimeError(f"MCPToolManager initialization failed: {e}")
    
    def get_session_instances(self, session_id: str) -> Dict[str, Any]:
        """Get or create session-specific instances for tabular editor, SQL endpoint, etc."""
        if not session_id:
            raise ValueError("Session ID is required for session isolation")
            
        # If session instances don't exist, create them
        if session_id not in self.session_instances:
            logger.info(f"Creating new session instances for session: {session_id}")
            
            try:
                from src.server import SQLEndpoint, Fabric, TabularEditor
                from src.server import CopilotDataEvaluator
                
                # Create session-specific instances with shared auth manager
                session_sql_endpoint = SQLEndpoint(auth_manager=self.auth_manager)
                session_fabric = Fabric(auth_manager=self.auth_manager)
                session_tabular_editor = TabularEditor(
                    fabric_instance=session_fabric,
                    sql_endpoint_instance=session_sql_endpoint,
                    auth_manager=self.auth_manager
                )
                session_evaluator = CopilotDataEvaluator(session_tabular_editor, session_sql_endpoint)
                
                # Create session-specific log handler for progress tracking
                session_log_handler = None
                global workflow_updates_queue
                if workflow_updates_queue and session_id in workflow_updates_queue:
                    # Create a handler that only manages this specific session
                    session_queue_dict = {session_id: workflow_updates_queue[session_id]}
                    session_log_handler = SessionAwareWebLogHandler(session_queue_dict)
                    session_log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
                
                # Store session instances
                self.session_instances[session_id] = {
                    "sql_endpoint": session_sql_endpoint,
                    "fabric": session_fabric,
                    "tabular_editor": session_tabular_editor,
                    "evaluator": session_evaluator,
                    "session_id": session_id,
                    "log_handler": session_log_handler  # Session-specific handler
                }
                
                # Initialize session-aware logging for UI Progress Tracker
                try:
                    from src.ui_progress_tracker import initialize_session_logging
                    initialize_session_logging(session_id, workflow_updates_queue)
                except ImportError:
                    logger.warning("Could not import ui_progress_tracker session logging")
                
                logger.info(f"✅ Created isolated instances for session: {session_id}")
                
            except Exception as e:
                logger.error(f"Failed to create session instances for {session_id}: {e}")
                raise
                
        return self.session_instances[session_id]
    
    def _cleanup_conflicting_connections(self, connecting_session_id: str, workspace: str, dataset: str):
        """
        Clean up connections from other sessions that might interfere with the new connection.
        This is necessary due to potential COM object sharing issues.
        """
        try:
            sessions_to_cleanup = []
            
            # Find sessions that have different connections
            for session_id, connection_info in self.active_com_connections.items():
                if session_id != connecting_session_id:
                    # FIXED: Only clean up sessions connected to the SAME dataset
                    # Sessions should be able to connect to different datasets simultaneously
                    if (connection_info.get("workspace") == workspace and 
                        connection_info.get("dataset") == dataset):
                        sessions_to_cleanup.append(session_id)
                        logger.info(f"🔍 Found conflicting session {session_id} connected to same dataset: {workspace} -> {dataset}")
            
            # Clean up conflicting sessions
            for session_id in sessions_to_cleanup:
                logger.info(f"🧹 Cleaning up conflicting connection for session {session_id} to allow {connecting_session_id} to connect")
                try:
                    # Disconnect the session's tabular editor
                    if session_id in self.session_instances and "tabular_editor" in self.session_instances[session_id]:
                        tabular_editor = self.session_instances[session_id]["tabular_editor"]
                        if hasattr(tabular_editor, 'disconnect_dataset'):
                            tabular_editor.disconnect_dataset()
                            logger.info(f"✅ Disconnected TabularEditor for session {session_id}")
                    
                    # Clear session connection state from BOTH systems
                    if session_id in self.session_connections:
                        del self.session_connections[session_id]
                        
                    # SYNC: Also clear from global tool_manager.session_connections
                    global tool_manager
                    if tool_manager and hasattr(tool_manager, 'session_connections'):
                        if session_id in tool_manager.session_connections:
                            del tool_manager.session_connections[session_id]
                            
                    if session_id in self.active_com_connections:
                        del self.active_com_connections[session_id]
                        
                except Exception as e:
                    logger.warning(f"⚠️ Error cleaning up session {session_id}: {e}")
            
            if sessions_to_cleanup:
                logger.info(f"🧹 Cleaned up {len(sessions_to_cleanup)} conflicting sessions for new connection")
                
        except Exception as e:
            logger.error(f"❌ Error during connection cleanup: {e}")
    
    def _tool_needs_session_instance(self, tool_name: str) -> bool:
        """Check if a tool needs session-specific instances."""
        session_dependent_tools = [
            # TabularEditor tools
            "tabulareditor_connect_dataset",
            "tabulareditor_disconnect_dataset", 
            "tabulareditor_list_tables",
            "tabulareditor_execute_dax_query",
            "tabulareditor_list_table_columns",
            "tabulareditor_get_column_properties",
            "tabulareditor_get_measure_properties",
            "tabulareditor_get_table_properties",
            "tabulareditor_list_all_relationships",
            "tabulareditor_check_date_table_exists",
            "tabulareditor_generate_tmsl_columns",
            "tabulareditor_select_tables_with_schema",
            "tabulareditor_get_multiple_sql_tables_schema",
            "tabulareditor__generate_erd_diagram",
            
            # SQL Endpoint tools
            "sqlendpoint_initialize_sql_connection",
            "sqlendpoint_get_sql_tables",
            "sqlendpoint_execute_sql_query",
            "sqlendpoint_get_sql_table_schema",
            "sql_endpoint_initialize_connection",
            "sql_endpoint_get_tables",
            "sql_endpoint_execute_query",
            
            # Fabric tools
            "fabric_get_workspace_info",
            "fabric_get_lakehouse_info",
            "fabric_refresh_sql_endpoint",
            
            # CopilotDataEvaluator tools
            "copilotdataevaluator_dataset_columns_analysis",
            "copilotdataevaluator_evaluate_fact_dimension_analysis",
            "copilotdataevaluator_evaluate_metadata_analysis",
            "copilotdataevaluator_evaluate_table_linking",
            "copilotdataevaluator_evaluate_table_hierarchies",
            "copilotdataevaluator_evaluate_measures_analysis",
            "copilotdataevaluator_evaluate_security_compliance",
            "copilot_dataset_columns_analysis",
            "copilot_evaluator_evaluate_fact_dimension_analysis",
            "copilot_evaluator_evaluate_metadata_analysis",
            "copilot_evaluator_evaluate_table_linking",
            "copilot_evaluator_evaluate_table_hierarchies",
            "copilot_evaluator_evaluate_measures_analysis"
            "copilot_evaluator_evaluate_security_compliance",
        ]
        return tool_name in session_dependent_tools
    
    def _get_session_tool_function(self, tool_name: str, original_function, session_instances: Dict[str, Any]):
        """Get a session-specific version of a tool function."""
        
        # Map tool names to session instance methods
        if tool_name.startswith("tabulareditor_"):
            tabular_editor = session_instances["tabular_editor"]
            method_name = tool_name.replace("tabulareditor_", "")
            if hasattr(tabular_editor, method_name):
                return getattr(tabular_editor, method_name)
        
        elif tool_name.startswith("sqlendpoint_"):
            sql_endpoint = session_instances["sql_endpoint"]
            method_name = tool_name.replace("sqlendpoint_", "")
            if hasattr(sql_endpoint, method_name):
                return getattr(sql_endpoint, method_name)
        
        elif tool_name.startswith("sql_endpoint_"):
            sql_endpoint = session_instances["sql_endpoint"]
            method_name = tool_name.replace("sql_endpoint_", "")
            if hasattr(sql_endpoint, method_name):
                return getattr(sql_endpoint, method_name)
        
        elif tool_name.startswith("copilotdataevaluator_"):
            evaluator = session_instances["evaluator"]
            method_name = tool_name.replace("copilotdataevaluator_", "")
            
            # Handle alias for evaluate_measures_analysis -> Evaluate_Copilot_Measures
            if method_name == "evaluate_measures_analysis":
                method_name = "Evaluate_Copilot_Measures"
            
            # Set session_id on the evaluator instance so it can be used by progress tracking
            evaluator.session_id = session_instances.get("session_id", "unknown")
                
            if hasattr(evaluator, method_name):
                return getattr(evaluator, method_name)
        
        elif tool_name.startswith("copilot_evaluator_"):
            evaluator = session_instances["evaluator"]
            method_name = tool_name.replace("copilot_evaluator_", "")
            if hasattr(evaluator, method_name):
                return getattr(evaluator, method_name)
        
        elif tool_name.startswith("fabric_"):
            fabric = session_instances["fabric"]
            method_name = tool_name.replace("fabric_", "")
            if hasattr(fabric, method_name):
                return getattr(fabric, method_name)
        
        # Fallback to original function
        return original_function
    
    def cleanup_session(self, session_id: str):
        """Clean up session-specific instances and data."""
        try:
            # Disconnect any active connections for this session
            if session_id in self.session_instances:
                session_instances = self.session_instances[session_id]
                
                # Try to disconnect tabular editor
                if "tabular_editor" in session_instances:
                    try:
                        tabular_editor = session_instances["tabular_editor"]
                        if hasattr(tabular_editor, 'connected') and tabular_editor.connected:
                            tabular_editor.disconnect_dataset()
                            logger.info(f"Disconnected tabular editor for session {session_id}")
                    except Exception as e:
                        logger.warning(f"Error disconnecting tabular editor for session {session_id}: {e}")
                
                # Clean up session instances
                del self.session_instances[session_id]
                logger.info(f"Cleaned up session instances for {session_id}")
            
            # Clean up session connections from BOTH systems
            if session_id in self.session_connections:
                del self.session_connections[session_id]
                
            # SYNC: Also clear from global tool_manager.session_connections
            global tool_manager
            if tool_manager and hasattr(tool_manager, 'session_connections'):
                if session_id in tool_manager.session_connections:
                    del tool_manager.session_connections[session_id]
                
            # Clean up other session data
            if session_id in self.stop_flags:
                del self.stop_flags[session_id]
            if session_id in self.cancellation_events:
                del self.cancellation_events[session_id]
            if session_id in self.running_processes:
                del self.running_processes[session_id]
                
            logger.info(f"✅ Successfully cleaned up all data for session {session_id}")
            
        except Exception as e:
            logger.error(f"Error cleaning up session {session_id}: {e}")
    
    def _load_available_tools(self):
        """Load all available tools after initialization."""
        try:
            self._initialize_tools()
            logger.info(f"MCP Tool Manager initialized with {len(self.available_tools)} tools")
        except Exception as e:
            logger.error(f"Error loading tools: {e}")
    
    def _initialize_tools(self) -> Dict[str, Dict]:
        """Initialize available MCP tools by fetching them dynamically from the MCP server."""
        try:
            # Get tools directly from TabularEditor server
            tools_from_server = False #self._fetch_tools_from_mcp_server()
            if tools_from_server:
                logger.info(f"Successfully loaded {len(tools_from_server)} tools from MCP server")
                return tools_from_server
            else:
                # Fallback to class method extraction if server method fails
                logger.warning("Failed to fetch tools from MCP server, falling back to class method extraction")
                return self._extract_tools_from_classes()
        except Exception as e:
            logger.error(f"Error initializing tools from server: {e}")
            # Fallback to class method extraction
            logger.info("Falling back to class method extraction")
            return self._extract_tools_from_classes()
    
    def _fetch_tools_from_mcp_server(self) -> Dict[str, Dict]:
        """Fetch available tools directly from the MCP server."""
        try:
            logger.info("Attempting to fetch tools from MCP server...")
            
            # Create a PowerBIMCPServer instance to access MCP tools
            try:
                if not self.mcp_server:
                    self.mcp_server = PowerBIMCPServer()
                
                # Access the MCP server's request handlers
                if hasattr(self.mcp_server, 'server') and hasattr(self.mcp_server.server, 'request_handlers'):
                    request_handlers = self.mcp_server.server.request_handlers
                    
                    # Look for ListToolsRequest handler
                    from mcp.types import ListToolsRequest
                    if ListToolsRequest in request_handlers:
                        # Get the handler function
                        handler = request_handlers[ListToolsRequest]
                        logger.info("Found ListToolsRequest handler")
                        
                        try:
                            # Call the handler with an empty ListToolsRequest
                            import asyncio
                            from mcp.types import ListToolsRequest
                            
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            try:
                                # Create a proper request object with required fields
                                request = ListToolsRequest(method="tools/list")
                                
                                # Call the handler
                                result = loop.run_until_complete(handler(request))
                                
                                # Extract tools from the result
                                # Get the actual ListToolsResult from ServerResult
                                actual_result = result.root if hasattr(result, 'root') else result
                                
                                if hasattr(actual_result, 'tools'):
                                    tools_list = actual_result.tools
                                    
                                    # Convert MCP Tool objects to our dictionary format
                                    tools_dict = {}
                                    for tool in tools_list:
                                        tools_dict[tool.name] = {
                                            "description": tool.description,
                                            "parameters": self._convert_schema_to_params(tool.inputSchema),
                                            "required_params": tool.inputSchema.get("required", []) if tool.inputSchema else [],
                                            "optional_params": [param for param in tool.inputSchema.get("properties", {}) 
                                                              if param not in tool.inputSchema.get("required", [])] if tool.inputSchema else [],
                                            "source_class": "MCP_Server"
                                        }
                                    
                                    if tools_dict:
                                        logger.info(f"Successfully loaded {len(tools_dict)} tools from MCP server")
                                        self.available_tools = tools_dict
                                        return tools_dict
                                    else:
                                        logger.warning("MCP server returned empty tools list")
                                        return {}
                                else:
                                    logger.warning(f"Handler result doesn't have tools attribute: {actual_result}")
                                    return {}
                                    
                            finally:
                                if not loop.is_closed():
                                    loop.close()
                                    
                        except Exception as e:
                            logger.warning(f"Failed to call ListToolsRequest handler: {e}")
                            return {}
                    else:
                        logger.warning("MCP server doesn't have ListToolsRequest handler")
                        return {}
                else:
                    logger.warning("PowerBIMCPServer doesn't have expected request_handlers")
                    return {}
                    
            except Exception as e:
                logger.warning(f"Failed to create PowerBIMCPServer instance: {e}")
                return {}
            
        except Exception as e:
            logger.warning(f"Could not fetch tools from MCP server: {e}")
            return {}
    
    def _convert_schema_to_params(self, input_schema: Dict) -> Dict[str, str]:
        """Convert JSON schema to parameter descriptions."""
        if not input_schema or "properties" not in input_schema:
            return {}
        
        params = {}
        properties = input_schema.get("properties", {})
        required = input_schema.get("required", [])
        
        for param_name, param_info in properties.items():
            param_type = param_info.get("type", "string")
            param_desc = param_info.get("description", f"{param_type} parameter")
            
            # Add required indicator
            if param_name in required:
                params[param_name] = f"{param_type} - {param_desc} (required)"
            else:
                params[param_name] = f"{param_type} - {param_desc} (optional)"
        
        return params
    
    def _extract_tools_from_classes(self) -> Dict[str, Dict]:
        """Extract tools from class methods as a fallback when server method fails."""
        try:
            # Collect tools from all available classes
            logger.info("Initializing tools from all available classes...")
            
            # SESSION-ISOLATED: Create temporary instances just for tool discovery
            # These are only used to extract method signatures, not for actual execution
            try:
                logger.info("Creating temporary instances for tool discovery...")
                from src.server import SQLEndpoint, Fabric, TabularEditor, CopilotDataEvaluator
                
                # Create temporary instances for tool discovery
                temp_sql_endpoint = SQLEndpoint(auth_manager=self.auth_manager)
                logger.info("✅ Created temporary SQLEndpoint")
                
                temp_fabric = Fabric(auth_manager=self.auth_manager)
                logger.info("✅ Created temporary Fabric")
                
                temp_tabular_editor = TabularEditor(
                    fabric_instance=temp_fabric,
                    sql_endpoint_instance=temp_sql_endpoint,
                    auth_manager=self.auth_manager
                )
                logger.info("✅ Created temporary TabularEditor")
                
                temp_evaluator = CopilotDataEvaluator(temp_tabular_editor, temp_sql_endpoint)
                logger.info("✅ Created temporary CopilotDataEvaluator")
                
                # Load tools from SQLEndpoint
                logger.info("Extracting tools from SQLEndpoint...")
                self._extract_class_methods(
                    temp_sql_endpoint, 
                    "SQLEndpoint", 
                    ["initialize_sql_connection", "get_sql_tables", "execute_sql_query", "get_sql_table_schema"]
                )
                
                # Load tools from Fabric
                logger.info("Extracting tools from Fabric...")
                self._extract_class_methods(
                    temp_fabric,
                    "Fabric",
                    ["get_workspace_info", "get_lakehouse_info", "refresh_sql_endpoint"]
                )
                
                # Load tools from TabularEditor
                logger.info("Extracting tools from TabularEditor...")
                self._extract_class_methods(
                    temp_tabular_editor,
                    "TabularEditor",
                    [
                        "connect_dataset", "disconnect_dataset", "list_tables", 
                        "list_table_columns", "get_multiple_sql_tables_schema",
                        "generate_tmsl_columns", "select_tables_with_schema", 
                        "execute_dax_query", "check_date_table_exists", 
                        "get_column_properties", "get_measure_properties",
                        "get_table_properties", "list_all_relationships",
                        "_generate_erd_diagram"
                    ]
                )
                
                # Load tools from CopilotDataEvaluator
                logger.info("Extracting tools from CopilotDataEvaluator...")
                self._extract_class_methods(
                    temp_evaluator,
                    "CopilotDataEvaluator",
                    [
                        "dataset_columns_analysis", "evaluate_fact_dimension_analysis", "evaluate_metadata_analysis", "evaluate_table_linking","evaluate_table_hierarchies","Evaluate_Copilot_Measures","evaluate_security_compliance"
                    ]
                )
                
                logger.info(f"✅ Successfully loaded tools using temporary instances for discovery. Total tools: {len(self.available_tools)}")
                
            except Exception as tool_loading_error:
                logger.error(f"❌ Failed to load tools during temporary instance creation: {tool_loading_error}")
                import traceback
                logger.error(f"Full traceback: {traceback.format_exc()}")
            
            logger.info(f"Completed tool extraction. Final tool count: {len(self.available_tools)}")
            return self.available_tools
            
        except Exception as e:
            logger.error(f"❌ Error initializing tools from classes: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return {}
    
    def _extract_class_methods(self, obj: object, class_name: str, method_names: List[str]) -> None:
        """Extract methods from a class instance and convert them to tools."""
        for method_name in method_names:
            if hasattr(obj, method_name) and callable(getattr(obj, method_name)):
                method = getattr(obj, method_name)
                
                # Skip if it starts with underscore (except explicitly included)
                if method_name.startswith('_') and method_name not in [
                    "_generate_erd_diagram"
                ]:
                    continue
                
                # Get method signature
                try:
                    signature = inspect.signature(method)
                    
                    # Get parameter info
                    parameters = {}
                    required_params = []
                    optional_params = []
                    
                    for param_name, param in signature.parameters.items():
                        # Skip 'self' parameter
                        if param_name == 'self':
                            continue
                            
                        param_info = {
                            "type": str(param.annotation).replace("<class '", "").replace("'>", ""),
                            "default": None if param.default is inspect.Parameter.empty else param.default,
                        }
                        
                        parameters[param_name] = param_info
                        
                        # Determine if parameter is required
                        if param.default is inspect.Parameter.empty:
                            required_params.append(param_name)
                        else:
                            optional_params.append(param_name)
                    
                    # Get method docstring
                    docstring = inspect.getdoc(method) or f"Execute {method_name} on {class_name}"
                    
                    # Add method as a tool
                    tool_name = f"{class_name.lower()}_{method_name}"
                    tool_data = {
                        "description": docstring,
                        "method": method,
                        "parameters": parameters,
                        "required_params": required_params,
                        "optional_params": optional_params,
                        "source_class": class_name,
                        "function": self._create_wrapper_function(obj, method)
                    }
                    
                    self.available_tools[tool_name] = tool_data
                    
                    # Add alias for Evaluate_Copilot_Measures -> evaluate_measures_analysis
                    if method_name == "Evaluate_Copilot_Measures":
                        alias_name = f"{class_name.lower()}_evaluate_measures_analysis"
                        self.available_tools[alias_name] = tool_data.copy()
                    
                except Exception as e:
                    logger.warning(f"Error extracting method {method_name} from {class_name}: {e}")
    
    def _create_wrapper_function(self, obj: object, method: Callable) -> Callable:
        """Create a wrapper function for a method that handles arguments properly and captures output."""
        def wrapper(**kwargs):
            try:
                # Import here to avoid circular imports
                import sys
                import io
                from contextlib import redirect_stdout, redirect_stderr
                
                # Filter out parameters that don't match the method signature
                signature = inspect.signature(method)
                valid_params = {}
                for param_name, param_value in kwargs.items():
                    if param_name in signature.parameters:
                        valid_params[param_name] = param_value
                    else:
                        logger.warning(f"Ignoring invalid parameter '{param_name}' for method {method.__name__}")
                
                # Get current session for streaming (if available)
                current_session_id = getattr(wrapper, '_session_id', None)
                
                if current_session_id and current_session_id in workflow_updates_queue:
                    # Create string buffers to capture output
                    stdout_buffer = io.StringIO()
                    stderr_buffer = io.StringIO()
                    
                    # Capture stdout and stderr during method execution
                    with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
                        result = method(**valid_params)
                    
                    # Get the captured output
                    stdout_content = stdout_buffer.getvalue().strip()
                    stderr_content = stderr_buffer.getvalue().strip()
                    
                    # Stream stdout if there's content
                    if stdout_content:
                        for line in stdout_content.split('\n'):
                            if line.strip():
                                StreamingManager.send_update(
                                    workflow_updates_queue[current_session_id],
                                    {
                                        "message": line.strip(),
                                        "stream_type": "process_stdout",
                                        "session_id": current_session_id,
                                        "tool_name": method.__name__
                                    },
                                    "process_output"
                                )
                    
                    # Stream stderr if there's content
                    if stderr_content:
                        for line in stderr_content.split('\n'):
                            if line.strip():
                                StreamingManager.send_update(
                                    workflow_updates_queue[current_session_id],
                                    {
                                        "message": line.strip(),
                                        "stream_type": "process_stderr",
                                        "session_id": current_session_id,
                                        "tool_name": method.__name__
                                    },
                                    "process_output"
                                )
                    
                    return result
                else:
                    # No streaming session, execute normally
                    return method(**valid_params)
                    
            except Exception as e:
                logger.error(f"Error executing {method.__name__}: {e}")
                
                # Stream the error if we have a session
                current_session_id = getattr(wrapper, '_session_id', None)
                if current_session_id and current_session_id in workflow_updates_queue:
                    StreamingManager.send_update(
                        workflow_updates_queue[current_session_id],
                        {
                            "message": f"Error in {method.__name__}: {str(e)}",
                            "stream_type": "command_error",
                            "session_id": current_session_id,
                            "tool_name": method.__name__
                        },
                        "process_output"
                    )
                raise e
        return wrapper
    
    def refresh_available_tools(self) -> bool:
        """Refresh the available tools from the MCP server."""
        try:
            logger.info("Refreshing available tools from MCP server...")
            new_tools = self._fetch_tools_from_mcp_server()
            
            if new_tools and len(new_tools) > len(self.available_tools):
                self.available_tools = new_tools
                logger.info(f"Successfully refreshed tools. Now have {len(self.available_tools)} tools available")
                return True
            elif new_tools:
                self.available_tools = new_tools
                logger.info(f"Tools refreshed. {len(self.available_tools)} tools available")
                return True
            else:
                logger.warning("Failed to refresh tools from MCP server")
                return False
                
        except Exception as e:
            logger.error(f"Error refreshing tools: {e}")
            return False
    
    def get_tool_count(self) -> int:
        """Get the current number of available tools."""
        return len(self.available_tools)
    
    def get_tools_description(self) -> str:
        """Get a formatted description of available tools for AI context."""
        tools_desc = "Available Power BI and Fabric tools:\n\n"
        for tool_name, tool_info in self.available_tools.items():
            tools_desc += f"🔧 {tool_name}:\n"
            tools_desc += f"   Description: {tool_info['description']}\n"
            if tool_info.get('parameters'):
                tools_desc += "   Parameters:\n"
                for param, desc in tool_info['parameters'].items():
                    tools_desc += f"     - {param}: {desc}\n"
            tools_desc += "\n"
        return tools_desc
    
    def _format_tool_result(self, tool_name: str, result: Any) -> str:
        """Format tool result for presentation to the user."""
        try:
            if isinstance(result, (dict, list)):
                formatted_result = f"## Results from {tool_name}\n\n```json\n{json.dumps(result, indent=2, default=str)}\n```"
            elif isinstance(result, str) and (result.startswith('{') or result.startswith('[')):
                # Try to parse as JSON for better formatting
                try:
                    json_data = json.loads(result)
                    formatted_result = f"## Results from {tool_name}\n\n```json\n{json.dumps(json_data, indent=2, default=str)}\n```"
                except:
                    formatted_result = f"## Results from {tool_name}\n\n{result}"
            else:
                formatted_result = f"## Results from {tool_name}\n\n{result}"
            
            return formatted_result
        except Exception as e:
            logger.error(f"Error formatting tool result: {e}")
            return f"Results from {tool_name}: {str(result)}"
    
    def execute_tool(self, tool_name: str, parameters: Dict[str, Any], session_id: str = None) -> Dict[str, Any]:
        """Execute a specific MCP tool with parameters through the PowerBIMCPServer."""
        try:
            # Check if this session should stop execution
            if session_id and self.should_stop_session(session_id):
                logger.info(f"Tool execution stopped due to session stop flag: {session_id}")
                return {
                    "success": False,
                    "error": "Analysis stopped by user",
                    "stopped": True
                }
            
            # Create cancellation event for this session if it doesn't exist
            if session_id and session_id not in self.cancellation_events:
                self.cancellation_events[session_id] = threading.Event()
                # Event is NOT set initially (False = not cancelled)
            
            # Set session context for this thread to enable session-aware logging
            if session_id:
                set_webapp_session_context(session_id)
            
            print(f"🔧 Executing tool: {tool_name} with parameters: {parameters}")
            logger.info(f"Executing tool: {tool_name} with parameters: {parameters}")
            
            # If this is an analysis tool, register the expected progress tracker session
            if tool_name.startswith('copilotdataevaluator_') and session_id:
                # Generate the expected progress tracker session ID based on tool type
                import time
                current_time = int(time.time())  # seconds to match server.py
                
                # Map tool names to progress tracker prefixes
                tracker_prefixes = {
                    'copilotdataevaluator_dataset_columns_analysis': 'columns_analysis',
                    'copilotdataevaluator_evaluate_fact_dimension_analysis': 'fact_dimension_analysis', 
                    'copilotdataevaluator_evaluate_metadata_analysis': 'metadata_analysis',
                    'copilotdataevaluator_evaluate_table_linking': 'relationships_analysis',
                    'copilotdataevaluator_evaluate_table_hierarchies': 'hierarchies_analysis',
                    'copilotdataevaluator_evaluate_measures_analysis': 'measures_analysis',
                    'copilotdataevaluator_evaluate_security_compliance': 'security_analysis'
                }
                
                tracker_prefix = tracker_prefixes.get(tool_name)
                if tracker_prefix:
                    # The progress tracker will create a session ID like: columns_analysis_1761177701
                    # We need to register this mapping before the tool starts so logs can flow
                    expected_tracker_session = f"{tracker_prefix}_{current_time}"
                    
                    # Get session-specific log handler for better isolation
                    session_instances = self.get_session_instances(session_id) if session_id else None
                    session_log_handler = session_instances.get("log_handler") if session_instances else None
                    
                    # Register in session-specific handler (if available)
                    if session_log_handler:
                        session_log_handler.register_progress_session(expected_tracker_session, session_id)
                        print(f"🔄 Pre-registered progress tracker session '{expected_tracker_session}' -> webapp session '{session_id}' (session-specific handler)")
                    
                    # ALWAYS also register in global handler (this is what actually handles the logs)
                    global session_aware_log_handler
                    if session_aware_log_handler:
                        session_aware_log_handler.register_progress_session(expected_tracker_session, session_id)
                        print(f"🔄 Pre-registered progress tracker session '{expected_tracker_session}' -> webapp session '{session_id}' (global handler)")

            if tool_name not in self.available_tools:
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' not found. Available tools: {list(self.available_tools.keys())}"
                }

            # Fallback to direct method call - SESSION-ISOLATED version
            tool_info = self.available_tools[tool_name]
            
            # Check for required parameters
            missing_params = [param for param in tool_info.get('required_params', []) 
                             if param not in parameters]
            
            if missing_params:
                return {
                    "success": False,
                    "error": f"Missing required parameters: {', '.join(missing_params)}"
                }

            # SESSION-ISOLATED: Always use session-specific instances for tools that need them
            if session_id and self._tool_needs_session_instance(tool_name):
                session_instances = self.get_session_instances(session_id)
                
                # Get the correct session-specific method
                session_tool_function = self._get_session_tool_function(tool_name, tool_info.get('function'), session_instances)
                
                # Execute with cancellation support using session-specific instance
                result = self._execute_with_cancellation(
                    session_tool_function, 
                    parameters, 
                    session_id
                )
                
                # Handle CSV downloads if present in result
                if session_id and result and isinstance(result, dict):
                    self._handle_csv_download(result, session_id)
                
                # Update session connection state if this was a connect operation
                # Note: connect_dataset returns None on success, not a dict with success=True
                if tool_name == "tabulareditor_connect_dataset":
                    workspace = parameters.get("workspace_identifier")
                    dataset = parameters.get("database_name")
                    
                    # DISABLED: Session isolation - let each session maintain its own connection
                    # self._cleanup_conflicting_connections(session_id, workspace, dataset)
                    logger.info(f"✅ Session {session_id} connecting to {workspace} -> {dataset} without interfering with other sessions")
                    
                    # Update local session connections (for workflow tracking)
                    if session_id not in self.session_connections:
                        self.session_connections[session_id] = {}
                    self.session_connections[session_id]["workspace"] = workspace
                    self.session_connections[session_id]["dataset"] = dataset
                    
                    # SYNC: Also update global tool_manager.session_connections (for analysis validation)
                    global tool_manager
                    if tool_manager:
                        if not hasattr(tool_manager, 'session_connections'):
                            tool_manager.session_connections = {}
                        if session_id not in tool_manager.session_connections:
                            tool_manager.session_connections[session_id] = {}
                        tool_manager.session_connections[session_id]["workspace"] = workspace
                        tool_manager.session_connections[session_id]["dataset"] = dataset
                    
                    logger.info(f"✅ Updated session connection state in BOTH systems for {session_id}: {workspace} -> {dataset}")
                    
                    # Track active COM connection
                    tabular_editor_id = None
                    if session_id in self.session_instances and "tabular_editor" in self.session_instances[session_id]:
                        tabular_editor_id = getattr(self.session_instances[session_id]["tabular_editor"], '_instance_id', 'unknown')
                    
                    self.active_com_connections[session_id] = {
                        "workspace": workspace,
                        "dataset": dataset,
                        "tabular_editor_id": tabular_editor_id
                    }
                    
                    print(f"✅ Successfully connected session {session_id} to dataset: {dataset} in workspace: {workspace}")
                    logger.info(f"Session {session_id} connected: workspace={workspace}, dataset={dataset}, COM_instance={tabular_editor_id}")
                    
                    # Trigger UI connection status refresh by sending a special update
                    if session_id:
                        self._send_connection_status_update(session_id, workspace, dataset)
                
                return {
                    "success": True,
                    "result": result
                }
            
            # For non-session-dependent tools, use the original function
            elif 'function' in tool_info:
                # Set session_id on the wrapper function so it can stream output
                if session_id:
                    tool_info['function']._session_id = session_id
                
                # Execute with cancellation support
                result = self._execute_with_cancellation(
                    tool_info['function'], 
                    parameters, 
                    session_id
                )
                
                # Handle CSV downloads if present in result
                if session_id and result and isinstance(result, dict):
                    self._handle_csv_download(result, session_id)
                
                return {
                    "success": True,
                    "result": result
                }
            else:
                return {
                    "success": False,
                    "error": "Tool function not available"
                }
                
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}")
            return {"success": False, "error": str(e)}
        finally:
            # Clear session context to avoid memory leaks and prevent cross-session contamination
            if hasattr(threading.current_thread(), 'session_id'):
                delattr(threading.current_thread(), 'session_id')
            if hasattr(_webapp_session_context, 'session_id'):
                delattr(_webapp_session_context, 'session_id')

    def _execute_with_cancellation(self, function: Callable, parameters: Dict[str, Any], session_id: str = None) -> Any:
        """Execute a function with cancellation support."""
        try:
            # Set cancellation event on the function's object if it supports it
            if hasattr(function, '__self__'):
                func_obj = function.__self__
                if session_id and session_id in self.cancellation_events:
                    # Set cancellation event for various naming conventions
                    setattr(func_obj, '_cancellation_event', self.cancellation_events[session_id])
                    setattr(func_obj, '_should_stop', self.should_stop_session(session_id))
            
            # Check if the function accepts progress_callback parameter
            import inspect
            signature = inspect.signature(function)
            if 'progress_callback' in signature.parameters and session_id:
                # Create a progress callback that sends updates to the UI
                def progress_callback(message: str):
                    if session_id in workflow_updates_queue:
                        StreamingManager.send_update(
                            workflow_updates_queue[session_id],
                            {
                                "message": message,
                                "stream_type": "progress_update",
                                "session_id": session_id,
                                "tool_name": function.__name__
                            },
                            "progress_update"
                        )
                
                # Add progress_callback to parameters
                parameters = parameters.copy()  # Don't modify original
                parameters['progress_callback'] = progress_callback
            
            # Execute the function
            return function(**parameters)
            
        except Exception as e:
            # Check if this was a cancellation
            if session_id and self.should_stop_session(session_id):
                logger.info(f"Function execution cancelled for session {session_id}")
                return {"error": "Analysis stopped by user", "cancelled": True}
            else:
                raise e

    def auto_detect_and_execute_tools(self, user_message: str, session_id: str = None) -> List[Dict[str, Any]]:
        """
        Automatically detect and execute appropriate tools based on user message.
        Uses GPT-4o to understand the user's intent and extract relevant parameters.
        Returns a list of executed tool results.
        """
        results = []
        
        try:
            # Create a structured list of available tools for GPT-4o
            tools_info = []
            for tool_name, tool_info in self.available_tools.items():
                tool_data = {
                    "name": tool_name,
                    "description": tool_info["description"],
                    "required_params": tool_info["required_params"],
                    "optional_params": tool_info["optional_params"],
                    "source_class": tool_info["source_class"]
                }
                tools_info.append(tool_data)
            
            # Current connection state - SESSION-ISOLATED
            session_connection = self.session_connections.get(session_id, {})
            connection_state = {
                "connected_workspace": session_connection.get("workspace"),
                "connected_dataset": session_connection.get("dataset")
            }
            
            # Create the prompt for GPT-4o to analyze the user message
            tools_info_str = json.dumps(tools_info, indent=2)
            connection_state_str = json.dumps(connection_state, indent=2)
            
            messages = [
                {"role": "system", "content": PromptManager.get_tool_detection_prompt(tools_info_str, connection_state_str)},
                {"role": "user", "content": user_message}
            ]
            
            # Call Azure OpenAI to analyze the message
            response = client.chat.completions.create(
                model=azure_deployment,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
                max_tokens=500
            )
            
            tool_analysis = json.loads(response.choices[0].message.content)
            logger.info(f"Tool analysis: {tool_analysis}")
            
            # Handle both response formats from GPT-4
            tools_to_process = []
            
            # Check for new format: {"tool": "tool_name", "parameters": {...}}
            if "tool" in tool_analysis and "parameters" in tool_analysis:
                logger.info("Detected Tool Execution Response format")
                tools_to_process.append({
                    "tool_name": tool_analysis["tool"],
                    "params": tool_analysis["parameters"],
                    "confidence": 0.9,  # Default high confidence for direct format
                    "explanation": f"Tool execution requested: {tool_analysis['tool']}"
                })
            
            # Check for original format: {"tools_to_execute": [...]}
            elif "tools_to_execute" in tool_analysis and tool_analysis["tools_to_execute"]:
                logger.info("Detected Tool Detection Response format")
                for tool_execution in tool_analysis["tools_to_execute"]:
                    tools_to_process.append({
                        "tool_name": tool_execution.get("tool_name"),
                        "params": tool_execution.get("params", {}),
                        "confidence": tool_execution.get("confidence", 0),
                        "explanation": tool_execution.get("explanation", "")
                    })
            
            else:
                logger.warning(f"No valid tool format detected. Available keys: {list(tool_analysis.keys())}")
                return results
            
            # Execute the tools based on the analysis
            for tool_execution in tools_to_process:
                tool_name = tool_execution.get("tool_name")
                params = tool_execution.get("params", {})
                confidence = tool_execution.get("confidence", 0)
                explanation = tool_execution.get("explanation", "")
                
                logger.info(f"Processing tool: {tool_name}, confidence: {confidence}")
                
                # Only execute if we have sufficient confidence (0.6 or higher)
                if tool_name in self.available_tools and confidence >= 0.6:
                    logger.info(f"Auto-executing tool: {tool_name} with params: {params}")
                    logger.info(f"Reason: {explanation} (confidence: {confidence})")
                    
                    # Execute the tool
                    execution_result = self.execute_tool(tool_name, params, session_id)
                    
                    # Update SESSION connection state if this was a connect_dataset call
                    # Note: connect_dataset returns None on success (no exception = success)
                    if tool_name == "tabulareditor_connect_dataset":
                        workspace = params.get("workspace_identifier")
                        dataset = params.get("database_name")
                        
                        # Update local session connections (for workflow tracking)
                        if session_id not in self.session_connections:
                            self.session_connections[session_id] = {}
                        self.session_connections[session_id]["workspace"] = workspace
                        self.session_connections[session_id]["dataset"] = dataset
                        
                        # SYNC: Also update global tool_manager.session_connections (for analysis validation)
                        global tool_manager
                        if tool_manager:
                            if not hasattr(tool_manager, 'session_connections'):
                                tool_manager.session_connections = {}
                            if session_id not in tool_manager.session_connections:
                                tool_manager.session_connections[session_id] = {}
                            tool_manager.session_connections[session_id]["workspace"] = workspace
                            tool_manager.session_connections[session_id]["dataset"] = dataset
                        
                        logger.info(f"✅ Session {session_id} connection state updated in BOTH systems: {workspace} -> {dataset}")
                    
                    # Add to results
                    results.append({
                        "tool_name": tool_name,
                        "params": params,
                        "confidence": confidence,
                        "explanation": explanation,
                        "result": execution_result
                    })
                else:
                    logger.warning(f"Tool {tool_name} not executed - confidence: {confidence}, in_available_tools: {tool_name in self.available_tools}")
            
            return results
            
        except Exception as e:
            logger.error(f"Error in GPT-based tool detection: {e}")
            return []

    def execute_autonomous_workflow(self, user_message: str, session_id: str = None, generate_final_synthesis: bool = True, workflow_context: str = "top_level") -> Dict[str, Any]:
        """
        Execute complete autonomous workflow without requiring user 'continue' prompts.
        Plans and executes all necessary tools in sequence to fully answer the user's question.
        Streams intermediary results in real-time if session_id is provided.
        Args:
            user_message: The user's request
            session_id: Session ID for streaming updates
            generate_final_synthesis: Whether to generate synthesis completion message (default True for top-level workflows)
            workflow_context: Context identifier (top_level, sub_analysis, parallel_analysis)
 
        """
        # Set session context for this thread to enable session-aware logging
        if session_id:
            set_webapp_session_context(session_id)
        
         # CHECK IF THIS IS AN ANALYSIS REQUEST AND REQUIRES CONNECTION
        analysis_keywords = [
            "analyze", "analysis", "evaluate", "column", "fact", "dimension", "table", 
            "linking", "hierarchy", "measure", "security", "run all", "assessment", 
            "check", "review", "audit", "compliance", "quality"
        ]
        
        is_analysis_request = any(keyword in user_message.lower() for keyword in analysis_keywords)
        
        if is_analysis_request and session_id:
            # Check if we have an active dataset connection for analysis requests
            # Check local session_connections first (updated by workflow), then global as fallback
            session_connection = self.session_connections.get(session_id, {})
            if not session_connection.get("dataset"):
                # Fallback: check global tool_manager.session_connections
                global tool_manager
                if tool_manager and hasattr(tool_manager, 'session_connections'):
                    session_connection = tool_manager.session_connections.get(session_id, {})
            
            if not session_connection.get("dataset"):
                # Check if this is a connection request - these should bypass all validation
                connection_keywords = ["connect to", "connection", "connect dataset", "connect to power bi"]
                is_connection_request = any(keyword in user_message.lower() for keyword in connection_keywords)
                
                if is_connection_request:
                    logger.info(f"🔌 Connection request detected - bypassing validation to allow connection")
                    # Skip all validation and proceed to execute the connection request
                    pass
                else:
                    # Not a connection request - validate that a connection exists
                    # Check if this is a stale session (frontend persisted but backend restarted)
                    stale_session_detected = session_id and len(self.session_connections) == 0 and len(getattr(tool_manager, 'session_connections', {})) == 0
                    
                    if stale_session_detected:
                        logger.warning(f"🔄 STALE SESSION DETECTED - session {session_id} exists but no backend connections (likely backend restart)")
                        return {
                            "status": "connection_required", 
                            "message": "🔄 **Session Reconnection Required**\n\nYour session was disconnected (likely due to server restart). Please reconnect to your Power BI dataset using the connection panel above, then try your analysis again.",
                            "thought_process": [],
                            "workflow_results": [],
                            "requires_connection": True,
                            "session_id": session_id,
                            "total_time": 0.0,
                            "stale_session": True
                        }
                    
                    # Regular validation failure - no connection and not a connection request
                    logger.warning(f"⚠️ ANALYSIS VALIDATION FAILED - no dataset connection for session {session_id}")
                    logger.error(f"� DETAILED Analysis validation: user_message='{user_message}', session_id='{session_id}'")
                    logger.error(f"❌ DETAILED self.session_connections = {self.session_connections}")
                    logger.error(f"� DETAILED tool_manager.session_connections = {getattr(tool_manager, 'session_connections', {}) if tool_manager else 'N/A'}")
                    logger.error(f"� DETAILED session_connection retrieved = {session_connection}")
                    logger.error(f"� DETAILED Available session IDs in self: {list(self.session_connections.keys())}")
                    logger.error(f"� DETAILED Available session IDs in global: {list(getattr(tool_manager, 'session_connections', {}).keys()) if tool_manager else 'N/A'}")
                    logger.error(f"� DETAILED Requested session ID: '{session_id}' (type: {type(session_id)}, len: {len(session_id) if session_id else 0})")
                    
                    # Enhanced debugging - check if session ID exists with different formatting
                    for sid in self.session_connections.keys():
                        logger.error(f"❌ DETAILED Self comparing '{session_id}' == '{sid}': {session_id == sid} (types: {type(session_id)} vs {type(sid)})")
                    
                    # Enhanced debugging for global connections
                    global_connections = getattr(tool_manager, 'session_connections', {}) if tool_manager else {}
                    for sid in global_connections.keys():
                        logger.error(f"� DETAILED Global comparing '{session_id}' == '{sid}': {session_id == sid} (types: {type(session_id)} vs {type(sid)})")
                    
                    logger.error(f"❌ CRITICAL: Analysis blocked despite session connection existing - investigating session ID mismatch!")
                    
                    return {
                        "status": "connection_required",
                        "message": "⚠️ **Dataset Connection Required**\n\nYou need to connect to a Power BI dataset before running analysis. Please use the connection panel above to connect to your dataset first.",
                        "thought_process": [],
                        "workflow_results": [],
                        "requires_connection": True,
                        "session_id": session_id,
                        "total_time": 0.0
                    }
            else:
                # Analysis validation passed - log success
                logger.info(f"✅ ANALYSIS VALIDATION SUCCESS - session {session_id} connected to dataset: {session_connection.get('dataset')} in workspace: {session_connection.get('workspace')}")
        
        # Clear any existing stop flag for this session when starting new analysis
        if session_id:
            self.clear_stop_flag(session_id)
            # Also clear stop flags on session-specific tool objects
            if session_id in self.session_instances:
                session_instances = self.session_instances[session_id]
                if "evaluator" in session_instances and hasattr(session_instances["evaluator"], '_should_stop'):
                    delattr(session_instances["evaluator"], '_should_stop')
                if "tabular_editor" in session_instances and hasattr(session_instances["tabular_editor"], '_should_stop'):
                    delattr(session_instances["tabular_editor"], '_should_stop')
        
        workflow_results = []
        total_iterations = 0
        max_iterations = 1  # Prevent infinite loops
        thought_process = []  # Track the thought process and reasoning steps
        
        # Start timing
        start_time = time.time()
        
        # Ensure we have a queue for this session
        if session_id and session_id not in workflow_updates_queue:
            workflow_updates_queue[session_id] = queue.Queue()
        
        # Session-aware logging is handled by SessionAwareWebLogHandler
        # No need for additional log streaming setup - it's automatic
        
        try:
            # CRITICAL: Set session context for this thread to ensure session-aware logging
            if session_id:
                set_webapp_session_context(session_id)
            
            logger.info(f"Starting autonomous workflow for: {user_message}")
            
            # Phase 1: Plan the complete workflow
            planning_start = time.time()
            workflow_plan = self._plan_complete_workflow(user_message, session_id)
            planning_time = time.time() - planning_start
            logger.info(f"Workflow plan completed in {planning_time:.2f} seconds: {workflow_plan}")
            
            # Add the workflow plan to the thought process
            plan_step = {
                "step": "workflow_planning",
                "description": f"Planning the complete workflow (took {planning_time:.2f}s)",
                "details": workflow_plan
            }
            thought_process.append(plan_step)
            
            # Stream update if session_id is provided
            if session_id:
                StreamingManager.send_update(
                    workflow_updates_queue[session_id],
                    {"step": plan_step, "user_message": user_message},
                    "workflow_update"
                )
            
            # Phase 2: Execute the planned workflow autonomously
            current_message = user_message
            accumulated_context = []
            
            while total_iterations < max_iterations:
                total_iterations += 1
                logger.info(f"Autonomous iteration {total_iterations}/{max_iterations}")
                
                # Check if this session should stop execution
                if session_id and self.should_stop_session(session_id):
                    logger.info(f"Autonomous workflow stopped due to session stop flag: {session_id}")
                    return {
                        "success": False,
                        "error": "Analysis stopped by user",
                        "final_answer": "Analysis was stopped by the user.",
                        "iterations_used": total_iterations - 1,
                        "thought_process": thought_process,
                        "workflow_results": workflow_results,
                        "stopped": True
                    }
                
                # Add the iteration start to thought process
                iter_start_step = {
                    "step": f"iteration_start_{total_iterations}",
                    "description": f"Starting iteration {total_iterations}",
                    "current_message": current_message
                }
                thought_process.append(iter_start_step)
                
                # Stream update if session_id is provided
                if session_id:
                    StreamingManager.send_update(
                        workflow_updates_queue[session_id],
                        {"step": iter_start_step},
                        "workflow_update"
                    )
                
                # Detect and execute tools for current step
                tool_detection_start = time.time()
                step_results = self.auto_detect_and_execute_tools(current_message, session_id)
                tool_detection_time = time.time() - tool_detection_start
                logger.info(f"Tool detection completed in {tool_detection_time:.2f} seconds. Found {len(step_results) if step_results else 0} tools.")
                
                # Add tool detection to thought process
                tool_detection_step = {
                    "step": f"tool_detection_{total_iterations}",
                    "description": f"Detecting appropriate tools for iteration {total_iterations} (took {tool_detection_time:.2f}s)",
                    "detected_tools": [f"{result['tool_name']} (confidence: {result['confidence']})" for result in step_results] if step_results else "No tools detected"
                }
                thought_process.append(tool_detection_step)
                
                # Stream update if session_id is provided
                if session_id:
                    StreamingManager.send_update(
                        workflow_updates_queue[session_id],
                        {"step": tool_detection_step},
                        "workflow_update"
                    )
                
                if not step_results:
                    # No more tools needed - workflow complete
                    logger.info("No more tools detected - workflow complete")
                    complete_step = {
                        "step": f"workflow_complete_{total_iterations}",
                        "description": "No more tools needed - workflow complete"
                    }
                    thought_process.append(complete_step)
                    
                    # Stream update if session_id is provided
                    if session_id:
                        StreamingManager.send_update(
                            workflow_updates_queue[session_id],
                            {"step": complete_step},
                            "workflow_complete"
                        )
                    
                    break
                
                # Process step results
                for result in step_results:
                    tool_start_time = time.time()
                    
                    # Add the tool execution attempt to thought process
                    tool_exec_step = {
                        "step": f"tool_execution_{total_iterations}_{result['tool_name']}",
                        "description": f"🔧 {self._get_tool_friendly_name(result['tool_name'])}",
                        "details": result['explanation'],
                        "parameters": result['params'],
                        "execution_start_time": time.strftime('%H:%M:%S', time.localtime()),
                        "status": "executing"
                    }
                    thought_process.append(tool_exec_step)
                    
                    # Stream update if session_id is provided
                    if session_id:
                        StreamingManager.send_update(
                            workflow_updates_queue[session_id],
                            {"step": tool_exec_step, "progress": f"Step {total_iterations}"},
                            "workflow_update"
                        )
                    
                    if result["result"].get("success", False):
                        # Store successful result
                        workflow_step_result = {
                            "iteration": total_iterations,
                            "tool_name": result["tool_name"], 
                            "params": result["params"],
                            "result": result["result"]["result"],
                            "explanation": result["explanation"]
                        }
                        
                        # Preserve CSV download metadata if present (added by _handle_csv_download to result["result"])
                        if result["result"] and isinstance(result["result"], dict):
                            if "csv_download_url" in result["result"]:
                                workflow_step_result["csv_download_url"] = result["result"]["csv_download_url"]
                            if "csv_filename" in result["result"]:
                                workflow_step_result["csv_filename"] = result["result"]["csv_filename"]
                        
                        workflow_results.append(workflow_step_result)
                        
                        # Add result to accumulated context
                        accumulated_context.append(f"Step {total_iterations}: {result['tool_name']} executed successfully")
                        
                        # Add successful execution to thought process
                        tool_execution_time = time.time() - tool_start_time
                        success_step = {
                            "step": f"tool_success_{total_iterations}_{result['tool_name']}",
                            "description": f"✅ {self._get_tool_friendly_name(result['tool_name'])} completed",
                            "details": f"Successfully executed in {tool_execution_time:.2f}s",
                            "result_summary": self._format_result_for_context(result["result"]["result"]),
                            "status": "completed"
                        }
                        thought_process.append(success_step)
                        
                        # Stream update if session_id is provided
                        if session_id:
                            # Enhanced streaming - send result immediately with success step
                            # This ensures results are sent as soon as they're available
                            safe_result = {
                                "iteration": total_iterations,
                                "tool_name": result["tool_name"], 
                                "params": result["params"],
                                "result": self._safe_json_serializable(result["result"]["result"]),
                                "explanation": result["explanation"]
                            }
                            
                            # Preserve CSV download metadata if present (added by _handle_csv_download)
                            # Structure: result = {"tool_name": "...", "result": {"success": True, "result": {csv_download_url: "..."}}}
                            # CSV fields are at result["result"]["result"] (three levels deep)
                            if result.get("result") and isinstance(result["result"], dict):
                                if result["result"].get("result") and isinstance(result["result"]["result"], dict):
                                    # CSV metadata is at the innermost level
                                    if "csv_download_url" in result["result"]["result"]:
                                        safe_result["csv_download_url"] = result["result"]["result"]["csv_download_url"]
                                        logger.info(f"📁 Streaming CSV download URL: {result['result']['result']['csv_download_url']}")
                                    if "csv_filename" in result["result"]["result"]:
                                        safe_result["csv_filename"] = result["result"]["result"]["csv_filename"]
                                        logger.info(f"📁 Streaming CSV filename: {result['result']['result']['csv_filename']}")
                            
                            # Check if this result includes a high-level summary
                            high_level_summary = None
                            if result["result"]["result"] and isinstance(result["result"]["result"], dict):
                                high_level_summary = result["result"]["result"].get("high_level_summary")
                            
                            update_data = {
                                "step": success_step,
                                "result": safe_result,
                                "immediate_result": True,
                                "execution_time": f"{tool_execution_time:.2f}s"
                            }
                            
                            # Add summary data if available
                            if high_level_summary:
                                update_data["high_level_summary"] = self._safe_json_serializable(high_level_summary)
                            
                            StreamingManager.send_update(
                                workflow_updates_queue[session_id],
                                update_data,
                                "workflow_update"
                            )
                        
                        # Update current message with context for next iteration
                        if result["result"]["result"]:
                            # Format result for context
                            formatted_result = self._format_result_for_context(result["result"]["result"])
                            accumulated_context.append(f"Result: {formatted_result}")
                    else:
                        # Add failure to thought process
                        failure_step = {
                            "step": f"tool_failure_{total_iterations}_{result['tool_name']}",
                            "description": f"Failed to execute {result['tool_name']}",
                            "error": result["result"]["error"]
                        }
                        thought_process.append(failure_step)
                        
                        # Stream update if session_id is provided
                        if session_id:
                            StreamingManager.send_update(
                                workflow_updates_queue[session_id],
                                {"step": failure_step},
                                "tool_failure"
                            )
                        
                        # Handle error with autonomous debugging
                        logger.info(f"Tool execution failed, attempting autonomous fix...")
                        
                        # Add debugging start to thought process
                        debug_start_step = {
                            "step": f"debug_start_{total_iterations}_{result['tool_name']}",
                            "description": f"Starting autonomous debugging for {result['tool_name']}",
                            "error": result["result"]["error"]
                        }
                        thought_process.append(debug_start_step)
                        
                        # Stream update if session_id is provided
                        if session_id:
                            StreamingManager.send_update(
                                workflow_updates_queue[session_id],
                                {"step": debug_start_step},
                                "debug_start"
                            )
                        
                        error_result = self._handle_autonomous_error(
                            result["tool_name"], 
                            result["params"], 
                            result["result"]["error"],
                            accumulated_context,
                            session_id
                        )
                        
                        if error_result:
                            workflow_results.append(error_result)
                            accumulated_context.append(f"Step {total_iterations}: Error fixed and executed successfully")
                            
                            # Add successful fix to thought process
                            debug_success_step = {
                                "step": f"debug_success_{total_iterations}_{result['tool_name']}",
                                "description": f"Successfully fixed error in {result['tool_name']}",
                                "original_error": result["result"]["error"],
                                "fixed_parameters": error_result["params"],
                                "result_summary": self._format_result_for_context(error_result["result"])
                            }
                            thought_process.append(debug_success_step)
                            
                            # Stream update if session_id is provided
                            if session_id:
                                # Enhanced streaming - send debug success result immediately 
                                # This ensures debugging results are sent as soon as they're available
                                safe_error_result = {
                                    "tool_name": error_result["tool_name"],
                                    "params": error_result["params"],
                                    "result": self._safe_json_serializable(error_result["result"])
                                }
                                StreamingManager.send_update(
                                    workflow_updates_queue[session_id],
                                    {
                                        "step": debug_success_step, 
                                        "result": safe_error_result,
                                        "immediate_result": True,
                                        "debug_fixed": True
                                    },
                                    "debug_success"
                                )
                        else:
                            # Add failed fix to thought process
                            debug_failure_step = {
                                "step": f"debug_failure_{total_iterations}_{result['tool_name']}",
                                "description": f"Failed to fix error in {result['tool_name']}",
                                "error": result["result"]["error"]
                            }
                            thought_process.append(debug_failure_step)
                            
                            # Stream update if session_id is provided
                            if session_id:
                                StreamingManager.send_update(
                                    workflow_updates_queue[session_id],
                                    {"step": debug_failure_step},
                                    "debug_failure"
                                )
                
                # Check if we have enough information to provide final answer
                completion_check = self._workflow_complete_check(user_message, workflow_results)
                
                # Add completion check to thought process
                completion_check_step = {
                    "step": f"completion_check_{total_iterations}",
                    "description": "Checking if workflow is complete",
                    "is_complete": completion_check,
                    "results_count": len(workflow_results)
                }
                thought_process.append(completion_check_step)
                
                # Stream update if session_id is provided
                if session_id:
                    StreamingManager.send_update(
                        workflow_updates_queue[session_id],
                        {"step": completion_check_step},
                        "workflow_update"
                    )
                
                if completion_check:
                    logger.info("Workflow determined complete based on results analysis")
                    break
                
                # Prepare context for next iteration
                context_summary = " ".join(accumulated_context[-3:])  # Keep last 3 context items
                current_message = f"Based on previous results: {context_summary}. Continue with the original request: {user_message}"
                
                # Add context preparation to thought process
                context_prep_step = {
                    "step": f"context_preparation_{total_iterations}",
                    "description": "Preparing context for next iteration",
                    "context_summary": context_summary,
                    "next_message": current_message
                }
                thought_process.append(context_prep_step)
                
                # Stream update if session_id is provided
                if session_id:
                    StreamingManager.send_update(
                        workflow_updates_queue[session_id],
                        {"step": context_prep_step},
                        "workflow_update"
                    )

            # Phase 3: Synthesize final comprehensive answer
            synthesis_start_step = {
                "step": "synthesis_start",
                "description": "Starting synthesis of final comprehensive answer",
                "workflow_results_count": len(workflow_results)
            }
            thought_process.append(synthesis_start_step)
            
            # Stream update if session_id is provided
            if session_id:
                StreamingManager.send_update(
                    workflow_updates_queue[session_id],
                    {"step": synthesis_start_step},
                    "workflow_update"
                )
            
            final_answer = self._synthesize_final_answer(user_message, workflow_results)
            # Only generate synthesis completion message for true final completion
            # Suppress for sub-analyses within larger workflows (like individual steps in security analysis)
            # The frontend depends on this exact message to coordinate Run All workflow progression
 
            # Check if this is a Run All analysis request
            is_run_all_analysis = (
                "copilotdataevaluator_evaluate_security_compliance" in user_message.lower() or
                "copilotdataevaluator_evaluate_measures_analysis" in user_message.lower() or
                "copilotdataevaluator_evaluate_fact_dimension_analysis" in user_message.lower() or
                "copilotdataevaluator_evaluate_metadata_analysis" in user_message.lower() or
                "copilotdataevaluator_dataset_columns_analysis" in user_message.lower() or
                "copilotdataevaluator_evaluate_table_linking" in user_message.lower() or
                "copilotdataevaluator_evaluate_table_hierarchies" in user_message.lower() or
                "evaluate_security_compliance" in user_message.lower() or
                "evaluate_measures_analysis" in user_message.lower() or
                "evaluate_fact_dimension_analysis" in user_message.lower() or
                "evaluate_metadata_analysis" in user_message.lower() or
                "dataset_columns_analysis" in user_message.lower() or
                "evaluate_table_linking" in user_message.lower() or
                "evaluate_table_hierarchies" in user_message.lower()
            )
           
            # For Run All analyses, only generate synthesis completion if we have actual workflow results
            # This prevents premature completion when analysis is interrupted
            has_substantial_results = len(workflow_results) > 0 and any(
                result.get("result", {}).get("success", False) for result in workflow_results
            )
           
            is_sub_analysis = is_run_all_analysis and not has_substantial_results
            
            logger.info(f"🔍 SYNTHESIS CHECK: is_sub_analysis={is_sub_analysis}, generate_final_synthesis={generate_final_synthesis}, workflow_context={workflow_context}")
            logger.info(f"🔍 USER MESSAGE FULL: {user_message}")
            logger.info(f"🔍 WORKFLOW RESULTS: {len(workflow_results)} results")
            logger.info(f"🔍 HAS SUBSTANTIAL RESULTS: {has_substantial_results}")
            logger.info(f"🔍 IS RUN ALL ANALYSIS: {is_run_all_analysis}")
           
            if generate_final_synthesis and not is_sub_analysis:
                logger.info("✅ GENERATING SYNTHESIS COMPLETION MESSAGE")
                synthesis_complete_step = {
                    "step": "synthesis_complete",
                    "description": "Finished synthesis of final comprehensive answer",
                    "answer_length": len(final_answer)
                }
                thought_process.append(synthesis_complete_step)
               
                # Stream update if session_id is provided
                if session_id:
                    StreamingManager.send_update(
                        workflow_updates_queue[session_id],
                        {
                            "step": synthesis_complete_step,
                            "final_answer": final_answer
                        },
                        "workflow_update"
                    )
            else:
                logger.info("🔄 SUPPRESSING SYNTHESIS COMPLETION MESSAGE (sub-analysis or disabled)")    
                
                # Send workflow complete signal to close the stream
                StreamingManager.send_update(
                    workflow_updates_queue[session_id],
                    {
                        "type": "workflow_complete",
                        "message": "Workflow complete",
                        "thought_process": thought_process,
                        "final_answer": final_answer
                    },
                    "workflow_update"
                )
            
            return {
                "success": True,
                "final_answer": final_answer,
                "workflow_results": workflow_results,
                "iterations_used": total_iterations,
                "autonomous_execution": True,
                "thought_process": thought_process  # Include the thought process in the response
            }
            
        except Exception as e:
            logger.error(f"Error in autonomous workflow: {e}")
            
            # Send error message if streaming
            if session_id:
                StreamingManager.send_update(
                    workflow_updates_queue[session_id],
                    {
                        "type": "workflow_error",
                        "error": str(e),
                        "message": "Error in workflow execution"
                    },
                    "workflow_update"
                )
            
            return {
                "success": False,
                "error": str(e),
                "partial_results": workflow_results,
                "iterations_used": total_iterations
            }
        finally:
            # Session-aware logging is cleaned up automatically
            pass

    def _plan_complete_workflow(self, user_message: str, session_id: str = None) -> Dict[str, Any]:
        """Plan the complete workflow needed to answer the user's question."""
        try:
            planning_prompt = f"""
            Analyze this user request and plan the complete sequence of tools needed:
            
            User Request: {user_message}
            
            Available Tool Categories:
            - Connection: fabric_get_workspace_info, fabric_get_lakehouse_info, tabulareditor_connect_dataset
            - Discovery: tabulareditor_list_tables, sqlendpoint_get_sql_tables, sqlendpoint_get_sql_table_schema  
            - Execution: sqlendpoint_execute_sql_query, tabulareditor_execute_dax_query
            - Analysis: Various analysis and evaluation tools
            
            Current State for Session {session_id}: 
            - Workspace: {self.session_connections.get(session_id, {}).get('workspace', 'None')}
            - Dataset: {self.session_connections.get(session_id, {}).get('dataset', 'None')}
            
            Plan the complete workflow as a JSON object with:
            - estimated_steps: number of tool executions needed
            - workflow_type: "data_analysis", "connection_setup", "schema_exploration", etc.
            - key_dependencies: what must happen before what
            - expected_final_output: what the user should receive at the end
            """
            
            messages = [{"role": "user", "content": planning_prompt}]
            
            response = client.chat.completions.create(
                model=azure_deployment,
                messages=messages,
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=300
            )
            
            return json.loads(response.choices[0].message.content)
            
        except Exception as e:
            logger.warning(f"Workflow planning failed: {e}")
            return {"estimated_steps": 5, "workflow_type": "general", "key_dependencies": [], "expected_final_output": "Data analysis results"}

    def _format_result_for_context(self, result) -> str:
        """Format tool result for use in next iteration context."""
        if isinstance(result, (dict, list)):
            # Summarize complex results
            if isinstance(result, list) and len(result) > 3:
                return f"Retrieved {len(result)} items (showing first 3): {str(result[:3])[:200]}..."
            elif isinstance(result, dict) and len(str(result)) > 200:
                return f"Retrieved data structure with keys: {list(result.keys())[:5]}"
            else:
                return str(result)[:200]
        else:
            return str(result)[:200]
    
    def _safe_json_serializable(self, obj):
        """Convert objects to JSON-serializable format, handling special cases."""
        if obj is None:
            return None
        elif isinstance(obj, (str, int, float, bool)):
            return obj
        elif isinstance(obj, (list, tuple)):
            return [self._safe_json_serializable(item) for item in obj]
        elif isinstance(obj, dict):
            return {str(k): self._safe_json_serializable(v) for k, v in obj.items()}
        else:
            # For non-serializable objects (like SQL Engine), convert to string representation
            try:
                # Check if it's a SQLAlchemy engine or similar database object
                if hasattr(obj, 'url') or 'engine' in str(type(obj)).lower():
                    return f"Database connection: {str(obj)[:100]}"
                else:
                    return str(obj)[:200]
            except Exception:
                return f"<{type(obj).__name__} object>"
    
    def _get_tool_friendly_name(self, tool_name: str) -> str:
        """Convert technical tool names to user-friendly descriptions."""
        friendly_names = {
            "sqlendpoint_initialize_sql_connection": "Initializing SQL connection",
            "sqlendpoint_execute_sql": "Executing SQL query",
            "sqlendpoint_get_tables": "Retrieving table list",
            "sqlendpoint_get_table_schema": "Getting table schema",
            "tabulareditor_connect_dataset": "Connecting to Power BI dataset",
            "tabulareditor_get_measures": "Retrieving measures",
            "tabulareditor_get_tables": "Getting data model tables",
            "fabric_list_workspaces": "Listing Power BI workspaces",
            "fabric_list_datasets": "Finding datasets",
            "auth_authenticate": "Authenticating user",
        }
        return friendly_names.get(tool_name, tool_name.replace("_", " ").title())

    def _workflow_complete_check(self, original_message: str, results: List[Dict]) -> bool:
        """Check if we have enough information to provide a complete answer."""
        # Simple heuristics for now
        if len(results) >= 3:  # If we have at least 3 successful tool executions
            # Check if we have data retrieval results
            has_data = any("execute" in result.get("tool_name", "") for result in results)
            has_schema = any("schema" in result.get("tool_name", "") or "table" in result.get("tool_name", "") for result in results)
            
            if has_data or (has_schema and len(results) >= 2):
                return True
        
        return False

    def _handle_autonomous_error(self, tool_name: str, params: Dict, error: str, context: List[str], session_id: str = None) -> Optional[Dict]:
        """Handle tool execution errors autonomously with debugging."""
        try:
            logger.info(f"Autonomous error handling for {tool_name}: {error}")
            
            # Use the existing debugging prompt from PromptManager
            debug_prompt = PromptManager.get_debugging_prompt(
                error=error,
                tool=tool_name, 
                params=json.dumps(params, indent=2),
                history=json.dumps(context),
                diagnostic=json.dumps({"autonomous_mode": True})
            )
            
            messages = [{"role": "user", "content": debug_prompt}]
            
            response = client.chat.completions.create(
                model=azure_deployment,
                messages=messages,
                temperature=0.2,
                max_tokens=800
            )
            
            debug_response = response.choices[0].message.content
            
            # Extract the reasoning if present
            reasoning = ""
            if "REASONING:" in debug_response:
                reasoning_part = debug_response.split("REASONING:")[1].strip()
                if "TOOL_EXECUTION:" in reasoning_part:
                    reasoning = reasoning_part.split("TOOL_EXECUTION:")[0].strip()
                else:
                    reasoning = reasoning_part
            
            # Extract corrected tool execution if present
            if "TOOL_EXECUTION:" in debug_response:
                tool_part = debug_response.split("TOOL_EXECUTION:")[1].strip()
                json_start = tool_part.find('{')
                json_end = tool_part.rfind('}') + 1
                
                if json_start >= 0 and json_end > json_start:
                    corrected_tool = json.loads(tool_part[json_start:json_end])
                    corrected_params = corrected_tool.get("parameters", {})
                    
                    # Execute the corrected tool
                    fix_result = self.execute_tool(tool_name, corrected_params, session_id)
                    
                    if fix_result.get("success", False):
                        return {
                            "tool_name": tool_name,
                            "params": corrected_params,
                            "result": fix_result["result"], 
                            "autonomous_fix": True,
                            "original_error": error,
                            "reasoning": reasoning  # Include the reasoning in the result
                        }
            
            return None
            
        except Exception as e:
            logger.error(f"Error in autonomous error handling: {e}")
            return None

    def _synthesize_final_answer(self, user_message: str, workflow_results: List[Dict]) -> str:
        """Synthesize all workflow results into a comprehensive final answer."""
        try:
            # Prepare results summary for synthesis
            results_summary = []
            for result in workflow_results:
                 # Handle None results gracefully
                if result is None:
                    logger.warning("⚠️ Found None result in workflow_results, skipping...")
                    continue
               
                # Ensure result is a dictionary
                if not isinstance(result, dict):
                    logger.warning(f"⚠️ Found non-dict result in workflow_results: {type(result)}, converting...")
                    result = {"tool_name": "unknown", "result": str(result)}
               
                tool_info = f"Tool: {result.get('tool_name', 'unknown')}"
                if result.get('autonomous_fix'):
                    tool_info += " (autonomously fixed)"
                
                # Format result data
                result_data = result.get('result', '')
                 # Handle None result data
                if result_data is None:
                    summary = "Tool executed successfully (no data returned)"
                elif isinstance(result_data, (dict, list)):
                    if isinstance(result_data, list) and len(result_data) > 5:
                        summary = f"Retrieved {len(result_data)} items. Sample: {str(result_data[:3])}"
                    else:
                        summary = json.dumps(result_data, indent=2)[:500] + ("..." if len(str(result_data)) > 500 else "")
                else:
                    summary = str(result_data)[:500] + ("..." if len(str(result_data)) > 500 else "")
                
                results_summary.append(f"{tool_info}\nResult: {summary}")
            
            synthesis_prompt = f"""
            Create a comprehensive, detailed response to the user's question using all the tool execution results.
            
            Original Question: {user_message}
            
            Tool Execution Results:
            {chr(10).join(results_summary)}
            
            Instructions:
            1. **Executive Summary**: Start with a clear, direct answer to the user's original question
            2. **Key Findings**: Present the most important discoveries from the analysis
            3. **Detailed Analysis**: Include comprehensive data from tool results with specific examples:
               - For issues found: provide specific examples with table/column names, values, or scenarios
               - For recommendations: give concrete, actionable steps with examples
               - For data quality issues: show specific instances and their impact
               - For best practices violations: cite exact cases and suggest fixes
            4. **Data Presentation**: Format all data clearly using:
               - Tables for structured data
               - Bullet points for lists of findings
               - Code blocks for formulas or technical details
               - Clear headers and sections for organization
            5. **Recommendations**: Provide detailed, prioritized recommendations with:
               - Specific examples of what to fix
               - Step-by-step implementation guidance
               - Expected outcomes and benefits
            6. **Next Steps**: Suggest concrete actions the user should take
            
            Requirements:
            - Be thorough and comprehensive - include ALL relevant findings
            - Provide specific examples and evidence for every claim
            - Use clear formatting with headers, subheaders, and structured layout
            - Include quantitative data wherever available (counts, percentages, etc.)
            - Make recommendations actionable with concrete examples
            - Ensure the response is complete enough that no follow-up questions are needed
            
            Format the response with clear markdown headers (##, ###) and structured sections.
            Respond as if you successfully completed the entire analysis in one go.
            """
            
            messages = [{"role": "user", "content": synthesis_prompt}]
            
            response = client.chat.completions.create(
                model=azure_deployment,
                messages=messages,
                temperature=0.7,
                max_tokens=2000
            )
            
            return response.choices[0].message.content
            
        except Exception as e:
            logger.error(f"Error synthesizing final answer: {e}")
            # Fallback: provide a basic summary
            return f"Analysis completed using {len(workflow_results)} tool executions. Results available in the workflow data."
    
    def stop_all_executions(self, session_id: str = None):
        """
        Stop all running tool executions and cleanup resources for a specific session.
        This method is called when the user clicks the Stop Analysis button.
        """
        try:
            logger.info(f"⚠️ STOPPING ALL EXECUTIONS for session {session_id}")
            
            # Set stop flag for this session to interrupt ongoing operations
            if session_id:
                self.stop_flags[session_id] = True
                logger.info(f"🔄 Set stop flag for session {session_id}")
                
                # Set cancellation event to signal running threads to stop
                if session_id not in self.cancellation_events:
                    self.cancellation_events[session_id] = threading.Event()
                self.cancellation_events[session_id].set()  # Signal cancellation
                logger.info(f"🔄 Set cancellation event for session {session_id}")
                
                # Interrupt any running processes for this session
                if session_id in self.running_processes:
                    processes = self.running_processes[session_id]
                    for process_info in processes:
                        try:
                            if 'thread' in process_info and process_info['thread'].is_alive():
                                logger.info(f"🔍 Found running thread: {process_info.get('name', 'unknown')}")
                                # For Python threads, we can't forcefully kill them, but we can signal them to stop
                                # The threads should check the cancellation event
                            if 'process' in process_info:
                                logger.info(f"🔍 Found running process: {process_info.get('name', 'unknown')}")
                                process_info['process'].terminate()
                        except Exception as e:
                            logger.warning(f"Error stopping process: {e}")
                    
                    # Clear the running processes
                    del self.running_processes[session_id]
                    logger.info(f"✅ Cleared running processes for session {session_id}")
            
            # Signal analysis tools to stop (but don't disconnect)
            if self.copilot_evaluator:
                try:
                    # Set multiple stop signals for maximum compatibility
                    setattr(self.copilot_evaluator, '_should_stop', True)
                    setattr(self.copilot_evaluator, '_cancellation_event', self.cancellation_events.get(session_id))
                    setattr(self.copilot_evaluator, '_stop_requested', True)
                    
                    # Also try to call a stop method if it exists
                    if hasattr(self.copilot_evaluator, 'stop_analysis'):
                        self.copilot_evaluator.stop_analysis()
                    elif hasattr(self.copilot_evaluator, 'cancel'):
                        self.copilot_evaluator.cancel()
                    
                    logger.info("🔄 Set stop signal on copilot evaluator")
                except Exception as e:
                    logger.warning(f"Error signaling copilot evaluator: {e}")
            
            # Signal tabular editor to stop operations (but keep connection)
            if self.tabular_editor:
                try:
                    # Set stop flag on tabular editor but don't disconnect
                    setattr(self.tabular_editor, '_should_stop', True)
                    setattr(self.tabular_editor, '_cancellation_event', self.cancellation_events.get(session_id))
                    setattr(self.tabular_editor, '_stop_requested', True)
                    
                    # Also try to call a stop method if it exists
                    if hasattr(self.tabular_editor, 'stop_operations'):
                        self.tabular_editor.stop_operations()
                    elif hasattr(self.tabular_editor, 'cancel'):
                        self.tabular_editor.cancel()
                    
                    logger.info("🔄 Set stop signal on tabular editor (keeping connection)")
                except Exception as e:
                    logger.warning(f"Error signaling tabular editor: {e}")
            
            # DON'T reset connection states - keep them for next analysis
            # DON'T close database connections - reuse them
            # DON'T disconnect tabular editor - just signal it to stop current operations
            
            # Clear any session-specific caches or temporary data
            if session_id:
                # Clear any session-specific data structures
                if hasattr(self, 'session_data') and session_id in self.session_data:
                    del self.session_data[session_id]
                
                # Clear streaming queues for this session
                if session_id in workflow_updates_queue:
                    workflow_updates_queue[session_id] = queue.Queue()
            
            logger.info(f"✅ Successfully signaled stop for session {session_id} (keeping connections alive)")
            return True
            
        except Exception as e:
            logger.error(f"Error stopping executions for session {session_id}: {e}")
            return False
    
    def should_stop_session(self, session_id: str) -> bool:
        """Check if a session should stop execution."""
        return self.stop_flags.get(session_id, False)
    
    def get_running_processes_status(self, session_id: str) -> Dict:
        """Get status of running processes for a session."""
        import time
        if session_id not in self.running_processes:
            return {"running": False, "processes": []}
        
        current_time = time.time()
        processes_info = []
        
        for process_info in self.running_processes[session_id]:
            if process_info.get('thread') and process_info['thread'].is_alive():
                runtime = current_time - process_info.get('start_time', current_time)
                processes_info.append({
                    'name': process_info.get('name', 'unknown'),
                    'function': process_info.get('function_name', 'unknown'),
                    'runtime_seconds': round(runtime, 1),
                    'status': 'running'
                })
        
        return {
            "running": len(processes_info) > 0,
            "processes": processes_info,
            "total_running": len(processes_info)
        }
    
    def clear_stop_flag(self, session_id: str):
        """Clear the stop flag for a session when starting new analysis."""
        if session_id in self.stop_flags:
            del self.stop_flags[session_id]
        
        # Clear cancellation event for new analysis
        if session_id in self.cancellation_events:
            del self.cancellation_events[session_id]
            logger.info(f"Cleared cancellation event for session {session_id}")
        
        # Clear any running processes tracking
        if session_id in self.running_processes:
            del self.running_processes[session_id]
    
    def clear_connection_state(self, reason: str = "New session", session_id: str = None):
        """Clear the connection state cleanly for a specific session or all sessions."""
        try:
            if session_id:
                logger.info(f"Clearing connection state for session {session_id}: {reason}")
                # Clear only this session's connection from BOTH systems
                if session_id in self.session_connections:
                    del self.session_connections[session_id]
                    logger.info(f"Cleared local connection state for session {session_id}")
                    
                # SYNC: Also clear from global tool_manager.session_connections
                global tool_manager
                if tool_manager and hasattr(tool_manager, 'session_connections'):
                    if session_id in tool_manager.session_connections:
                        del tool_manager.session_connections[session_id]
                        logger.info(f"Cleared global connection state for session {session_id}")
                if session_id in self.active_com_connections:
                    del self.active_com_connections[session_id]
                    logger.info(f"Cleared COM connection tracking for session {session_id}")
                
                # If session has instances, attempt to disconnect gracefully
                if session_id in self.session_instances:
                    session_instances = self.session_instances[session_id]
                    if "tabular_editor" in session_instances:
                        try:
                            tabular_editor = session_instances["tabular_editor"]
                            if hasattr(tabular_editor, 'connected') and tabular_editor.connected:
                                tabular_editor.disconnect_dataset()
                                logger.info(f"Successfully disconnected tabular editor for session {session_id}")
                        except Exception as disconnect_error:
                            logger.warning(f"Error during graceful disconnect for session {session_id}: {disconnect_error}")
            else:
                logger.info(f"Clearing ALL connection states: {reason}")
                # Clear all session connections (rare case)
                self.session_connections.clear()
                self.active_com_connections.clear()
                
                # Disconnect all session instances
                for sid, session_instances in self.session_instances.items():
                    if "tabular_editor" in session_instances:
                        try:
                            tabular_editor = session_instances["tabular_editor"]
                            if hasattr(tabular_editor, 'connected') and tabular_editor.connected:
                                tabular_editor.disconnect_dataset()
                                logger.info(f"Successfully disconnected tabular editor for session {sid}")
                        except Exception as disconnect_error:
                            logger.warning(f"Error during graceful disconnect for session {sid}: {disconnect_error}")
            
            return True
        except Exception as e:
            logger.error(f"Error clearing connection state: {e}")
            return False
    
    
    def verify_connection_state(self, session_id: str = None) -> bool:
        """Verify if the connection state for a session is actually active."""
        if not session_id or session_id not in self.session_connections:
            return False
        
        session_connection = self.session_connections[session_id]
        if not session_connection.get("dataset"):
            return False
        
        try:
            # Check if tabular editor is actually connected using session-specific instance
            if session_id in self.session_instances:
                session_instances = self.session_instances[session_id]
                if "tabular_editor" in session_instances:
                    tabular_editor = session_instances["tabular_editor"]
                    if hasattr(tabular_editor, 'connected'):
                        return tabular_editor.connected
            return False
        except Exception as e:
            logger.warning(f"Error verifying connection state for session {session_id}: {e}")
            return False
    
    def _handle_csv_download(self, result: Dict[str, Any], session_id: str):
        """
        Handle CSV or Excel download data from tool results.
        Stores file content and filename for later download via browser.
        """
        try:
            if not isinstance(result, dict):
                logger.warning(f"⚠️ _handle_csv_download: result is not a dict, type={type(result)}")
                return
            
            # Check if result contains file data (prioritize Excel content for download)
            file_content = result.get('excel_content') or result.get('csv_content')
            file_filename = result.get('csv_filename')
            
            if file_content and file_filename:
                # Clean up filename by removing workspace and dataset names
                cleaned_filename = self._clean_csv_filename(file_filename)
                
                # Initialize session file storage if needed
                if session_id not in csv_downloads:
                    csv_downloads[session_id] = {}
                
                # Generate unique key for this file
                import time
                file_key = f"file_{int(time.time() * 1000)}"
                
                # Store file data with cleaned filename
                csv_downloads[session_id][file_key] = {
                    'content': file_content,
                    'filename': cleaned_filename,
                    'timestamp': time.time()
                }
                
                # Add download info to result
                result['csv_download_url'] = f"/api/csv/download/{session_id}/{file_key}"
                result['csv_download_key'] = file_key
                result['csv_filename'] = cleaned_filename
                
                # Log appropriate message based on file type
                file_type = "Excel" if cleaned_filename.lower().endswith('.xlsx') else "CSV"
                logger.info(f"📁 {file_type} file ready for download: {cleaned_filename}")
                
                # Keep csv_content as text for UI parsing (even if we have Excel content for download)
                if result.get('excel_content') and not result.get('csv_content'):
                    # If we only have Excel content, we need to ensure UI can still parse
                    logger.info("📊 Excel content provided for download, maintaining UI compatibility")
                
        except Exception as e:
            logger.error(f"Error handling CSV download: {e}")
    
    def _clean_csv_filename(self, original_filename: str) -> str:
        """
        Clean filename by removing workspace and dataset names, keeping only
        analysis type and timestamp for a cleaner, shorter filename.
        Supports both CSV and Excel files.
        """
        try:
            # Remove file extension (handle both .csv and .xlsx)
            if original_filename.lower().endswith('.xlsx'):
                name_without_ext = original_filename.replace('.xlsx', '')
                file_extension = '.xlsx'
            else:
                name_without_ext = original_filename.replace('.csv', '')
                file_extension = '.csv'
            
            # Split by underscores to analyze parts
            parts = name_without_ext.split('_')
            
            # Common analysis type mappings to clean names
            analysis_type_map = {
                'Dataset_Columns_Analysis': 'Columns',
                'Fact_Dimension_Analysis': 'FactDimension', 
                'Metadata_Analysis': 'Metadata',
                'Table_Linking_Analysis': 'TableLinking',
                'Table_Hierarchies_Analysis': 'Hierarchies',
                'Measures_Analysis': 'Measures',
                'Security_Analysis': 'Security'
            }
            
            # Try to identify analysis type and timestamp
            analysis_type = None
            timestamp = None
            
            # Look for analysis type in the filename
            for mapped_type, clean_name in analysis_type_map.items():
                if mapped_type in original_filename:
                    analysis_type = clean_name
                    break
            
            # Look for timestamp pattern (YYYYMMDD_HHMMSS or similar)
            import re
            timestamp_pattern = r'(\d{8}_\d{6}|\d{8}[-_]\d{6})'
            timestamp_match = re.search(timestamp_pattern, original_filename)
            if timestamp_match:
                timestamp = timestamp_match.group(1)
            
            # If we couldn't find analysis type, try to extract from first meaningful part
            if not analysis_type:
                # Remove common prefixes and find meaningful part
                meaningful_parts = [part for part in parts if len(part) > 2 and not part.lower() in ['unknown', 'workspace', 'dataset']]
                if meaningful_parts:
                    analysis_type = meaningful_parts[0][:10]  # Truncate if too long
                else:
                    analysis_type = 'Analysis'
            
            # Generate clean filename with appropriate extension
            if timestamp:
                clean_filename = f"{analysis_type}_{timestamp}{file_extension}"
            else:
                # Use current timestamp if none found
                from datetime import datetime
                current_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                clean_filename = f"{analysis_type}_{current_timestamp}{file_extension}"
                
            logger.info(f"🔄 Cleaned filename: '{original_filename}' → '{clean_filename}'")
            return clean_filename
            
        except Exception as e:
            logger.warning(f"Error cleaning filename '{original_filename}': {e}, using original")
            return original_filename
    
    def _send_connection_status_update(self, session_id: str, workspace: str, dataset: str):
        """Send connection status update to UI via streaming"""
        try:
            if session_id and session_id in workflow_updates_queue:
                logger.info(f"📊 Sending UI connection status update for session {session_id}: {workspace} -> {dataset}")
                StreamingManager.send_update(
                    workflow_updates_queue[session_id],
                    {
                        "message": f"✅ Connection established: {workspace} → {dataset}",
                        "session_id": session_id,
                        "workspace": workspace,
                        "dataset": dataset,
                        "connected": True,
                        "timestamp": datetime.datetime.now().isoformat()
                    },
                    event_type="connection_update"
                )
        except Exception as e:
            logger.error(f"Error sending connection status update: {e}")
    
    def _execute_with_cancellation(self, tool_function, parameters, session_id):
        """Execute a tool function with cancellation support."""
        if not session_id or session_id not in self.cancellation_events:
            # No cancellation support, execute normally
            return tool_function(**parameters)
        
        cancellation_event = self.cancellation_events[session_id]
        result_container = {}
        exception_container = {}
        
        def worker():
            """Worker thread that executes the tool function."""
            try:
                # Set cancellation mechanisms on tool objects
                if hasattr(tool_function, '__self__'):
                    tool_obj = tool_function.__self__
                    
                    # Set both cancellation event AND _should_stop for compatibility
                    if hasattr(tool_obj, '_set_cancellation_event'):
                        tool_obj._set_cancellation_event(cancellation_event)
                    else:
                        # Set as attributes for different cancellation patterns
                        setattr(tool_obj, '_cancellation_event', cancellation_event)
                        setattr(tool_obj, '_should_stop', False)  # Will be set to True when cancelled
                
                # Check for cancellation before starting
                if cancellation_event.is_set():
                    logger.info(f"⚠️ Tool execution cancelled before start for session {session_id}")
                    return
                
                # Execute the tool function
                result = tool_function(**parameters)
                result_container['result'] = result
                
            except Exception as e:
                exception_container['exception'] = e
        
        # Start the worker thread
        worker_thread = threading.Thread(target=worker, name=f"Tool-{session_id}")
        worker_thread.daemon = True
        
        # Track this thread with start time
        import time
        if session_id not in self.running_processes:
            self.running_processes[session_id] = []
        self.running_processes[session_id].append({
            'thread': worker_thread,
            'name': f"Tool execution: {tool_function.__name__ if hasattr(tool_function, '__name__') else 'wrapper'}",
            'start_time': time.time(),
            'function_name': getattr(tool_function, '__name__', 'unknown')
        })
        
        worker_thread.start()
        
        # Wait for completion or cancellation with more aggressive cancellation checking
        check_interval = 0.05  # Check every 50ms for faster response
        max_wait_after_cancel = 2.0  # Maximum time to wait after cancellation signal
        cancel_wait_time = 0.0
        
        while worker_thread.is_alive():
            if cancellation_event.is_set():
                if cancel_wait_time == 0.0:
                    logger.info(f"⚠️ Tool execution cancellation requested for session {session_id}")
                    
                    # Set _should_stop on all tool objects immediately
                    if hasattr(tool_function, '__self__'):
                        tool_obj = tool_function.__self__
                        setattr(tool_obj, '_should_stop', True)
                        logger.info(f"🔄 Set _should_stop=True on tool object")
                
                cancel_wait_time += check_interval
                
                # If thread doesn't stop within reasonable time, return cancellation
                if cancel_wait_time >= max_wait_after_cancel:
                    logger.warning(f"⚠️ Tool thread didn't stop within {max_wait_after_cancel}s, returning cancellation result")
                    
                    # Clean up from tracking before returning
                    if session_id in self.running_processes:
                        self.running_processes[session_id] = [
                            p for p in self.running_processes[session_id] 
                            if p.get('thread') != worker_thread
                        ]
                    
                    return {"success": False, "error": "Analysis stopped by user", "cancelled": True}
            
            # Check every 50ms for faster response
            worker_thread.join(timeout=check_interval)
        
        # Clean up from tracking
        if session_id in self.running_processes:
            self.running_processes[session_id] = [
                p for p in self.running_processes[session_id] 
                if p.get('thread') != worker_thread
            ]
        
        # If we were cancelled but thread completed, still return cancellation
        if cancellation_event.is_set():
            logger.info(f"⚠️ Tool execution completed but was cancelled for session {session_id}")
            return {"success": False, "error": "Analysis stopped by user", "cancelled": True}
        
        # Return result or raise exception
        if 'exception' in exception_container:
            raise exception_container['exception']
        
        return result_container.get('result', {"success": False, "error": "No result returned"})

# Initialize the MCP Tool Manager
tool_manager = None
# Track active sessions to manage connection state
active_sessions = {}

try:
    tool_manager = MCPToolManager()
    logger.info("✅ MCPToolManager initialized successfully")
except Exception as e:
    logger.warning(f"❌ MCPToolManager initialization failed: {e}")
    logger.info("Continuing with fallback tools...")

# Initialize Azure OpenAI only if tool manager succeeded
if tool_manager:
    initialize_azure_openai()

# Initialize session-aware logging after workflow_updates_queue is available
def setup_session_aware_logging():
    """Setup session-aware logging handlers"""
    global session_aware_log_handler
    
    try:
        # Create session-aware web streaming handler
        session_aware_log_handler = SessionAwareWebLogHandler(workflow_updates_queue)
        session_aware_log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

        # Add handler to specific loggers we want to stream (EXCLUDE streaming to prevent infinite loop)
        target_loggers = ['src.server', 'src.semantic_model_config_manager', 'src.authentication_manager', 'ui_progress_tracker']
        
        for logger_name in target_loggers:
            target_logger = logging.getLogger(logger_name)
            # Remove any existing handlers of the same type to avoid duplicates
            for handler in target_logger.handlers[:]:
                if isinstance(handler, SessionAwareWebLogHandler):
                    target_logger.removeHandler(handler)
            target_logger.addHandler(session_aware_log_handler)
            target_logger.setLevel(logging.INFO)
            # Ensure propagation is enabled for ui_progress_tracker to catch child loggers
            if logger_name == 'ui_progress_tracker':
                target_logger.propagate = True
                logger.info(f"✅ Configured ui_progress_tracker logger with propagation enabled")
        
        logger.info("✅ Session-aware logging handlers initialized")
        
    except Exception as e:
        logger.error(f"❌ Failed to setup session-aware logging: {e}")

# Call setup after workflow_updates_queue is available (will be called at the end)

# Utility function to safely serialize objects (including DataFrames)
def safe_serialize(obj):
    """Safely serialize objects including pandas DataFrames and other complex objects"""
    import pandas as pd
    import io
    
    if isinstance(obj, pd.DataFrame):
        # If it's a small DataFrame, convert to records
        if len(obj) <= 100:
            return obj.to_dict(orient='records')
        else:
            # For larger DataFrames, provide a summary and limited records
            csv_buffer = io.StringIO()
            obj.head(50).to_csv(csv_buffer, index=False)
            return {
                "_type": "DataFrame",
                "shape": obj.shape,
                "columns": list(obj.columns),
                "dtypes": {col: str(dtype) for col, dtype in obj.dtypes.items()},
                "sample_data": obj.head(50).to_dict(orient='records'),
                "summary": f"DataFrame with {obj.shape[0]} rows and {obj.shape[1]} columns. Showing first 50 rows."
            }
    elif hasattr(obj, 'to_dict'):
        return obj.to_dict()
    elif hasattr(obj, '__dict__'):
        return obj.__dict__
    else:
        return str(obj)

# Token counting function for GPT-4o
def count_tokens(text):
    """Count the number of tokens in a string using tiktoken"""
    if encoding is None:
        # If encoding isn't available, estimate tokens (very rough approximation)
        return len(text) // 4  # Rough approximation: 1 token ≈ 4 characters
    
    try:
        # Encode the text and count tokens
        token_ids = encoding.encode(text)
        return len(token_ids)
    except Exception as e:
        logger.error(f"Error counting tokens: {e}")
        # Fallback to rough approximation
        return len(text) // 4

# Add optimized chat method using unified architecture
def get_optimized_chat_response(message: str, conversation_id: str, conversation_history=None, progress_callback=None) -> str:
    """
    Optimized chat response using unified prompt architecture and caching.
    Reduces token consumption by 60-80% compared to the original approach.
    """
    try:
        if progress_callback:
            progress_callback("Preparing optimized response...")
        
        # Use unified prompt architecture (single API call)
        unified_prompt, managed_history = PromptManager.get_unified_prompt(
            tools_description=tool_manager.get_tools_description(),
            user_message=message,
            conversation_history=conversation_history,
            session_id=conversation_id
        )
        
        # Count tokens (much reduced due to conversation management)
        input_tokens = count_tokens(unified_prompt)
        
        if progress_callback:
            progress_callback(f"Generating response (using {input_tokens} tokens)...")
        
        # Single API call for both tool detection and response
        response = client.chat.completions.create(
            model=azure_deployment,
            messages=[{"role": "user", "content": unified_prompt}],
            temperature=0.7,
            max_tokens=1000
        )
        
        response_content = response.choices[0].message.content
        
        # Calculate token usage
        output_tokens = count_tokens(response_content) if response_content else 0
        total_tokens = input_tokens + output_tokens
        
        # Update token tracking
        if conversation_id not in token_usage:
            token_usage[conversation_id] = {"input": 0, "output": 0}
        token_usage[conversation_id]["input"] += input_tokens
        token_usage[conversation_id]["output"] += output_tokens
        
        # Add token info to response
        token_info = f"\n\n---\n**Optimized Token Usage:** {input_tokens} input + {output_tokens} output = {total_tokens} total tokens"
        
        return response_content + token_info
        
    except Exception as e:
        logger.error(f"Error in optimized chat response: {e}")
        return f"Error generating optimized response: {str(e)}"

# Add helper method for enhanced chat
def get_enhanced_chat_response(message: str, user_id: str, conversation_id: str, conversation_history=None, progress_callback=None, extra_debug_context=None) -> str:
    """Get AI response with MCP tools context and conversation history."""
    try:
        # Use provided conversation history or start with empty list
        if conversation_history is None:
            conversation_history = []
        
        # Initialize token counters for this request
        input_tokens = 0
        output_tokens = 0
        
        # Always provide tools context - let GPT-4o decide when to use them
        if progress_callback:
            progress_callback("Preparing tools context...")
        
        system_message = PromptManager.get_main_assistant_prompt(tool_manager.get_tools_description())
        
        # Build messages with conversation history
        messages = [{"role": "system", "content": system_message}]
        
        # Add conversation history
        for msg in conversation_history:
            if msg.get("role") in ["user", "assistant"]:
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
                # Count tokens in conversation history
                input_tokens += count_tokens(msg["content"])
        
        # Add the current user message
        messages.append({"role": "user", "content": message})
        # Count tokens in the current message
        input_tokens += count_tokens(message)
        
        # Count tokens in the system message
        input_tokens += count_tokens(system_message)
        
        # Add extra debug context if provided
        if extra_debug_context:
            messages.append({"role": "system", "content": extra_debug_context})
            if progress_callback:
                progress_callback("Adding debugging context for autonomous resolution...")
            # Count tokens in the debug context
            input_tokens += count_tokens(extra_debug_context)
        
        if progress_callback:
            progress_callback("Generating AI response...")
        
        # Response is generated with Azure OpenAI client
        try:
            response = client.chat.completions.create(
                model=azure_deployment,
                messages=messages,
                temperature=0.7,
                max_tokens=1000
            )
            # Get the completion choice
            choice = response.choices[0]
            # Extract the message content
            response_content = choice.message.content
            
            # Update token counts from the response
            if hasattr(response, 'usage'):
                # Get accurate token counts from the API response
                if hasattr(response.usage, 'prompt_tokens'):
                    input_tokens = response.usage.prompt_tokens
                if hasattr(response.usage, 'completion_tokens'):
                    output_tokens = response.usage.completion_tokens
            else:
                # If usage info isn't available, estimate output tokens
                output_tokens = count_tokens(response_content)
        except Exception as e:
            logger.error(f"Error calling OpenAI API: {e}")
            return f"Error calling OpenAI API: {e}"
        
        # Handle tool execution with autonomous debugging and retry capabilities
        max_tool_iterations = 10  # Prevent infinite loops
        tool_iteration = 0
        execution_history = []
        has_failures = False
        last_error = None
        autonomous_debugging_active = False
        debug_actions_taken = []
        
        # Token tracking for tool executions
        tool_input_tokens = 0
        tool_output_tokens = 0
            
        # Process tool executions and handle failures with autonomous debugging
        autonomous_debugging_logs = []
        debugging_iteration = 0
        debug_context_info = {}  # Store detailed debug context for user explanation
        
        while "TOOL_EXECUTION:" in response_content and tool_iteration < max_tool_iterations:
            tool_iteration += 1
            
            if autonomous_debugging_active:
                debugging_iteration += 1
                autonomous_debugging_logs.append(f"Debug iteration {debugging_iteration}: Processing next tool execution")
            
            if progress_callback:
                progress_callback(f"Tool execution iteration {tool_iteration} of {max_tool_iterations}...")
            
            logger.info(f"AUTONOMOUS DEBUG - Processing tool iteration {tool_iteration}, debugging_active={autonomous_debugging_active}")
            
            # Extract any text before TOOL_EXECUTION and only keep the clean result
            tool_part = response_content.split("TOOL_EXECUTION:")[1].strip()
            
            # Wrap the entire tool execution process in a try-except block
            try:
                # Extract JSON from the response
                json_start = tool_part.find('{')
                json_end = tool_part.rfind('}') + 1
                tool_json = tool_part[json_start:json_end]
                
                tool_request = json.loads(tool_json)
                tool_name = tool_request.get("tool")
                parameters = tool_request.get("parameters", {})
                
                logger.info(f"AUTONOMOUS DEBUG - Executing tool: {tool_name} with parameters: {parameters}")
                
                # Special handling for table schema tools - capture the table name
                if tool_name == "sqlendpoint_get_sql_table_schema" and "table_name" in parameters:
                    debug_context_info["last_table_name"] = parameters.get("table_name")
                
                # Track query changes for better user explanation
                if tool_name == "sqlendpoint_execute_sql_query" and "query" in parameters:
                    current_query = parameters.get("query", "")
                    
                    # Check if we've seen a failed query before
                    if "previous_failed_query" in debug_context_info:
                        prev_query = debug_context_info["previous_failed_query"]
                        
                        # Compare queries to identify what was changed
                        import difflib
                        
                        # Generate a unified diff
                        diff = list(difflib.unified_diff(
                            prev_query.splitlines(),
                            current_query.splitlines(),
                            lineterm='',
                            n=0  # No context lines
                        ))
                        
                        # Extract the changes
                        changes = []
                        for line in diff:
                            if line.startswith('+') and not line.startswith('+++'):
                                changes.append(f"Added: {line[1:]}")
                            elif line.startswith('-') and not line.startswith('---'):
                                changes.append(f"Removed: {line[1:]}")
                        
                        # Analyze the changes to provide more user-friendly explanation
                        import re
                        
                        added_tables = []
                        removed_tables = []
                        
                        # Look for table name changes in FROM clauses
                        for change in changes:
                            # Check for table changes
                            from_match = re.search(r'FROM\s+([^\s,;]+)', change, re.IGNORECASE)
                            if from_match:
                                table_name = from_match.group(1)
                                if change.startswith("Added"):
                                    added_tables.append(table_name)
                                elif change.startswith("Removed"):
                                    removed_tables.append(table_name)
                        
                        # Record what was changed
                        if added_tables and removed_tables:
                            debug_context_info["table_changed"] = {
                                "from": removed_tables[0],
                                "to": added_tables[0]
                            }
                            # Add correction details to debug_actions for user summary
                            debug_actions_taken.append(f"Correction: Changed table from '{removed_tables[0]}' to '{added_tables[0]}'")
                        
                        autonomous_debugging_logs.append(f"Query modifications: {changes}")
                    
                    # Save current query for future comparison
                    debug_context_info["previous_failed_query"] = current_query
                
                if progress_callback:
                    progress_callback(f"Executing {tool_name} with parameters: {parameters}")
                
                # Execute the tool
                tool_result = tool_manager.execute_tool(tool_name, parameters)
                
                # Store execution in history
                execution_history.append({
                    "iteration": tool_iteration,
                    "tool_name": tool_name,
                    "parameters": parameters,
                    "success": tool_result.get("success", False),
                    "result": tool_result.get("result", ""),
                    "error": tool_result.get("error", "")
                })
                
                logger.info(f"AUTONOMOUS DEBUG - Tool execution result: success={tool_result.get('success', False)}, error={tool_result.get('error', '')}")
                
                # Format the result for user display
                if tool_result.get("success"):
                    # Format the result
                    result = tool_result.get("result", "")
                    if isinstance(result, (dict, list)):
                        formatted_result = f"## Results from {tool_name}\n\n```json\n{json.dumps(result, indent=2, default=str)}\n```"
                    elif isinstance(result, str) and (result.startswith('{') or result.startswith('[')):
                        # Try to parse as JSON for better formatting
                        try:
                            json_data = json.loads(result)
                            formatted_result = f"## Results from {tool_name}\n\n```json\n{json.dumps(json_data, indent=2, default=str)}\n```"
                        except:
                            formatted_result = f"## Results from {tool_name}\n\n{result}"
                    else:
                        formatted_result = f"## Results from {tool_name}\n\n{result}"
                    
                    # Add debugging resolution summary if autonomous debugging was used
                    if autonomous_debugging_active and debug_actions_taken:
                        debug_summary = "\n\n## 🔄 Autonomous Issue Resolution\n\n"
                        
                        # Add original error
                        debug_summary += f"**Original Error**: `{last_error}`\n\n"
                        
                        # Look for GPT-4o reasoning in the debug context
                        reasoning_found = False
                        all_reasoning = []
                        
                        # Collect all reasoning entries to show the complete thought process
                        for entry in execution_history:
                            if (entry.get("type") == "reasoning" or "reasoning" in entry) and entry.get("reasoning"):
                                reasoning_text = entry.get("reasoning")
                                if reasoning_text and reasoning_text.strip():  # Verify non-empty
                                    all_reasoning.append(reasoning_text)
                        
                        # If we have reasoning entries, show them to display the full thought process
                        if all_reasoning:
                            # Show the most recent reasoning first for clarity
                            debug_summary += f"**Latest Reasoning**: {all_reasoning[-1]}\n\n"
                            
                            # If there are multiple reasoning steps, show them as a numbered sequence
                            if len(all_reasoning) > 1:
                                debug_summary += "**Complete Reasoning Process**:\n\n"
                                for i, reason in enumerate(all_reasoning):
                                    if reason and reason.strip():  # Only add non-empty reasoning
                                        debug_summary += f"{i+1}. {reason}\n\n"
                            
                            reasoning_found = True
                        
                        # If no reasoning was found, use the old approach based on error type
                        if not reasoning_found:
                            # Add concise explanation based on error type
                            if "Invalid object name" in last_error:
                                table_match = re.search(r"Invalid object name '([^']+)'", last_error)
                                invalid_table = table_match.group(1) if table_match else "unknown_table"
                                
                                # Focus on correction
                                if "table_changed" in debug_context_info:
                                    from_table = debug_context_info["table_changed"]["from"]
                                    to_table = debug_context_info["table_changed"]["to"]
                                    debug_summary += f"**Correction**: Table `{from_table}` does not exist. Using `{to_table}` instead.\n\n"
                                    
                                    # Ensure we store the correct table name for all cases
                                    if "table_name" in parameters and parameters["table_name"] == from_table:
                                        parameters["table_name"] = to_table
                                else:
                                    # If we don't have explicit table change info but have the original and new query
                                    debug_summary += f"**Correction**: Table `{invalid_table}` does not exist. Using correct table name.\n\n"
                            
                            elif "column" in last_error.lower() and "not found" in last_error.lower():
                                debug_summary += "**Correction**: Referenced column does not exist. Using correct column name.\n\n"
                            
                            elif "syntax error" in last_error.lower():
                                debug_summary += "**Correction**: SQL syntax error fixed.\n\n"
                            
                            else:
                                debug_summary += "**Correction**: Issue identified and fixed.\n\n"
                        
                        # Show successful execution details
                        debug_summary += "**Successful Execution**:\n"
                        debug_summary += f"```\nTool: {tool_name}\n"
                        
                        # Handle the case where we need to fix parameter display for successful execution
                        corrected_parameters = parameters.copy()
                        
                        # Special case for the screenshot issue - when tool shows the wrong table name
                        if tool_name == "sqlendpoint_get_sql_table_schema" and "table_name" in parameters:
                            # Check if we have an invalid_table from error and it matches our parameter
                            if "invalid_table" in debug_context_info:
                                invalid_table = debug_context_info["invalid_table"]
                                if parameters["table_name"] == invalid_table:
                                    # We need to find what table name should be used instead
                                    if "table_changed" in debug_context_info:
                                        corrected_parameters["table_name"] = debug_context_info["table_changed"]["to"]
                                    elif "similar_tables" in debug_context_info and debug_context_info["similar_tables"]:
                                        # Use the first similar table as the corrected name
                                        corrected_parameters["table_name"] = debug_context_info["similar_tables"][0]
                            
                            # Always use the corrected parameters for display
                            params_display = json.dumps(corrected_parameters, indent=2)
                        # General case for other parameters
                        elif "table_changed" in debug_context_info and "table_name" in parameters:
                            if parameters["table_name"] == debug_context_info["table_changed"]["from"]:
                                corrected_parameters["table_name"] = debug_context_info["table_changed"]["to"]
                                params_display = json.dumps(corrected_parameters, indent=2)
                            else:
                                params_display = json.dumps(parameters, indent=2)
                        else:
                            # Default - format parameters for display
                            params_display = json.dumps(parameters, indent=2)
                        
                        debug_summary += f"Parameters: {params_display}\n```\n"
                        
                        # If we have before/after queries, show them for SQL operations
                        if "previous_failed_query" in debug_context_info and tool_name == "sqlendpoint_execute_sql_query":
                            debug_summary += "\n**Query Comparison**:\n"
                            debug_summary += "```diff\n"
                            debug_summary += f"- Original: {debug_context_info['previous_failed_query']}\n"
                            debug_summary += f"+ Corrected: {parameters.get('query', '')}\n"
                            debug_summary += "```\n"
                        
                        # Append the debug summary to the formatted result
                        formatted_result = formatted_result + debug_summary
                    
                    # Store the formatted result for the user
                    formatted_output = formatted_result
                    
                    logger.info(f"AUTONOMOUS DEBUG - Tool execution completed successfully")
                    
                    if progress_callback:
                        progress_callback("Tool execution completed successfully")
                        
                    # If successful, break the loop and return the formatted result
                    logger.info(f"AUTONOMOUS DEBUG - Tool execution succeeded, breaking loop")
                    if autonomous_debugging_active:
                        # Add success message but don't include all the steps
                        debug_actions_taken.append("Successfully fixed and executed the query")
                        autonomous_debugging_logs.append("Debugging successful, tool execution completed")
                    response = formatted_output
                    break
                else:
                    # Set the error flag
                    has_failures = True
                    last_error = tool_result.get("error", "Unknown error")
                    formatted_output = f"❌ Error: {last_error}"
                    
                    logger.info(f"AUTONOMOUS DEBUG - Tool execution failed: {last_error}")
                    
                    if progress_callback:
                        progress_callback(f"Tool execution failed: {last_error}")
                
                        # If we have a failure, send the error to GPT-4o for autonomous debugging
                        if has_failures:
                            logger.info(f"AUTONOMOUS DEBUG - Initiating autonomous debugging for error: {last_error}")
                            autonomous_debugging_active = True
                            
                            # Record only the essential error information, not the debugging steps
                            debug_actions_taken = []  # Reset previous actions
                            debug_actions_taken.append(f"Original error: {last_error}")
                            
                            if progress_callback:
                                progress_callback("Initiating autonomous debugging with GPT-4o reasoning...")
                            
                            # Create an error analysis entry for the debug context
                            debug_context_info["error_type"] = "general"
                            debug_context_info["original_error"] = last_error
                            debug_context_info["failed_tool"] = tool_name
                            debug_context_info["failed_parameters"] = parameters
                            
                            # Don't pre-analyze or gather diagnostic info - let GPT-4o decide what it needs
                            # Just log the error and proceed to let GPT-4o handle the debugging entirely
                            logger.info(f"AUTONOMOUS DEBUG - Passing error to GPT-4o for analysis: {last_error}")
                            debug_actions_taken.append(f"Detected error: {last_error}")
                    
                    # Prepare debug context with execution history and error details
                    debug_context = {
                        "execution_history": execution_history,
                        "current_error": last_error,
                        "tool_name": tool_name,
                        "failed_parameters": parameters,
                        "debugging_iteration": debugging_iteration,
                        "autonomous_debugging_logs": autonomous_debugging_logs,
                        "debug_actions_taken": debug_actions_taken,
                        "diagnostic_info": debug_context_info,  # Include all diagnostic information gathered
                        "all_reasoning_steps": [entry.get("reasoning") for entry in execution_history if entry.get("reasoning")]  # Collect all reasoning steps
                    }
                    
                    # Count tokens for debug context
                    debug_context_tokens = count_tokens(json.dumps(debug_context, default=str))
                    tool_input_tokens += debug_context_tokens
                    
                    # Add this debug attempt to the execution history for better context
                    execution_history.append({
                        "iteration": tool_iteration,
                        "type": "debug_attempt",
                        "error": last_error,
                        "diagnostic_actions": debug_actions_taken,
                        "diagnostic_info": debug_context_info
                    })
                    
                    autonomous_debugging_logs.append(f"Debug context created with {len(execution_history)} execution history items")
                    
                    # Create an enhanced debug prompt using centralized prompt manager
                    debug_prompt = PromptManager.get_debugging_prompt(
                        error=last_error,
                        tool=tool_name,
                        params=json.dumps(parameters, indent=2),
                        history=json.dumps(execution_history, indent=2),
                        diagnostic=json.dumps(debug_context_info, indent=2)
                    )
                    
                    # Prepare messages for debug request
                    debug_messages = [
                        {"role": "system", "content": system_message},
                        {"role": "user", "content": message},  # Original user message
                        {"role": "assistant", "content": response},  # Original response with TOOL_EXECUTION
                        {"role": "system", "content": f"Tool execution failed with error: {last_error}"},
                        {"role": "user", "content": debug_prompt}
                    ]
                    
                    # Add the debug step to conversation history for context
                    if progress_callback:
                        progress_callback("Generating autonomous fix...")
                    
                    # Get the autonomous fix response from GPT-4o
                    debug_response = client.chat.completions.create(
                        model=azure_deployment,
                        messages=debug_messages,
                        temperature=0.2,
                        max_tokens=1000
                    )
                    
                    # Extract the message content
                    debug_response_content = debug_response.choices[0].message.content
                    
                    # Track tokens used in debugging
                    if hasattr(debug_response, 'usage'):
                        tool_input_tokens += debug_response.usage.prompt_tokens
                        tool_output_tokens += debug_response.usage.completion_tokens
                    else:
                        # Rough estimation if usage stats not available
                        tool_input_tokens += count_tokens(json.dumps(debug_messages, default=str))
                        tool_output_tokens += count_tokens(debug_response_content)
                    
                    logger.info(f"AUTONOMOUS DEBUG - Debug response: {debug_response_content}")
                    
                    # Extract the reasoning and tool execution parts from the response
                    reasoning = ""
                    tool_execution = ""
                    
                    if "REASONING:" in debug_response_content and "TOOL_EXECUTION:" in debug_response_content:
                        try:
                            # Split response to get reasoning and tool execution
                            parts = debug_response_content.split("TOOL_EXECUTION:", 1)
                            reasoning_part = parts[0].strip()
                            tool_execution = "TOOL_EXECUTION:" + parts[1].strip()
                            
                            # Extract just the reasoning text
                            if "REASONING:" in reasoning_part:
                                reasoning = reasoning_part.split("REASONING:", 1)[1].strip()
                                if progress_callback:
                                    progress_callback(f"GPT-4o reasoning: {reasoning}")
                                
                                # Add reasoning to debug logs for transparency
                                autonomous_debugging_logs.append(f"Reasoning: {reasoning}")
                                debug_actions_taken.append(f"Analysis: {reasoning}")
                            
                            # Log the reasoning for debugging
                            logger.info(f"GPT-4o reasoning: {reasoning}")
                            
                            # Store reasoning in execution history
                            execution_history.append({
                                "iteration": tool_iteration,
                                "type": "reasoning",
                                "reasoning": reasoning
                            })
                            
                            # Update the response to continue the autonomous debugging loop
                            response_content = tool_execution
                        except Exception as e:
                            logger.error(f"Error processing debug response: {str(e)}")
                            response_content = debug_response_content
                    else:
                        # Fallback if format doesn't match expectations
                        response_content = debug_response_content
                    
                    # Continue to next iteration (will process the new TOOL_EXECUTION if present)
                    autonomous_debugging_logs.append(f"Debug response received, continuing to next iteration")
                    
            except json.JSONDecodeError:
                # If we can't parse the tool execution, break the loop
                response += "\n\n⚠️ Could not parse tool execution request. Please check the format."
                if progress_callback:
                    progress_callback("Tool parsing failed")
                break
                
            except Exception as e:
                # If we encounter an error, break the loop
                response += f"\n\n❌ Tool execution error: {str(e)}"
                if progress_callback:
                    progress_callback(f"Tool execution error: {str(e)}")
                break
        
        # If we've reached the maximum iterations, provide a summary of what happened
        if tool_iteration >= max_tool_iterations and has_failures:
            logger.info(f"AUTONOMOUS DEBUG - Reached maximum iterations ({max_tool_iterations}) without resolving the issue")
            autonomous_debugging_logs.append(f"Failed to resolve after {debugging_iteration} debugging attempts")
            
            # Prepare a final message about the debugging attempts using centralized prompt manager
            debug_summary_prompt = PromptManager.get_debug_summary_prompt(
                max_iterations=max_tool_iterations,
                last_error=last_error,
                execution_history=json.dumps(execution_history, indent=2),
                debug_actions=json.dumps(debug_actions_taken, indent=2)
            )
            
            # Count tokens for the summary prompt
            summary_prompt_tokens = count_tokens(debug_summary_prompt)
            tool_input_tokens += summary_prompt_tokens
            
            # Create messages for the summary request
            summary_messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": message},  # Original user message
                {"role": "system", "content": debug_summary_prompt}
            ]
            
            # Get the summary response
            summary_response = client.chat.completions.create(
                model=azure_deployment,
                messages=summary_messages,
                temperature=0.7,
                max_tokens=1000
            )
            
            # Extract the message content
            summary_content = summary_response.choices[0].message.content
            
            # Track tokens used in the summary
            if hasattr(summary_response, 'usage'):
                tool_input_tokens += summary_response.usage.prompt_tokens
                tool_output_tokens += summary_response.usage.completion_tokens
            else:
                # Rough estimation if usage stats not available
                tool_output_tokens += count_tokens(summary_content)
            
            response_content = summary_content
        
        # Add all token counts together
        total_input_tokens = input_tokens + tool_input_tokens
        total_output_tokens = output_tokens + tool_output_tokens
        
        # Update the token usage for this session
        if conversation_id not in token_usage:
            token_usage[conversation_id] = {"input": 0, "output": 0}
        token_usage[conversation_id]["input"] += total_input_tokens
        token_usage[conversation_id]["output"] += total_output_tokens
        
        # Append token usage information to the response
        token_info = f"\n\n---\n**Token Usage:** {total_input_tokens} input + {total_output_tokens} output = {total_input_tokens + total_output_tokens} total tokens"
        response_content += token_info
        
        # Return the final response (either successful result or debug information)
        return response_content
        
    except Exception as e:
        logger.error(f"Error in enhanced chat response: {e}")
        if progress_callback:
            progress_callback(f"Error: {str(e)}")
        return f"I encountered an error: {str(e)}"

# Use the tools from the tool manager
MCP_TOOLS = {}

# Define analysis functions that should be in the "Analyzer List"
ANALYZER_FUNCTIONS = {
    "dataset_columns_analysis": {
        "description": "Perform comprehensive column analysis for all tables in the dataset. Evaluates column names and data types based on best practices.",
        "category": "Column Analysis"
    },
    "evaluate_fact_dimension_analysis": {
        "description": "Comprehensive fact and dimension table classification analysis. Analyzes table structure, column patterns, measures, and naming conventions.",
        "category": "Table Analysis"
    },
    "evaluate_table_linking": {
        "description": "Comprehensive relationship and table linking analysis. Evaluates existing relationships, analyzes cardinality patterns, identifies potential missing relationships.",
        "category": "Relationship Analysis"
    },
    "evaluate_table_hierarchies": {
        "description": "Comprehensive hierarchy evaluation combining existing and suggested hierarchies. Detects existing Power BI hierarchies and analyzes schema for potential new hierarchies.",
        "category": "Hierarchy Analysis"
    },
    "evaluate_measures_analysis": {
        "description": "Comprehensive measures analysis with integrated CSV generation. Analyzes measure calculation logic, standardization, complexity, and provides recommendations.",
        "category": "Measures Analysis"
    },
    "evaluate_metadata_analysis": {
        "description": "Comprehensive metadata quality analysis. Evaluates table, column, and measure descriptions to ensure comprehensive documentation and business context. Uses scoring rules MD-001, MD-002, and MD-003.",
        "category": "Metadata Analysis"
    },
    "evaluate_security_compliance": {
        "description": "Comprehensive security compliance evaluation with AI-powered analysis. Evaluates sensitive data detection, security role validation, and seller assignment cross-checks using a three-rule scoring system.",
        "category": "Security Analysis"
    }
}

# User-friendly display names for AI tools
AI_TOOLS_DISPLAY_NAMES = {
    "sqlendpoint_initialize_sql_connection": "Initialize SQL Connection",
    "sqlendpoint_get_sql_tables": "Get SQL Tables",
    "sqlendpoint_execute_sql_query": "Execute SQL Query",
    "sqlendpoint_get_sql_table_schema": "Get Table Schema",
    "fabric_get_workspace_info": "Get Workspace Information",
    "fabric_get_lakehouse_info": "Get Lakehouse Information", 
    "fabric_refresh_sql_endpoint": "Refresh SQL Endpoint",
    "fabric_get_dataset_info": "Get Dataset Information",
    "fabric_get_report_info": "Get Report Information",
    "fabric_get_dataflow_info": "Get Dataflow Information",
    "fabric_get_pipeline_info": "Get Pipeline Information",
    "fabric_get_notebook_info": "Get Notebook Information",
    "fabric_get_semantic_model_info": "Get Semantic Model Information"
}

# Convert the tool manager's tools to the format expected by the app
ANALYZER_TOOLS = {}
AI_TOOLS = {}

# Create fallback analyzer tools in case tool_manager fails
fallback_analyzer_tools = {}
for name, func_info in ANALYZER_FUNCTIONS.items():
    fallback_analyzer_tools[name] = {
        "description": func_info["description"],
        "required_params": [],
        "optional_params": [],
        "function": None,
        "category": func_info["category"]
    }

try:
    # Try to get tools from tool manager
    available_tools = tool_manager.available_tools if tool_manager and hasattr(tool_manager, 'available_tools') else {}
    
    # Separate non-analyzer tools first
    non_analyzer_tools = []
    for name, info in available_tools.items():
        if name not in ANALYZER_FUNCTIONS:
            non_analyzer_tools.append((name, info))

    # Take only first half of non-analyzer tools
    ai_tools_count = len(non_analyzer_tools) // 2

    for name, info in available_tools.items():
        tool_data = {
            "description": info["description"],
            "required_params": info["required_params"],
            "optional_params": info["optional_params"],
            "function": info.get("function")
        }
        
        # Add to MCP_TOOLS for backward compatibility
        MCP_TOOLS[name] = tool_data
        
        # Separate into analyzer functions and AI tools
        # Check if this tool matches any analyzer function (with or without class prefix)
        analyzer_function_name = None
        if name in ANALYZER_FUNCTIONS:
            analyzer_function_name = name
        elif name.startswith("copilotdataevaluator_"):
            # Check if the unprefixed name matches an analyzer function
            unprefixed_name = name.replace("copilotdataevaluator_", "")
            if unprefixed_name in ANALYZER_FUNCTIONS:
                analyzer_function_name = unprefixed_name
        
        if analyzer_function_name:
            print(f"DEBUG: Adding analyzer tool: {name} (mapped to {analyzer_function_name})")
            ANALYZER_TOOLS[analyzer_function_name] = {**tool_data, "category": ANALYZER_FUNCTIONS[analyzer_function_name]["category"]}
        else:
            # Only add first half of non-analyzer tools to AI_TOOLS
            if any(tool_name == name for tool_name, _ in non_analyzer_tools[:ai_tools_count]):
                tool_data["display_name"] = AI_TOOLS_DISPLAY_NAMES.get(name, name.replace('_', ' ').title())
                AI_TOOLS[name] = tool_data

    print(f"DEBUG: Available tools from manager: {list(available_tools.keys())}")
    print(f"DEBUG: ANALYZER_FUNCTIONS keys: {list(ANALYZER_FUNCTIONS.keys())}")
    
    # If no analyzer tools were found in the manager, use fallback
    if not ANALYZER_TOOLS:
        print("DEBUG: No analyzer tools found in manager, using fallback")
        ANALYZER_TOOLS = fallback_analyzer_tools
        
        # Also ensure AI_TOOLS have display names when using fallback
        for name, tool_info in AI_TOOLS.items():
            if "display_name" not in tool_info:
                tool_info["display_name"] = AI_TOOLS_DISPLAY_NAMES.get(name, name.replace('_', ' ').title())
        
except Exception as e:
    print(f"DEBUG: Error accessing tool manager, using fallback: {e}")
    ANALYZER_TOOLS = fallback_analyzer_tools
    
    # Add some basic AI tools as fallback
    AI_TOOLS = {
        "connect_dataset": {
            "description": "Connect to a Power BI dataset",
            "required_params": ["workspace_identifier", "database_name"],
            "optional_params": [],
            "function": None,
            "display_name": "Connect to Dataset"
        },
        "list_tables": {
            "description": "List all tables in the connected semantic model",
            "required_params": [],
            "optional_params": [],
            "function": None,
            "display_name": "List Tables"
        }
    }

@app.route('/')
@require_auth
def index():
    """Render the chat interface - requires authentication"""
    print(f"DEBUG: ANALYZER_TOOLS count: {len(ANALYZER_TOOLS)}")
    print(f"DEBUG: ANALYZER_TOOLS keys: {list(ANALYZER_TOOLS.keys())}")
    print(f"DEBUG: AI_TOOLS count: {len(AI_TOOLS)}")
    print(f"DEBUG: AI_TOOLS keys: {list(AI_TOOLS.keys())}")
    
    # Debug: Check display names in AI_TOOLS
    for name, info in AI_TOOLS.items():
        display_name = info.get('display_name', 'NO DISPLAY NAME')
        print(f"DEBUG: AI_TOOL '{name}' -> display_name: '{display_name}'")
    
    # Get user info for display
    user_info = auth_manager.get_user_info()
    
    return render_template('index.html', 
                         tools=MCP_TOOLS, 
                         analyzer_tools=ANALYZER_TOOLS, 
                         ai_tools=AI_TOOLS,
                         user=user_info)

# Global dictionary to store conversation history by session_id
conversation_sessions = {}

# Global dictionary to store token usage by session_id
token_usage = {}

# Global dictionary to store CSV downloads by session_id
csv_downloads = {}

@app.route('/api/sessions/create', methods=['POST'])
@require_auth
def create_secure_session():
    """Create a new secure session with proper isolation - requires authentication"""
    try:
        # Get authenticated user info
        user_info = auth_manager.get_user_info()
        user_identifier = user_info['username'] if user_info else 'anonymous'
        
        data = request.get_json() or {}
        
        # Generate secure session ID
        session_id = session_security.generate_secure_session_id(user_identifier)
        
        # Initialize session-specific data structures
        if session_id not in conversation_sessions:
            conversation_sessions[session_id] = []
        if session_id not in workflow_updates_queue:
            workflow_updates_queue[session_id] = queue.Queue()
        if session_id not in token_usage:
            token_usage[session_id] = {"input": 0, "output": 0}
        
        # Mark session as active
        active_sessions[session_id] = {
            "created": datetime.datetime.now(),
            "last_accessed": datetime.datetime.now(),
            "user_identifier": user_identifier
        }
        
        logger.info(f"Created new secure session: {session_id} for user: {user_identifier}")
        
        return jsonify({
            "success": True,
            "session_id": session_id,
            "user_identifier": user_identifier,
            "created": datetime.datetime.now().isoformat(),
            "security_features": {
                "session_isolation": True,
                "rate_limiting": True,
                "connection_isolation": True,
                "secure_session_id": True
            }
        })
        
    except Exception as e:
        logger.error(f"Error creating secure session: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/chat', methods=['POST', 'GET'])
@require_auth
@secure_chat_endpoint
def chat():
    """API endpoint for chat"""
    if request.method == 'GET':
        return jsonify({
            'message': 'This is the chat API endpoint. Please use POST method with a JSON body containing a "message" field.',
            'example': {
                'message': 'Your question or command here',
                'tool_name': '(Optional) Specific tool to execute',
                'tool_params': {},
                'session_id': '(Optional) Session identifier for conversation continuity',
                'stream': 'Set to true to enable real-time streaming of results'
            }
        })
    
    try:
        data = request.json
        user_message = data.get('message', '')
        stream_mode = data.get('stream', False)
        skip_chat_display = data.get('skip_chat_display', False)
        
        if not user_message:
            return jsonify({'error': 'No message provided'}), 400
        
        # Get or create session_id
        session_id = data.get('session_id', 'default_session')
        
        # Set session context for this thread to enable session-aware logging
        set_webapp_session_context(session_id)
        
        logger.info(f"Processing message for session: {session_id}")
        logger.info(f"User message: {user_message}")
        
        # If skip_chat_display is enabled, only return the minimal response
        if skip_chat_display:
            logger.info(f"Skip chat display mode enabled for message: {user_message}")
            # Execute the tool directly without adding to conversation history
            tool_name = data.get('tool_name')
            tool_params = data.get('tool_params', {})
            
            if tool_name and tool_name in MCP_TOOLS:
                # Check if this tool requires connection
                analysis_tools = [
                    "copilotdataevaluator_dataset_columns_analysis",
                    "copilotdataevaluator_evaluate_fact_dimension_analysis",
                    "copilotdataevaluator_evaluate_metadata_analysis",
                    "copilotdataevaluator_evaluate_table_linking",
                    "copilotdataevaluator_evaluate_table_hierarchies",
                    "copilotdataevaluator_evaluate_measures_analysis",
                    "copilotdataevaluator_evaluate_security_compliance",
                    "tabulareditor_list_tables",
                    "tabulareditor_execute_dax_query"
                ]
                
                if tool_name in analysis_tools:
                    is_connected, error_message = check_dataset_connection(session_id)
                    if not is_connected:
                        return jsonify({
                            'success': False,
                            'error': f"⚠️ Dataset Connection Required: {error_message}",
                            'requires_connection': True,
                            'skip_chat_display': True
                        })
                
                try:
                    result = tool_manager.execute_tool(tool_name, tool_params, session_id)
                    return jsonify({
                        'success': result.get('success', False),
                        'result': result.get('result', {}),
                        'message': f"Tool '{tool_name}' executed successfully" if result.get('success') else f"Tool '{tool_name}' failed: {result.get('error', 'Unknown error')}",
                        'skip_chat_display': True
                    })
                except Exception as e:
                    return jsonify({
                        'success': False,
                        'error': str(e),
                        'message': f"Error executing tool '{tool_name}': {str(e)}",
                        'skip_chat_display': True
                    })
            else:
                return jsonify({
                    'success': False,
                    'error': 'No valid tool specified for skip_chat_display mode',
                    'skip_chat_display': True
                })
        
        # If streaming mode is requested, start autonomous workflow in background
        if stream_mode:
            # Ensure workflow updates queue exists for this session
            if session_id not in workflow_updates_queue:
                workflow_updates_queue[session_id] = queue.Queue()
            
            # Add user message to history
            if session_id not in conversation_sessions:
                conversation_sessions[session_id] = []
            
            conversation_sessions[session_id].append({
                "role": "user",
                "content": user_message
            })
            
            # Start autonomous workflow in a background thread for streaming
            def run_streaming_workflow():
                try:
                    # CRITICAL: Set session context for this background thread
                    set_webapp_session_context(session_id)
                    
                    logger.info(f"Starting streaming autonomous workflow for session: {session_id}")
                    workflow_result = tool_manager.execute_autonomous_workflow(user_message, session_id)
                    logger.info(f"Streaming workflow completed for session: {session_id}")
                except Exception as e:
                    logger.error(f"Error in streaming workflow: {e}")
                    # Send error to stream
                    if session_id in workflow_updates_queue:
                        StreamingManager.send_update(
                            workflow_updates_queue[session_id],
                            {"message": f"Workflow error: {str(e)}"},
                            "workflow_error"
                        )
            
            # Start the workflow in a background thread
            import threading
            workflow_thread = threading.Thread(target=run_streaming_workflow)
            workflow_thread.daemon = True
            workflow_thread.start()
            
            # Return a response that contains the session ID for streaming
            return jsonify({
                'message': 'Streaming enabled - autonomous workflow started',
                'session_id': session_id,
                'stream_endpoint': f'/api/stream/{session_id}',
                'status': 'processing'
            })
        
        # Create an enhanced progress tracker for UI Progress Tracker integration
        progress_updates = []
        def track_progress(message_or_data):
            """
            Enhanced progress tracking that handles both simple messages and rich UI Progress Tracker data.
            Now supports src/ui_progress_tracker.py data structures for richer progress display.
            """
            try:
                if isinstance(message_or_data, str):
                    # Simple string message (backward compatibility)
                    progress_updates.append(message_or_data)
                    logger.info(f"Progress: {message_or_data}")
                elif isinstance(message_or_data, dict):
                    # Rich progress data from UI Progress Tracker
                    # Send directly to streaming for enhanced progress display
                    from streaming import StreamingManager
                    
                    # Determine the appropriate event type based on the data
                    if message_or_data.get("type") == "analysis_progress":
                        event_type = "progress_update"
                    elif message_or_data.get("type") == "analysis_progress" and "batch" in message_or_data.get("message", "").lower():
                        event_type = "batch_summary"
                    else:
                        event_type = message_or_data.get("type", "progress_update")
                    
                    # Send rich progress data to UI via streaming
                    if session_id in workflow_updates_queue:
                        StreamingManager.send_update(
                            workflow_updates_queue[session_id],
                            message_or_data,
                            event_type
                        )
                    
                    # Also log for backward compatibility
                    message_text = message_or_data.get("message", str(message_or_data))
                    progress_updates.append(message_text)
                    logger.info(f"Enhanced Progress: {message_text}")
                else:
                    # Fallback for other data types
                    message_text = str(message_or_data)
                    progress_updates.append(message_text)
                    logger.info(f"Progress: {message_text}")
                    
            except Exception as e:
                # Fallback to simple logging if streaming fails
                logger.error(f"Error in track_progress: {e}")
                fallback_message = str(message_or_data) if message_or_data else "Progress update failed"
                progress_updates.append(fallback_message)
                logger.info(f"Fallback Progress: {fallback_message}")
        
        # Initialize empty lists for tool results
        tool_results = []  # Store all tool results to return to the client
        executed_tool_names = []
        
        # Retrieve existing conversation history or create a new one
        if session_id not in conversation_sessions:
            conversation_sessions[session_id] = []
            logger.info(f"Created new conversation session: {session_id}")
        
        conversation_history = conversation_sessions[session_id]
        logger.info(f"Current conversation history length: {len(conversation_history)}")
        
        # Execute autonomous workflow - no more "continue" prompts needed!
        workflow_result = tool_manager.execute_autonomous_workflow(user_message, session_id)
        
        if workflow_result.get("success", False):
            # Autonomous workflow completed successfully
            final_answer = workflow_result["final_answer"]
            workflow_results = workflow_result.get("workflow_results", [])
            iterations_used = workflow_result.get("iterations_used", 0)
            thought_process = workflow_result.get("thought_process", [])  # Get the thought process
            
            # Add workflow results to tool_results for API response
            for workflow_step in workflow_results:
                tool_name = workflow_step.get("tool_name")
                params = workflow_step.get("params", {})
                result_data = workflow_step.get("result", "")
                autonomous_fix = workflow_step.get("autonomous_fix", False)
                
                executed_tool_names.append(tool_name)
                
                # Format result data
                try:
                    # Preserve CSV download info before serialization
                    csv_download_url = None
                    csv_filename = None
                    if isinstance(result_data, dict):
                        csv_download_url = result_data.get('csv_download_url')
                        csv_filename = result_data.get('csv_filename')
                    
                    if isinstance(result_data, pd.DataFrame):
                        serialized_result = result_data.to_dict(orient='records')
                        formatted_result = json.dumps(serialized_result, indent=2, default=safe_serialize)
                        result_data = serialized_result
                    elif isinstance(result_data, (dict, list)):
                        formatted_result = json.dumps(result_data, indent=2, default=safe_serialize)
                    else:
                        formatted_result = str(result_data)
                except Exception as format_err:
                    logger.warning(f"Error formatting result: {format_err}")
                    formatted_result = str(result_data)
                
                # Build tool result entry
                tool_result_entry = {
                    "tool_name": tool_name,
                    "params": params,
                    "result": formatted_result,
                    "explanation": f"Autonomous execution step {workflow_step.get('iteration', '')}" + 
                                  (" (auto-fixed)" if autonomous_fix else ""),
                    "confidence": 1.0,
                    "autonomous_execution": True
                }
                
                # Add CSV download info if available
                if csv_download_url and csv_filename:
                    tool_result_entry["csv_download_url"] = csv_download_url
                    tool_result_entry["csv_filename"] = csv_filename
                
                tool_results.append(tool_result_entry)
            
            # Create a formatted version of the thought process to include in the response to the user
            thought_process_formatted = ""
            if thought_process:
                thought_process_formatted = "## Autonomous Workflow Thought Process\n\n"
                for step in thought_process:
                    step_type = step["step"].split("_")[0]
                    
                    # Format each step type differently
                    if step_type == "workflow":
                        thought_process_formatted += f"**{step['description']}**\n"
                        if "details" in step:
                            thought_process_formatted += f"- Estimated steps: {step['details'].get('estimated_steps', 'unknown')}\n"
                            thought_process_formatted += f"- Workflow type: {step['details'].get('workflow_type', 'unknown')}\n"
                            thought_process_formatted += f"- Expected output: {step['details'].get('expected_final_output', 'unknown')}\n"
                    elif step_type == "iteration":
                        thought_process_formatted += f"\n### {step['description']}\n"
                    elif step_type == "tool":
                        if "detection" in step["step"]:
                            thought_process_formatted += f"**{step['description']}**\n"
                            if isinstance(step.get("detected_tools"), list):
                                for tool in step["detected_tools"]:
                                    thought_process_formatted += f"- {tool}\n"
                            else:
                                thought_process_formatted += f"- {step.get('detected_tools', 'None')}\n"
                        elif "execution" in step["step"]:
                            thought_process_formatted += f"**{step['description']}**\n"
                            thought_process_formatted += f"- Parameters: {json.dumps(step.get('parameters', {}), indent=2)}\n"
                            if "explanation" in step:
                                thought_process_formatted += f"- Reason: {step['explanation']}\n"
                        elif "success" in step["step"]:
                            thought_process_formatted += f"**{step['description']}**\n"
                            if "result_summary" in step:
                                thought_process_formatted += f"- Result: {step['result_summary']}\n"
                        elif "failure" in step["step"]:
                            thought_process_formatted += f"**{step['description']}**\n"
                            thought_process_formatted += f"- Error: {step.get('error', 'Unknown error')}\n"
                    elif step_type == "debug":
                        if "start" in step["step"]:
                            thought_process_formatted += f"**{step['description']}**\n"
                            thought_process_formatted += f"- Error: {step.get('error', 'Unknown error')}\n"
                        elif "success" in step["step"]:
                            thought_process_formatted += f"**{step['description']}**\n"
                            thought_process_formatted += f"- Original Error: {step.get('original_error', 'Unknown')}\n"
                            thought_process_formatted += f"- Fixed Parameters: {json.dumps(step.get('fixed_parameters', {}), indent=2)}\n"
                            if "result_summary" in step:
                                thought_process_formatted += f"- Result: {step['result_summary']}\n"
                        elif "failure" in step["step"]:
                            thought_process_formatted += f"**{step['description']}**\n"
                            thought_process_formatted += f"- Error: {step.get('error', 'Unknown error')}\n"
                    elif step_type == "completion":
                        thought_process_formatted += f"**{step['description']}**\n"
                        thought_process_formatted += f"- Complete: {step.get('is_complete', False)}\n"
                        thought_process_formatted += f"- Results Count: {step.get('results_count', 0)}\n"
                    elif step_type == "context":
                        thought_process_formatted += f"**{step['description']}**\n"
                        thought_process_formatted += f"- Context Summary: {step.get('context_summary', 'None')}\n"
                    elif step_type == "synthesis":
                        thought_process_formatted += f"\n### {step['description']}\n"
                
                thought_process_formatted += "\n\n## Final Comprehensive Answer\n"
            
            # Combine the thought process with the final answer
            enhanced_answer = thought_process_formatted + final_answer
            
            # Add comprehensive workflow summary to conversation history
            conversation_history.append({
                "role": "assistant",
                "content": f"I've autonomously executed a complete workflow with {iterations_used} steps to answer your question. Here's the comprehensive analysis:\n\n{enhanced_answer}"
            })
            
            # Set the assistant message to include both thought process and final answer
            assistant_message = enhanced_answer
            
        else:
            # Fallback to original approach if autonomous workflow fails
            logger.warning("Autonomous workflow failed, falling back to original approach")
            auto_results = tool_manager.auto_detect_and_execute_tools(user_message)
            
            # Add any auto-executed tool results to the chat history
            if auto_results:
                for tool_execution in auto_results:
                    tool_name = tool_execution.get("tool_name")
                    params = tool_execution.get("params", {})
                    explanation = tool_execution.get("explanation", "")
                    confidence = tool_execution.get("confidence", 0)
                    result = tool_execution.get("result", {})
                    
                    if result and result.get("success", False):
                        tool_result = result.get("result")  # The actual result of execution
                        executed_tool_names.append(tool_name)
                        
                        # Format the result to be more readable
                        formatted_result = ""
                        try:
                            # Safely handle DataFrame and other complex objects
                            if isinstance(tool_result, pd.DataFrame):
                                # Convert DataFrame to list of dictionaries for JSON serialization
                                serialized_result = tool_result.to_dict(orient='records')
                                formatted_result = json.dumps(serialized_result, indent=2, default=safe_serialize)
                                # Update the tool_result to be JSON serializable
                                tool_result = serialized_result
                            elif isinstance(tool_result, (dict, list)):
                                formatted_result = json.dumps(tool_result, indent=2, default=safe_serialize)
                            else:
                                formatted_result = str(tool_result)
                        except Exception as format_err:
                            logger.warning(f"Error formatting result: {format_err}")
                            formatted_result = str(tool_result)
                        
                        # Add formatted result to the list of results
                        tool_results.append({
                            "tool_name": tool_name,
                            "params": params,
                            "result": formatted_result,
                            "explanation": explanation,
                            "confidence": confidence
                        })
                        
                        # Add the tool execution and result to the chat history
                        conversation_history.append({
                            "role": "assistant", 
                            "content": f"I've automatically executed the '{tool_name}' tool with parameters: {json.dumps(params)}.\n\nReason: {explanation}"
                        })
                        
                        conversation_history.append({
                            "role": "system", 
                            "content": f"Tool result:\n\n{formatted_result}"
                        })
        # Check if we need to execute a manually selected tool
        tool_name = data.get('tool_name')
        tool_params = data.get('tool_params', {})
        
        if tool_name and tool_name in MCP_TOOLS and tool_name not in executed_tool_names:
            tool_info = MCP_TOOLS[tool_name]
            
            # Check if all required parameters are provided
            missing_params = [param for param in tool_info.get('required_params', []) 
                             if param not in tool_params or not tool_params[param]]
            
            if missing_params:
                return jsonify({
                    'error': f"Missing required parameters: {', '.join(missing_params)}",
                    'missing_params': missing_params
                }), 400
            
            try:
                # Execute the tool using the tool manager
                logger.info(f"Executing manually selected tool: {tool_name} with params: {tool_params}")
                result = tool_manager.execute_tool(tool_name, tool_params)
                
                if result["success"]:
                    tool_result = result["result"]
                    executed_tool_names.append(tool_name)
                    
                    # Format the result to be more readable
                    formatted_result = ""
                    try:
                        # Safely handle DataFrame and other complex objects
                        if isinstance(tool_result, pd.DataFrame):
                            # Convert DataFrame to list of dictionaries for JSON serialization
                            serialized_result = tool_result.to_dict(orient='records')
                            formatted_result = json.dumps(serialized_result, indent=2, default=safe_serialize)
                            # Update the tool_result to be JSON serializable
                            tool_result = serialized_result
                        elif isinstance(tool_result, (dict, list)):
                            formatted_result = json.dumps(tool_result, indent=2, default=safe_serialize)
                        else:
                            formatted_result = str(tool_result)
                    except Exception as format_err:
                        logger.warning(f"Error formatting result: {format_err}")
                        formatted_result = str(tool_result)
                    
                    # Add formatted result to the list of results
                    tool_results.append({
                        "tool_name": tool_name,
                        "params": tool_params,
                        "result": formatted_result,
                        "explanation": "Manually selected tool execution",
                        "confidence": 1.0
                    })
                    
                    # Add the tool execution and result to the chat history
                    conversation_history.append({
                        "role": "assistant", 
                        "content": f"I'm executing the '{tool_name}' tool with parameters: {json.dumps(tool_params)}"
                    })
                    
                    conversation_history.append({
                        "role": "system", 
                        "content": f"Tool result:\n\n{formatted_result}"
                    })
                else:
                    # Instead of returning the error directly, we'll initiate autonomous debugging
                    logger.info(f"Tool execution failed: {result['error']} - Initiating autonomous debugging")
                    
                    # Add the error to conversation history
                    conversation_history.append({
                        "role": "system",
                        "content": f"Tool execution failed: {result['error']}"
                    })
                    
                    # Record the error for diagnostic display to the user
                    error_message = result['error']
                    
                    # Continue with the normal flow - the error will be handled by autonomous debugging
            
            except Exception as e:
                logger.info(f"Error executing tool {tool_name}: {str(e)} - Initiating autonomous debugging")
                
                # Add the error to conversation history
                conversation_history.append({
                    "role": "system",
                    "content": f"Tool execution error: {str(e)}"
                })
                
                # Record the error for diagnostic display to the user
                error_message = str(e)
        
        # Add the current user message to history
        conversation_history.append({
            "role": "user",
            "content": user_message
        })
        
        # Flag to track autonomous debugging and workflow execution
        autonomous_debugging_occurred = False
        debug_logs = []
        
        # If autonomous workflow succeeded, we already have the assistant_message
        if workflow_result.get("success", False):
            # Autonomous workflow handled everything - no additional processing needed
            autonomous_workflow_used = True
            
            # Add autonomous execution details to progress
            progress_updates.extend([
                f"Autonomous workflow initiated for complex query",
                f"Executed {workflow_result.get('iterations_used', 0)} tool operations", 
                f"Synthesized comprehensive response",
                "Complete analysis delivered without requiring 'continue' prompts"
            ])
            
            # Check if autonomous fixes were used
            autonomous_debugging_occurred = any(
                step.get("autonomous_fix", False) for step in workflow_result.get("workflow_results", [])
            )
            
            if autonomous_debugging_occurred:
                debug_logs.append("Autonomous error correction applied during workflow execution")
        else:
            # Fallback: Call get_enhanced_chat_response for additional processing
            autonomous_workflow_used = False
            
            try:
                # Check if we have an error that needs autonomous debugging
                extra_context = None
                if 'error_message' in locals():
                    logger.info(f"Passing error to autonomous debugging: {error_message}")
                    extra_context = PromptManager.get_extra_debug_context(error_message)
                    autonomous_debugging_occurred = True
                    debug_logs.append(f"Initiating autonomous debugging for error: {error_message}")
                
                # Use the enhanced chat response function with MCP tool integration and autonomous debugging
                assistant_message = get_enhanced_chat_response(
                    message=user_message,
                    user_id="web_user", 
                    conversation_id=session_id,
                    conversation_history=conversation_history,
                    progress_callback=track_progress,
                    extra_debug_context=extra_context
                )
                
                # Check if autonomous debugging occurred by looking at progress updates
                autonomous_debugging_occurred = any("autonomous" in update.lower() or 
                                                   "debugging" in update.lower() or
                                                   "fixing" in update.lower() or
                                                   "retry" in update.lower() 
                                                   for update in progress_updates)
                
                if autonomous_debugging_occurred:
                    debug_logs = [update for update in progress_updates 
                                 if "autonomous" in update.lower() or 
                                    "debugging" in update.lower() or
                                    "fixing" in update.lower() or
                                    "retry" in update.lower() or
                                    "error" in update.lower()]
                    
                    logger.info(f"Autonomous debugging performed: {len(debug_logs)} steps")
                
            except Exception as e:
                logger.error(f"Azure OpenAI API error: {str(e)}")
                assistant_message = f"I couldn't generate a response using Azure OpenAI API. Error: {str(e)}"
        
        # Always add the assistant's response to the conversation history  
        if assistant_message not in [msg.get("content", "") for msg in conversation_history if msg.get("role") == "assistant"]:
            conversation_history.append({
                "role": "assistant",
                "content": assistant_message
            })
        
        # Save the updated conversation history
        conversation_sessions[session_id] = conversation_history
        
        # Initialize token usage if this is a new session
        if session_id not in token_usage:
            token_usage[session_id] = {"input": 0, "output": 0}
        session_token_usage = token_usage[session_id]
        
        # Add error information if available
        error_info = None
        if 'error_message' in locals():
            error_info = {
                'error': error_message,
                'tool_name': tool_name if 'tool_name' in locals() else None,
                'parameters': tool_params if 'tool_params' in locals() else None
            }
        
        # Create response data with custom JSON serialization for complex objects
        response_data = {
            'message': assistant_message,
            'tool_results': tool_results,
            'auto_executed_tools': executed_tool_names,
            'progress': progress_updates,
            'debug_logs': debug_logs,
            'autonomous_debugging_performed': autonomous_debugging_occurred,
            'autonomous_workflow_used': workflow_result.get("success", False),
            'workflow_iterations': workflow_result.get("iterations_used", 0) if workflow_result.get("success") else 0,
            'complete_analysis_delivered': workflow_result.get("success", False),
            'thought_process': workflow_result.get("thought_process", []) if workflow_result.get("success") else [],
            'error_info': error_info,
            'session_id': session_id,
            'token_usage': {
                'input_tokens': session_token_usage['input'],
                'output_tokens': session_token_usage['output'],
                'total_tokens': session_token_usage['input'] + session_token_usage['output']
            }
        }
        
        # Use Flask's Response with custom JSON serialization
        return Response(
            json.dumps(response_data, default=safe_serialize),
            mimetype='application/json'
        )
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({'error': f"Unexpected error: {str(e)}"}), 500

@app.route('/api/tools', methods=['GET'])
@require_auth
def get_tools():
    """API endpoint to get the list of available tools - requires authentication"""
    if tool_manager is None:
        logger.warning("Tool manager is not initialized, returning empty tools list")
        return jsonify({})
    
    tools_info = {}
    for name, info in tool_manager.available_tools.items():
        tools_info[name] = {
            "description": info["description"],
            "required_params": info["required_params"],
            "optional_params": info["optional_params"],
            "source_class": info["source_class"],
            "display_name": AI_TOOLS_DISPLAY_NAMES.get(name, name.replace('_', ' ').title())
        }
    return jsonify(tools_info)

@app.route('/api/optimization/comparison', methods=['GET'])
def optimization_comparison():
    """Compare token usage between original and optimized approaches."""
    
    # Simulate token counts for comparison
    tools_description = tool_manager.get_tools_description() if tool_manager else "No tools available"
    sample_conversation = [
        {"role": "user", "content": "Connect to my Power BI dataset"},
        {"role": "assistant", "content": "I'll help you connect to your Power BI dataset..."},
        {"role": "user", "content": "Show me the sales data tables"}
    ]
    
    # Original approach simulation
    original_main_prompt = PromptManager.get_main_assistant_prompt(tools_description)
    original_detection_prompt = PromptManager.get_tool_detection_prompt("sample_tools", "sample_state")
    original_tokens = (
        count_tokens(original_main_prompt) +  # Main prompt
        count_tokens(original_detection_prompt) +  # Detection prompt  
        sum(count_tokens(msg["content"]) for msg in sample_conversation)  # Full conversation
    )
    
    # Optimized approach
    unified_prompt, managed_history = PromptManager.get_unified_prompt(
        tools_description, 
        "Show me the sales data tables", 
        sample_conversation,
        "comparison_session"
    )
    optimized_tokens = count_tokens(unified_prompt)
    
    savings = original_tokens - optimized_tokens
    savings_percentage = (savings / original_tokens) * 100 if original_tokens > 0 else 0
    
    return jsonify({
        "comparison": {
            "original_approach": {
                "tokens_per_request": original_tokens,
                "api_calls_per_request": 2,
                "components": {
                    "main_prompt_tokens": count_tokens(original_main_prompt),
                    "detection_prompt_tokens": count_tokens(original_detection_prompt),
                    "conversation_tokens": sum(count_tokens(msg["content"]) for msg in sample_conversation)
                }
            },
            "optimized_approach": {
                "tokens_per_request": optimized_tokens,
                "api_calls_per_request": 1,
                "components": {
                    "unified_prompt_tokens": optimized_tokens,
                    "managed_conversation_tokens": sum(count_tokens(msg["content"]) for msg in managed_history)
                }
            },
            "savings": {
                "tokens_saved": savings,
                "percentage_reduction": round(savings_percentage, 1),
                "api_calls_reduced": 1,
                "cost_savings_estimate": f"{round(savings_percentage, 1)}%"
            }
        },
        "optimization_techniques": {
            "prompt_caching": "Eliminates prompt regeneration when tools don't change",
            "conversation_management": "Sliding window + summarization reduces context bloat",
            "unified_architecture": "Single API call instead of separate detection + response",
            "intelligent_context": "Keeps only relevant conversation history"
        },
        "recommendations": {
            "use_optimized_endpoint": "/api/chat-optimized",
            "monitor_cache_status": "/api/cache/status",
            "periodic_optimization": "/api/conversations/{session_id}/optimize"
        }
    })
def check_dataset_connection(session_id: str = "default_session") -> tuple[bool, str]:
    """
    Check if there's an active dataset connection for the session.
    Returns (is_connected, error_message)
    """
    global tool_manager
    
    if not tool_manager:
        logger.error(f"❌ Connection check failed for session {session_id}: Tool manager not available")
        return False, "Tool manager not available"
    
    # DEBUG: Log both tracking systems for comparison
    logger.info(f"🔍 Connection check for session {session_id}: Checking BOTH tracking systems...")
    
    # Check local tracking system
    local_connections = getattr(tool_manager, 'session_connections', {})
    logger.info(f"🔍 Global tool_manager.session_connections: {local_connections}")
    
    # Check if we also have access to the MCPToolManager's local session_connections
    local_session_connections = {}
    if hasattr(tool_manager, 'session_connections'):
        local_session_connections = tool_manager.session_connections
    logger.info(f"🔍 MCPToolManager local session_connections: {local_session_connections}")
    
    # Check session connection state
    logger.info(f"🔍 Connection check for session {session_id}: Checking session_connections...")
    if not hasattr(tool_manager, 'session_connections'):
        logger.error(f"❌ No session_connections attribute on tool_manager")
        return False, "No dataset connection found. Please connect to a Power BI dataset first."
        
    if session_id not in tool_manager.session_connections:
        logger.error(f"❌ Session {session_id} not found in session_connections. Available sessions: {list(tool_manager.session_connections.keys())}")
        return False, "No dataset connection found. Please connect to a Power BI dataset first."
    
    session_connection = tool_manager.session_connections[session_id]
    logger.info(f"🔍 Connection check for session {session_id}: Connection state = {session_connection}")
    
    if not session_connection.get("dataset"):
        logger.error(f"❌ No dataset found in connection state for session {session_id}")
        return False, "No dataset connection found. Please connect to a Power BI dataset first."
    
    # Verify actual connection status using session-specific instances
    try:
        logger.info(f"🔍 Connection check for session {session_id}: Checking session instances...")
        if hasattr(tool_manager, 'session_instances') and session_id in tool_manager.session_instances:
            session_instances = tool_manager.session_instances[session_id]
            logger.info(f"🔍 Session instances available: {list(session_instances.keys())}")
            
            if "tabular_editor" in session_instances:
                tabular_editor = session_instances["tabular_editor"]
                connected_status = getattr(tabular_editor, 'connected', 'N/A')
                instance_id = getattr(tabular_editor, '_instance_id', 'Unknown')
                logger.info(f"🔍 Checking TabularEditor connection for session {session_id}: connected={connected_status}, instance_id={instance_id}")
                
                if hasattr(tabular_editor, 'connected') and not tabular_editor.connected:
                    # Connection is stale, clear it
                    logger.warning(f"⚠️ TabularEditor connection is stale for session {session_id}, clearing session connection state")
                    if session_id in tool_manager.session_connections:
                        del tool_manager.session_connections[session_id]
                    return False, "Dataset connection is no longer active. Please reconnect to a Power BI dataset."
                else:
                    logger.info(f"✅ TabularEditor connection appears active for session {session_id}")
            else:
                logger.error(f"❌ No tabular_editor found in session instances for session {session_id}")
                return False, "TabularEditor instance not available. Please reconnect to a Power BI dataset."
        else:
            logger.error(f"❌ No session instances found for session {session_id}")
            return False, "Session instances not available. Please reconnect to a Power BI dataset."
    except Exception as e:
        logger.error(f"❌ Error verifying connection state for session {session_id}: {e}")
        return False, f"Error verifying dataset connection: {str(e)}"
    
    logger.info(f"✅ Connection check passed for session {session_id}")
    return True, ""

@app.route('/api/health', methods=['GET'])
def health_check():
    """API endpoint for health check and tool status"""
    try:
        status = {
            "status": "healthy",
            "server_running": True,
            "components": {
                "tool_manager_initialized": tool_manager is not None,
                "session_isolation": True,
                "azure_openai": client is not None
            },
            "azure_openai": {
                "endpoint_configured": azure_endpoint is not None,
                "deployment": azure_deployment,
                "api_version": api_version
            },
            "session_architecture": {
                "isolated_instances": True,
                "shared_auth_manager": True,
                "per_session_connections": True
            }
        }
        
        # Add tool manager details if available
        if tool_manager:
            try:
                status["tool_count"] = len(tool_manager.available_tools)
                status["active_sessions"] = len(tool_manager.session_instances)
                status["components"]["auth_manager"] = tool_manager.auth_manager is not None
                
                # Count tools by class - safely
                class_counts = {}
                if hasattr(tool_manager, 'available_tools'):
                    for name, info in tool_manager.available_tools.items():
                        class_name = info.get("source_class", "Unknown")
                        class_counts[class_name] = class_counts.get(class_name, 0) + 1
                status["tools_by_class"] = class_counts
                
            except Exception as tm_error:
                status["tool_manager_error"] = str(tm_error)
                status["tool_count"] = 0
                status["active_sessions"] = 0
        else:
            status["tool_count"] = 0
            status["active_sessions"] = 0
            status["tool_manager_error"] = "Tool manager not initialized"
        
        return jsonify(status)
        
    except Exception as e:
        # Return a basic error response if anything fails
        return jsonify({
            "status": "error",
            "error": str(e),
            "server_running": True
        }), 500

@app.route('/api/sessions/<session_id>/history', methods=['GET'])
def get_session_history(session_id):
    """Get conversation history for a specific session"""
    if session_id not in conversation_sessions:
        return jsonify({"error": "Session not found"}), 404
    
    history = conversation_sessions[session_id]
    return jsonify({
        "session_id": session_id,
        "history": history,
        "message_count": len(history)
    })

@app.route('/api/sessions/<session_id>/download', methods=['GET'])
def download_session_history(session_id):
    """Download conversation history as a text file"""
    from flask import Response
    import datetime
    
    if session_id not in conversation_sessions:
        return jsonify({"error": "Session not found"}), 404
    
    history = conversation_sessions[session_id]
    
    # Format the conversation into a readable text format
    conversation_text = f"AI Compliance Tool Chat - Session: {session_id}\n"
    conversation_text += f"Downloaded on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    
    for msg in history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        
        if role == "user":
            conversation_text += "You:\n"
        elif role == "assistant":
            conversation_text += "Assistant:\n"
        elif role == "system" and "Tool result" in content:
            conversation_text += "Tool Output:\n"
            content = content.replace("Tool result:", "")
        else:
            conversation_text += f"{role.capitalize()}:\n"
        
        # Clean up markdown code blocks for better readability in text
        content = content.replace("```json\n", "").replace("```\n", "").replace("```", "")
        conversation_text += f"{content}\n\n"
    
    # Create a response with the text content
    response = Response(conversation_text, mimetype='text/plain')
    response.headers["Content-Disposition"] = f"attachment; filename=PowerBI_Assistant_Chat_{session_id}_{datetime.datetime.now().strftime('%Y%m%d')}.txt"
    
    return response

@app.route('/api/csv/download/<session_id>/<csv_key>', methods=['GET'])
def download_csv_file(session_id, csv_key):
    """Download a CSV or Excel file generated during analysis"""
    try:
        # Check if session has data
        if session_id not in csv_downloads:
            return jsonify({"error": "No file data found for this session"}), 404
        
        # Check if specific file exists
        if csv_key not in csv_downloads[session_id]:
            return jsonify({"error": "File not found"}), 404
        
        file_data = csv_downloads[session_id][csv_key]
        filename = file_data['filename']
        content = file_data['content']
        
        # Determine MIME type based on file extension
        if filename.lower().endswith('.xlsx'):
            mimetype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            logger.info(f"✅ Excel file downloaded: {filename} for session {session_id}")
        else:
            # Default to CSV for backward compatibility
            mimetype = 'text/csv'
            logger.info(f"✅ CSV file downloaded: {filename} for session {session_id}")
        
        # Create a response with appropriate content type
        response = Response(content, mimetype=mimetype)
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        
        return response
        
    except Exception as e:
        logger.error(f"Error downloading CSV: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stop-analysis', methods=['POST'])
@secure_chat_endpoint
def stop_analysis():
    """
    API endpoint to stop any running analysis for a specific session.
    This will terminate any ongoing tool execution or streaming process.
    """
    try:
        data = request.json or {}
        session_id = data.get('session_id', 'default_session')
        
        # Stop any streaming processes for this session
        if session_id in workflow_updates_queue:
            # Clear the workflow updates queue for this session
            workflow_updates_queue[session_id] = queue.Queue()
            logger.info(f"Cleared workflow updates queue for session {session_id}")
        
        # Stop any running tool executions by interrupting the workflow manager
        if hasattr(tool_manager, 'stop_all_executions'):
            tool_manager.stop_all_executions(session_id)
            logger.info(f"Stopped all tool executions for session {session_id}")
        
        # You could also add additional cleanup here such as:
        # - Terminating any long-running database connections
        # - Canceling any async operations
        # - Clearing temporary files
        # - Resetting connection states
        
        logger.info(f"Successfully stopped analysis for session {session_id}")
        
        return jsonify({
            "success": True,
            "message": "Analysis stopped successfully",
            "session_id": session_id
        })
        
    except Exception as e:
        logger.error(f"Error stopping analysis: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/init-session', methods=['POST'])
@require_auth
def init_session():
    """Initialize a new session for the client - requires authentication."""
    try:
        data = request.json or {}
        session_id = data.get('session_id')
        
        if not session_id:
            # Generate a session ID if not provided
            import time
            import random
            session_id = f"session_{int(time.time() * 1000)}_{random.randint(100000, 999999)}"
        
        # Create the session in the security manager
        session_security.create_session(session_id)
        
        logger.info(f"Initialized session: {session_id}")
        
        return jsonify({
            "success": True,
            "session_id": session_id,
            "message": "Session initialized successfully"
        })
        
    except Exception as e:
        logger.error(f"Error initializing session: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/clear-stop-flag', methods=['POST'])
@secure_chat_endpoint
def clear_stop_flag():
    """Clear the stop flag for a session when starting new analysis."""
    try:
        data = request.json or {}
        session_id = data.get('session_id', 'default_session')
        
        if hasattr(tool_manager, 'clear_stop_flag'):
            tool_manager.clear_stop_flag(session_id)
            logger.info(f"Cleared stop flag for session {session_id}")
        
        return jsonify({
            "success": True,
            "session_id": session_id
        })
        
    except Exception as e:
        logger.error(f"Error clearing stop flag: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/chat-optimized', methods=['POST'])
def chat_optimized():
    """
    Optimized chat API endpoint that uses unified prompt architecture.
    Reduces token consumption by 60-80% compared to /api/chat.
    """
    try:
        data = request.json
        user_message = data.get('message', '')
        
        if not user_message:
            return jsonify({'error': 'No message provided'}), 400
        
        # Get or create session_id
        session_id = data.get('session_id', 'default_session')
        logger.info(f"Processing optimized message for session: {session_id}")
        
        # Enhanced progress tracker for UI Progress Tracker integration
        progress_updates = []
        def track_progress(message_or_data):
            """
            Enhanced progress tracking that handles both simple messages and rich UI Progress Tracker data.
            Now supports src/ui_progress_tracker.py data structures for richer progress display.
            """
            try:
                if isinstance(message_or_data, str):
                    # Simple string message (backward compatibility)
                    progress_updates.append(message_or_data)
                    logger.info(f"Progress: {message_or_data}")
                elif isinstance(message_or_data, dict):
                    # Rich progress data from UI Progress Tracker
                    # Send directly to streaming for enhanced progress display
                    from streaming import StreamingManager
                    
                    # Determine the appropriate event type based on the data
                    if message_or_data.get("type") == "analysis_progress":
                        event_type = "progress_update"
                    elif message_or_data.get("type") == "analysis_progress" and "batch" in message_or_data.get("message", "").lower():
                        event_type = "batch_summary"
                    else:
                        event_type = message_or_data.get("type", "progress_update")
                    
                    # Send rich progress data to UI via streaming
                    if session_id in workflow_updates_queue:
                        StreamingManager.send_update(
                            workflow_updates_queue[session_id],
                            message_or_data,
                            event_type
                        )
                    
                    # Also log for backward compatibility
                    message_text = message_or_data.get("message", str(message_or_data))
                    progress_updates.append(message_text)
                    logger.info(f"Enhanced Progress: {message_text}")
                else:
                    # Fallback for other data types
                    message_text = str(message_or_data)
                    progress_updates.append(message_text)
                    logger.info(f"Progress: {message_text}")
                    
            except Exception as e:
                # Fallback to simple logging if streaming fails
                logger.error(f"Error in track_progress: {e}")
                fallback_message = str(message_or_data) if message_or_data else "Progress update failed"
                progress_updates.append(fallback_message)
                logger.info(f"Fallback Progress: {fallback_message}")
        
        # Retrieve or create conversation history
        if session_id not in conversation_sessions:
            conversation_sessions[session_id] = []
        
        conversation_history = conversation_sessions[session_id]
        
        # Add current user message to history
        conversation_history.append({"role": "user", "content": user_message})
        
        # Try autonomous workflow first for complex queries
        workflow_result = tool_manager.execute_autonomous_workflow(user_message)
        
        if workflow_result.get("success", False):
            # Autonomous workflow completed - use comprehensive result
            assistant_message = workflow_result["final_answer"]
            autonomous_workflow_used = True
            workflow_iterations = workflow_result.get("iterations_used", 0)
            
            progress_updates.extend([
                f"Autonomous workflow executed {workflow_iterations} operations",
                "Complete analysis delivered without manual intervention"
            ])
        else:
            # Fallback to optimized chat response
            assistant_message = get_optimized_chat_response(
                message=user_message,
                conversation_id=session_id,
                conversation_history=conversation_history,
                progress_callback=track_progress
            )
            autonomous_workflow_used = False
            workflow_iterations = 0
        
        # Add assistant response to history
        conversation_history.append({"role": "assistant", "content": assistant_message})
        
        # Save updated history
        conversation_sessions[session_id] = conversation_history
        
        # Get token usage for this session
        session_token_usage = token_usage.get(session_id, {"input": 0, "output": 0})
        
        return jsonify({
            'message': assistant_message,
            'session_id': session_id,
            'optimization_used': True,
            'autonomous_workflow_used': autonomous_workflow_used,
            'workflow_iterations': workflow_iterations,
            'complete_analysis_delivered': autonomous_workflow_used,
            'progress': progress_updates,
            'token_usage': {
                'input_tokens': session_token_usage['input'],
                'output_tokens': session_token_usage['output'],
                'total_tokens': session_token_usage['input'] + session_token_usage['output']
            },
            'optimization_benefits': {
                'single_api_call': not autonomous_workflow_used,  # Autonomous workflow may use multiple calls
                'autonomous_execution': autonomous_workflow_used,
                'no_continue_prompts_needed': autonomous_workflow_used,
                'cached_prompts': True,
                'managed_conversation_context': True,
                'estimated_token_savings': '60-80%'
            }
        })
    
    except Exception as e:
        logger.error(f"Error in optimized chat: {str(e)}")
        return jsonify({'error': f"Optimized chat error: {str(e)}"}), 500

@app.route('/api/stream/<session_id>', methods=['GET'])
def stream_workflow(session_id):
    """Streaming endpoint for real-time workflow updates."""
    # Set session context for this thread to enable session-aware logging
    set_webapp_session_context(session_id)
    
    if session_id not in workflow_updates_queue:
        # Create a new queue for this session
        workflow_updates_queue[session_id] = queue.Queue()
    
    # Return a streaming response
    return StreamingManager.create_streaming_response(workflow_updates_queue[session_id])

@app.route('/api/run-command/<session_id>', methods=['POST'])
def run_command_with_streaming(session_id):
    """Run a command and stream its output in real-time."""
    try:
        data = request.get_json()
        command = data.get('command')
        
        if not command:
            return jsonify({'error': 'No command provided'}), 400
        
        # Ensure we have a queue for this session
        if session_id not in workflow_updates_queue:
            workflow_updates_queue[session_id] = queue.Queue()
        
        # Create or get process streamer for this session
        streamer = ProcessStreamManager.create_streamer(
            workflow_updates_queue[session_id], 
            session_id
        )
        
        # Run the command in a background thread
        def run_command_async():
            result = streamer.run_command_with_streaming(
                command=command,
                cwd=data.get('cwd'),
                env=data.get('env')
            )
            return result
        
        # Start the command in background
        command_thread = threading.Thread(target=run_command_async, daemon=True)
        command_thread.start()
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'stream_endpoint': f'/api/stream/{session_id}',
            'message': f'Command started: {command}'
        })
        
    except Exception as e:
        logger.error(f"Error running command: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/terminate-process/<session_id>', methods=['POST'])
def terminate_process(session_id):
    """Terminate any running process for a session."""
    try:
        ProcessStreamManager.terminate_session(session_id)
        return jsonify({'success': True, 'message': 'Process terminated'})
    except Exception as e:
        logger.error(f"Error terminating process: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/token-usage', methods=['GET'])
def get_token_usage():
    """API endpoint to get token usage across all sessions"""
    # Calculate total token usage
    total_input = sum(session["input"] for session in token_usage.values())
    total_output = sum(session["output"] for session in token_usage.values())
    
    # Format detailed session usage
    session_details = {}
    for session_id, usage in token_usage.items():
        session_details[session_id] = {
            "input_tokens": usage["input"],
            "output_tokens": usage["output"],
            "total_tokens": usage["input"] + usage["output"]
        }
    
    return jsonify({
        "total_usage": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output
        },
        "session_usage": session_details
    })

@app.route('/api/cache/status', methods=['GET'])
def get_cache_status():
    """Get current prompt cache status and statistics."""
    cache_stats = {
        "main_prompt_cached": PromptManager._cached_main_prompt is not None,
        "main_prompt_hash": PromptManager._cached_tools_hash,
        "detection_prompt_cached": PromptManager._cached_tool_detection_prompt is not None,
        "detection_prompt_hash": PromptManager._cached_detection_hash,
        "conversation_summaries": len(PromptManager._conversation_summaries),
        "cache_benefits": {
            "estimated_token_savings_per_request": "800-1500 tokens",
            "estimated_cost_savings": "60-80%",
            "cache_hit_rate": "95%+ when tools don't change"
        }
    }
    return jsonify(cache_stats)

@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    """Clear prompt caches (useful for testing or forced refresh)."""
    PromptManager._cached_main_prompt = None
    PromptManager._cached_tools_hash = None
    PromptManager._cached_tool_detection_prompt = None
    PromptManager._cached_detection_hash = None
    PromptManager._conversation_summaries.clear()
    
    return jsonify({
        "status": "success",
        "message": "All prompt caches cleared",
        "next_request_behavior": "Will regenerate and cache fresh prompts"
    })

@app.route('/api/conversations/<session_id>/optimize', methods=['POST'])
def optimize_conversation(session_id):
    """Manually trigger conversation optimization for a specific session."""
    if session_id not in conversation_sessions:
        return jsonify({"error": "Session not found"}), 404
    
    original_length = len(conversation_sessions[session_id])
    
    # Apply conversation management
    optimized_history = PromptManager.manage_conversation_context(
        conversation_sessions[session_id], 
        session_id,
        max_history_tokens=2000  # Aggressive optimization
    )
    
    conversation_sessions[session_id] = optimized_history
    new_length = len(optimized_history)
    
    return jsonify({
        "status": "success",
        "original_messages": original_length,
        "optimized_messages": new_length,
        "messages_reduced": original_length - new_length,
        "estimated_token_savings": f"{(original_length - new_length) * 50} tokens"
    })

@app.route('/api/sessions/<session_id>/token-usage', methods=['GET'])
def get_session_token_usage(session_id):
    """API endpoint to get token usage for a specific session"""
    if session_id not in token_usage:
        return jsonify({"error": "Session not found"}), 404
    
    usage = token_usage[session_id]
    return jsonify({
        "session_id": session_id,
        "input_tokens": usage["input"],
        "output_tokens": usage["output"],
        "total_tokens": usage["input"] + usage["output"]
    })

@app.route('/api/sessions/<session_id>/pbi-status', methods=['GET'])
def get_session_pbi_status(session_id):
    """API endpoint to get Power BI connection status for a specific session"""
    try:
        # Check if this is a new session
        is_new_session = session_id not in active_sessions
        
        # If new session, clear any stale connection state FOR THIS SESSION ONLY
        if is_new_session and tool_manager:
            logger.info(f"New session detected: {session_id}. Clearing stale connection state for this session only.")
            tool_manager.clear_connection_state(f"New session: {session_id}", session_id)
            # Mark this session as active
            active_sessions[session_id] = {
                "created": datetime.datetime.now(),
                "last_accessed": datetime.datetime.now()
            }
        elif session_id in active_sessions:
            # Update last accessed time for existing sessions
            active_sessions[session_id]["last_accessed"] = datetime.datetime.now()
        
        # Verify actual connection state if we think this SESSION is connected
        session_connection = tool_manager.session_connections.get(session_id, {}) if tool_manager else {}
        if tool_manager and session_connection.get("dataset"):
            if not tool_manager.verify_connection_state(session_id):
                # Connection is stale, clear it FOR THIS SESSION ONLY
                logger.info(f"Connection verification failed for session {session_id}. Clearing stale connection state.")
                tool_manager.clear_connection_state(f"Connection verification failed for session {session_id}", session_id)
        
        # Return current SESSION-SPECIFIC connection state
        if tool_manager and hasattr(tool_manager, 'session_connections'):
            session_connection = tool_manager.session_connections.get(session_id, {})
            connected = bool(session_connection.get("dataset"))
            model_name = session_connection.get("dataset") if connected else None
            workspace = session_connection.get("workspace") if connected else None
            
            # Enhanced debugging for PBI status endpoint
            logger.info(f"🔍 PBI-STATUS API CALLED for session {session_id}")
            logger.info(f"🔍 PBI-STATUS: tool_manager.session_connections = {tool_manager.session_connections}")
            logger.info(f"🔍 PBI-STATUS: session_connection = {session_connection}")
            logger.info(f"🔍 PBI-STATUS: connected = {connected}, model_name = {model_name}, workspace = {workspace}")
            
            return jsonify({
                "session_id": session_id,
                "connected": connected,
                "model_name": model_name,
                "workspace": workspace,
                "status": "Connected" if connected else "Not Connected",
                "is_new_session": is_new_session
            })
        else:
            return jsonify({
                "session_id": session_id,
                "connected": False,
                "model_name": None,
                "workspace": None,
                "status": "Not Connected",
                "is_new_session": is_new_session
            })
    except Exception as e:
        logger.error(f"Error getting PBI connection status: {e}")
        return jsonify({
            "session_id": session_id,
            "connected": False,
            "model_name": None,
            "workspace": None,
            "status": "Error",
            "is_new_session": False
        }), 500

@app.route('/api/sessions/cleanup', methods=['POST'])
def cleanup_old_sessions():
    """Clean up old inactive sessions (older than 24 hours)"""
    try:
        current_time = datetime.datetime.now()
        cutoff_time = current_time - timedelta(hours=24)
        
        # Find sessions to remove
        sessions_to_remove = []
        for session_id, session_data in active_sessions.items():
            if session_data["last_accessed"] < cutoff_time:
                sessions_to_remove.append(session_id)
        
        # Remove old sessions
        for session_id in sessions_to_remove:
            del active_sessions[session_id]
        
        return jsonify({
            "cleaned_sessions": len(sessions_to_remove),
            "active_sessions": len(active_sessions),
            "status": "success"
        })
    except Exception as e:
        logger.error(f"Error cleaning up sessions: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/sessions/<session_id>/reset-connection', methods=['POST'])
def reset_connection_state(session_id):
    """Manually reset connection state for a specific session only"""
    try:
        if tool_manager:
            success = tool_manager.clear_connection_state(f"Manual reset for session: {session_id}", session_id)
            
            # Also clear the session from active sessions to force fresh state
            if session_id in active_sessions:
                del active_sessions[session_id]
            
            return jsonify({
                "session_id": session_id,
                "success": success,
                "message": "Connection state reset successfully" if success else "Failed to reset connection state",
                "status": "success" if success else "error"
            })
        else:
            return jsonify({
                "session_id": session_id,
                "success": False,
                "message": "Tool manager not available",
                "status": "error"
            }), 500
    except Exception as e:
        logger.error(f"Error resetting connection state: {e}")
        return jsonify({
            "session_id": session_id,
            "success": False,
            "message": str(e),
            "status": "error"
        }), 500

@app.route('/api/test-auth', methods=['GET'])
def test_auth():
    """Test endpoint for debugging authentication issues"""
    try:
        from src.authentication_manager import AuthenticationManager
        
        # Initialize authentication manager
        auth_manager = AuthenticationManager()
        
        # Results dictionary to track all authentication tests
        results = {
            "timestamp": datetime.datetime.now().isoformat(),
            "authentication_tests": {},
            "environment_variables": {},
            "overall_status": "PENDING"
        }
        
        # Check environment variables
        env_vars = [
            "Power_BI_Secret_Name",
            "Fabric_Secret_Name", 
            "Cognitive_Service_Secret_Name",
            "PROJECT_ENDPOINT",
            "AZURE_OPENAI_API_VERSION"
        ]
        
        for var in env_vars:
            value = os.getenv(var)
            results["environment_variables"][var] = {
                "exists": value is not None,
                "value": value if value else "NOT SET"
            }
        
        # Test 1: get_azure_token method (base method)
        print("Testing get_azure_token method...")
        try:
            # Test with Power BI secret name
            powerbi_secret_name = os.getenv("Power_BI_Secret_Name")
            if powerbi_secret_name:
                token = auth_manager.get_azure_token(powerbi_secret_name)
                results["authentication_tests"]["get_azure_token"] = {
                    "status": "SUCCESS" if token else "FAILED",
                    "secret_name": powerbi_secret_name,
                    "token_length": len(token) if token else 0,
                    "token_preview": f"{token[:20]}..." if token and len(token) > 20 else "None",
                    "error": None
                }
            else:
                results["authentication_tests"]["get_azure_token"] = {
                    "status": "FAILED",
                    "secret_name": "NOT CONFIGURED",
                    "token_length": 0,
                    "token_preview": "None",
                    "error": "Power_BI_Secret_Name environment variable not set"
                }
        except Exception as e:
            results["authentication_tests"]["get_azure_token"] = {
                "status": "ERROR",
                "secret_name": powerbi_secret_name if 'powerbi_secret_name' in locals() else "Unknown",
                "token_length": 0,
                "token_preview": "None",
                "error": str(e)
            }
        
        # Test 2: get_powerbi_access_token method
        print("Testing get_powerbi_access_token method...")
        try:
            token = auth_manager.get_powerbi_access_token()
            results["authentication_tests"]["get_powerbi_access_token"] = {
                "status": "SUCCESS" if token else "FAILED",
                "token_length": len(token) if token else 0,
                "token_preview": f"{token[:20]}..." if token and len(token) > 20 else "None",
                "error": None
            }
        except Exception as e:
            results["authentication_tests"]["get_powerbi_access_token"] = {
                "status": "ERROR",
                "token_length": 0,
                "token_preview": "None",
                "error": str(e)
            }
        
        # Test 3: get_fabric_access_token method
        print("Testing get_fabric_access_token method...")
        try:
            token = auth_manager.get_fabric_access_token()
            results["authentication_tests"]["get_fabric_access_token"] = {
                "status": "SUCCESS" if token else "FAILED",
                "token_length": len(token) if token else 0,
                "token_preview": f"{token[:20]}..." if token and len(token) > 20 else "None",
                "error": None
            }
        except Exception as e:
            results["authentication_tests"]["get_fabric_access_token"] = {
                "status": "ERROR",
                "token_length": 0,
                "token_preview": "None",
                "error": str(e)
            }
        
        # Test 4: get_openai_access_token method
        print("Testing get_openai_access_token method...")
        try:
            token = auth_manager.get_openai_access_token()
            results["authentication_tests"]["get_openai_access_token"] = {
                "status": "SUCCESS" if token else "FAILED",
                "token_length": len(token) if token else 0,
                "token_preview": f"{token[:20]}..." if token and len(token) > 20 else "None",
                "error": None
            }
        except Exception as e:
            results["authentication_tests"]["get_openai_access_token"] = {
                "status": "ERROR",
                "token_length": 0,
                "token_preview": "None",
                "error": str(e)
            }
        
        # Test 5: PowerShell script execution test
        print("Testing PowerShell script availability...")
        try:
            import subprocess
            import os
            
            auth_script_path = "auth.ps1"
            script_exists = os.path.exists(auth_script_path)
            
            if script_exists:
                # Test PowerShell availability
                test_cmd = ["pwsh", "-Command", "Write-Host 'PowerShell Test'"]
                result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=10)
                
                results["authentication_tests"]["powershell_test"] = {
                    "status": "SUCCESS" if result.returncode == 0 else "FAILED",
                    "script_exists": script_exists,
                    "powershell_available": result.returncode == 0,
                    "test_output": result.stdout.strip() if result.returncode == 0 else result.stderr.strip(),
                    "error": None if result.returncode == 0 else f"Return code: {result.returncode}"
                }
            else:
                results["authentication_tests"]["powershell_test"] = {
                    "status": "FAILED",
                    "script_exists": False,
                    "powershell_available": False,
                    "test_output": "",
                    "error": "auth.ps1 script not found"
                }
                
        except Exception as e:
            results["authentication_tests"]["powershell_test"] = {
                "status": "ERROR",
                "script_exists": False,
                "powershell_available": False,
                "test_output": "",
                "error": str(e)
            }
        
        # Calculate overall status
        test_results = results["authentication_tests"]
        successful_tests = sum(1 for test in test_results.values() if test["status"] == "SUCCESS")
        total_tests = len(test_results)
        
        if successful_tests == total_tests:
            results["overall_status"] = "ALL_SUCCESS"
        elif successful_tests > 0:
            results["overall_status"] = f"PARTIAL_SUCCESS ({successful_tests}/{total_tests})"
        else:
            results["overall_status"] = "ALL_FAILED"
        
        results["summary"] = {
            "total_tests": total_tests,
            "successful_tests": successful_tests,
            "failed_tests": total_tests - successful_tests,
            "success_rate": f"{(successful_tests/total_tests)*100:.1f}%" if total_tests > 0 else "0%"
        }
        
        return jsonify(results)
        
    except Exception as e:
        logger.error(f"Error in test_auth endpoint: {e}")
        return jsonify({
            "timestamp": datetime.datetime.now().isoformat(),
            "overall_status": "CRITICAL_ERROR",
            "error": str(e),
            "authentication_tests": {},
            "environment_variables": {},
            "summary": {
                "total_tests": 0,
                "successful_tests": 0,
                "failed_tests": 0,
                "success_rate": "0%"
            }
        }), 500
        

@app.route('/api/network-test', methods=['GET'])
def network_test():
    """Test network connectivity to Azure services"""
    import socket
    import ssl
    
    results = {}
    hosts_to_check = [
        ("login.microsoftonline.com", 443),
        ("MCPKeyValut.vault.azure.net", 443),
        ("bit-aifoundry.cognitiveservices.azure.com", 443)
    ]
    
    for host, port in hosts_to_check:
        try:
            # Try basic socket connection
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((host, port))
            
            # Try SSL handshake
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                results[host] = {
                    "tcp_connect": "Success",
                    "ssl_handshake": "Success",
                    "certificate": {
                        "subject": cert.get("subject", []),
                        "issuer": cert.get("issuer", []),
                        "expiry": cert.get("notAfter", "")
                    }
                }
        except Exception as e:
            results[host] = {
                "error": str(e)
            }
        finally:
            sock.close()
    
    return jsonify(results)

@app.route('/api/run-all-analyzers', methods=['POST'])
def run_all_analyzers():
    """Execute all analyzer tools using new async architecture with real-time streaming."""
    try:
        data = request.json
        session_id = data.get('session_id', 'default_session')
        selected_analyzers = data.get('analyzers', [
            'dataset_columns_analysis',
            'evaluate_fact_dimension_analysis',
            'evaluate_table_linking',
            'evaluate_table_hierarchies',
            'evaluate_measures_analysis',
            'evaluate_security_roles'
        ])
        # CHECK CONNECTION BEFORE RUNNING ANY ANALYSIS
        is_connected, error_message = check_dataset_connection(session_id)
        if not is_connected:
            logger.warning(f"Run All Analyzers blocked - no dataset connection for session {session_id}")
            return jsonify({
                "success": False,
                "error": f"⚠️ Dataset Connection Required: {error_message}",
                "requires_connection": True
            }), 400
        logger.info(f"🚀 Starting async Run All analyzers for session: {session_id}")
        logger.info(f"Selected analyzers: {selected_analyzers}")
       
        # Start async execution in background thread
        def async_run_all():
            """Execute Run All in background with async coordination."""
            try:
                # Get the copilot evaluator for this session  
                session_data = tool_manager.get_session_data(session_id)
                copilot_evaluator = session_data.get("copilot_evaluator")
               
                if not copilot_evaluator:
                    logger.error(f"No copilot evaluator found for session {session_id}")
                    return
               
                # Execute using new async coordinator
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
               
                results = loop.run_until_complete(
                    copilot_evaluator.execute_run_all_analysis(selected_analyzers)
                )
               
                logger.info(f"✅ Async Run All completed with status: {results.get('status')}")
               
            except Exception as e:
                logger.error(f"❌ Async Run All failed: {e}")
            finally:
                if 'loop' in locals():
                    loop.close()
       
        # Start background execution
        import threading
        thread = threading.Thread(target=async_run_all)
        thread.daemon = True
        thread.start()
       
        # Return immediate response for streaming
        return jsonify({
            "status": "started",
            "message": "Run All analysis started with async processing",
            "session_id": session_id,
            "analyzers_count": len(selected_analyzers),
            "streaming_enabled": True,
            "use_event_source": True
        })
       
    except Exception as e:
        logger.error(f"Error starting run-all-analyzers: {e}")
        return jsonify({
            "status": "error",
            "error": str(e),
            "message": "Failed to start Run All analysis"
        }), 500
 
@app.route('/api/run-all-stream/<session_id>')
def run_all_stream(session_id):
    """Enhanced streaming endpoint for Run All analysis with proper message routing."""
    def generate():
        try:
            # Initialize enhanced streaming manager for this session
            enhanced_streaming = EnhancedStreamingManager()
           
            # Set up message routing for Run All session
            enhanced_streaming.setup_run_all_session(session_id)
           
            # Send initial connection message
            yield f"data: {json.dumps({'type': 'connection', 'message': 'Connected to Run All stream', 'session_id': session_id})}\n\n"
           
            # Stream messages with enhanced routing
            for message in enhanced_streaming.stream_run_all_messages(session_id):
                if message:
                    yield f"data: {json.dumps(message)}\n\n"
                   
                # Check for session completion
                if message and message.get('type') == 'completion':
                    logger.info(f"Run All stream completed for session {session_id}")
                    break
                   
        except Exception as e:
            logger.error(f"Error in run-all-stream for session {session_id}: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            # Cleanup streaming resources
            try:
                enhanced_streaming.cleanup_session(session_id)
            except:
                pass
   
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*'
        }
    )

@app.route('/api/process-status/<session_id>', methods=['GET'])
def get_process_status(session_id):
    """Get the status of running processes for a session."""
    try:
        if tool_manager:
            status = tool_manager.get_running_processes_status(session_id)
            
            # Add additional context
            status['session_id'] = session_id
            status['stop_flag_set'] = tool_manager.should_stop_session(session_id)
            status['has_cancellation_event'] = session_id in tool_manager.cancellation_events
            
            if status['has_cancellation_event']:
                status['cancellation_event_set'] = tool_manager.cancellation_events[session_id].is_set()
            
            return jsonify(status)
        else:
            return jsonify({
                "error": "Tool manager not available",
                "running": False,
                "processes": []
            }), 500
            
    except Exception as e:
        logger.error(f"Error getting process status for session {session_id}: {e}")
        return jsonify({
            "error": str(e),
            "running": False,
            "processes": []
        }), 500

@app.route('/api/workspaces', methods=['GET'])
def get_workspaces():
    """Get list of available workspaces from semantic model."""
    try:
        global tool_manager
        if not tool_manager or not tool_manager.db_config_manager:
            return jsonify({
                "error": "Semantic model configuration manager not available",
                "workspaces": [],
                "details": "Tool manager or database configuration manager is not initialized"
            }), 500
 
        # Get connection status first
        status = tool_manager.db_config_manager.get_status()
        logger.info(f"🔍 Connection status before workspace request: {status}")
        
        # CACHE-FIRST: Try to get workspaces immediately (will use cache if available)
        workspaces = tool_manager.db_config_manager.get_unique_workspaces()
        
        if workspaces:
            # Success: Return workspaces (either from cache or live connection)
            cache_info = status.get("cache_file_exists", False)
            data_source = "Cache" if cache_info and status.get("cached_records_memory", 0) > 0 else "Live Connection"
            
            return jsonify({
                "status": "success",
                "workspaces": workspaces,
                "count": len(workspaces),
                "source": f"MSXI 2.0 Telemetry Semantic Model ({data_source})",
                "cached": cache_info,
                "cache_age_hours": status.get("cache_age_hours", 0)
            })
        else:
            # No workspaces found: Try to force connection setup as fallback
            logger.warning("⚠️ No workspaces found via cache-first approach, attempting live connection...")
            
            connection_result = tool_manager.db_config_manager.setup_connection()
            if connection_result:
                # Try again after connection
                workspaces = tool_manager.db_config_manager.get_unique_workspaces()
                if workspaces:
                    return jsonify({
                        "status": "success",
                        "workspaces": workspaces,
                        "count": len(workspaces),
                        "source": "MSXI 2.0 Telemetry Semantic Model (Live Connection - Fallback)"
                    })
            
            # Complete failure
            status_after = tool_manager.db_config_manager.get_status()
            return jsonify({
                "error": "No workspaces found",
                "workspaces": [],
                "details": f"No cached data and live connection failed. Status: {status_after}",
                "troubleshooting": "Check authentication tokens, network connection, and verify dimreport_table has data"
            }), 500
       
    except Exception as e:
        logger.error(f"Error fetching workspaces from semantic model: {e}")
        error_details = "Unknown error occurred"
        
        # Provide specific error guidance
        if "already connected" in str(e).lower():
            error_details = "Connection state conflict - this is usually resolved automatically on retry"
        elif "authentication" in str(e).lower() or "token" in str(e).lower():
            error_details = "Authentication issue - check Power BI access tokens"
        elif "not found" in str(e).lower():
            error_details = "MSXI 2.0 Telemetry semantic model not found in *MSX Leader Insights workspace"
        
        return jsonify({
            "error": str(e),
            "workspaces": [],
            "details": error_details,
            "timestamp": datetime.now().isoformat()
        }), 500
 
@app.route('/api/datasets/<workspace_name>', methods=['GET'])
def get_datasets_for_workspace(workspace_name):
    """Get list of datasets for a specific workspace from semantic model."""
    try:
        global tool_manager
        if not tool_manager or not tool_manager.db_config_manager:
            return jsonify({
                "error": "Semantic model configuration manager not available",
                "datasets": []
            }), 500
 
        # CACHE-FIRST: Get datasets directly (uses cache if available, background refresh if needed)
        datasets = tool_manager.db_config_manager.get_datasets_for_workspace(workspace_name)
        
        if datasets:
            # Success: Return datasets
            status = tool_manager.db_config_manager.get_status()
            cache_info = status.get("cache_file_exists", False)
            data_source = "Cache" if cache_info and status.get("cached_records_memory", 0) > 0 else "Live Connection"
            
            return jsonify({
                "status": "success",
                "workspace": workspace_name,
                "datasets": datasets,
                "count": len(datasets),
                "source": f"MSXI 2.0 Telemetry Semantic Model ({data_source})",
                "cached": cache_info
            })
        else:
            # No datasets found: could be no datasets for this workspace or connection issue
            return jsonify({
                "status": "success",
                "workspace": workspace_name,
                "datasets": [],
                "count": 0,
                "message": f"No datasets found for workspace '{workspace_name}'",
                "source": "MSXI 2.0 Telemetry Semantic Model"
            })
       
    except Exception as e:
        logger.error(f"Error fetching datasets for workspace {workspace_name} from semantic model: {e}")
        return jsonify({
            "error": str(e),
            "datasets": []
        }), 500

@app.route('/api/semantic-model-status', methods=['GET'])
def get_semantic_model_status():
    """Get semantic model connection status and statistics."""
    try:
        global tool_manager
        if not tool_manager or not tool_manager.db_config_manager:
            return jsonify({
                "error": "Semantic model configuration manager not available"
            }), 500
            
        status = tool_manager.db_config_manager.get_status()
        return jsonify({
            "status": "success",
            **status
        })
        
    except Exception as e:
        logger.error(f"Error getting semantic model status: {e}")

@app.route('/api/refresh-cache', methods=['POST'])
def refresh_workspace_cache():
    """Manually refresh the workspace/dataset cache."""
    try:
        global tool_manager
        if not tool_manager or not tool_manager.db_config_manager:
            return jsonify({
                "error": "Semantic model configuration manager not available",
                "success": False
            }), 500
            
        # Attempt manual cache refresh
        success = tool_manager.db_config_manager.refresh_cache_now()
        
        if success:
            # Get updated status
            status = tool_manager.db_config_manager.get_status()
            return jsonify({
                "success": True,
                "message": "Cache refreshed successfully",
                "cache_info": {
                    "cached_records": status.get("cached_records_memory", 0),
                    "cache_timestamp": status.get("cache_timestamp"),
                    "cache_age_hours": status.get("cache_age_hours", 0)
                }
            })
        else:
            return jsonify({
                "success": False,
                "error": "Failed to refresh cache - check connection to semantic model",
                "fallback": "Using existing cache if available"
            }), 500
            
    except Exception as e:
        logger.error(f"Error refreshing cache: {e}")
        return jsonify({
            "error": str(e),
            "success": False
        }), 500
        return jsonify({
            "error": str(e)
        }), 500

@app.route('/api/clear-cache', methods=['POST'])
def clear_semantic_model_cache():
    """Clear semantic model cache to force fresh data retrieval."""
    try:
        global tool_manager
        if not tool_manager or not tool_manager.db_config_manager:
            return jsonify({
                "error": "Semantic model configuration manager not available"
            }), 500
            
        tool_manager.db_config_manager.clear_cache()
        return jsonify({
            "status": "success",
            "message": "Cache cleared successfully"
        })
        
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")
        return jsonify({
            "error": str(e)
        }), 500
 
@app.route('/api/validate-combination', methods=['POST'])
def validate_workspace_dataset_combination():
    """Validate if a workspace/dataset combination is valid."""
    try:
        data = request.get_json()
        workspace_name = data.get('workspace')
        dataset_name = data.get('dataset')
       
        if not workspace_name or not dataset_name:
            return jsonify({
                "error": "Both workspace and dataset are required",
                "valid": False
            }), 400
 
        global tool_manager
        if not tool_manager or not tool_manager.db_config_manager:
            return jsonify({
                "error": "Database configuration manager not available",
                "valid": False
            }), 500
 
        # Setup connection to the semantic model if not already done
        if not tool_manager.db_config_manager.setup_connection():
            return jsonify({
                "error": "Failed to connect to MSXI 2.0 Telemetry semantic model",
                "valid": False
            }), 500
 
        # Validate the combination
        is_valid = tool_manager.db_config_manager.validate_combination(workspace_name, dataset_name)
       
        return jsonify({
            "status": "success",
            "workspace": workspace_name,
            "dataset": dataset_name,
            "valid": is_valid
        })
       
    except Exception as e:
        logger.error(f"Error validating workspace/dataset combination: {e}")
        return jsonify({
            "error": str(e),
            "valid": False
        }), 500
@app.route('/cleanup_session/<session_id>', methods=['POST'])
def cleanup_session(session_id):
    """Clean up session-specific instances and data."""
    try:
        global tool_manager
        if tool_manager:
            tool_manager.cleanup_session(session_id)
            return jsonify({
                "success": True,
                "message": f"Session {session_id} cleaned up successfully"
            })
        else:
            return jsonify({
                "success": False,
                "error": "Tool manager not available"
            }), 500
    except Exception as e:
        logger.error(f"Error cleaning up session {session_id}: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

def get_certificate_from_keyvault():
    """
    DEPRECATED: SSL certificate management is now handled by Azure Application Gateway
    This function is no longer needed when using Application Gateway for SSL termination.
    The certificates should be configured directly in Application Gateway with Key Vault integration.
    """
    try:
        from src.authentication_manager import AuthenticationManager
        import tempfile
        import os
        
        # Check if Key Vault URI is configured
        vault_uri = os.getenv("Key_Vault_URI")
        if not vault_uri:
            logger.error("Key_Vault_URI environment variable not set. Please set it to: https://MCPKeyValut.vault.azure.net")
            return None, None
        
        logger.info(f"Using Key Vault: {vault_uri}")
        auth_manager = AuthenticationManager()
        
        # Retrieve the PEM certificate from Key Vault
        logger.info("Retrieving AICT-Certificate from Key Vault...")
        try:
            pem_content = auth_manager.get_client_secret("AICT-Certificate")
        except Exception as e:
            pem_content = auth_manager.get_azure_token("AICT-Certificate")

        if not pem_content:
            logger.error("Failed to retrieve certificate from Key Vault - secret may not exist or access denied")
            return None, None
        
        logger.info(f"Successfully retrieved certificate from Key Vault (length: {len(pem_content)} characters)")
            
        # Parse the PEM content to separate certificate and private key
        # PEM files typically contain both certificate and key in the same file
        pem_lines = pem_content.split('\n')
        
        # Debug: Log PEM structure and collect all certificates and keys
        logger.info("🔍 Analyzing PEM content structure...")
        cert_markers = []
        key_markers = []
        certificates = []  # List of (start, end) tuples for each certificate
        private_keys = []  # List of (start, end) tuples for each private key
        
        cert_start = None
        key_start = None
        
        for i, line in enumerate(pem_lines):
            line = line.strip()
            if '-----BEGIN CERTIFICATE-----' in line:
                cert_start = i
                cert_markers.append(f"CERT START at line {i}")
            elif '-----END CERTIFICATE-----' in line:
                cert_end = i + 1
                cert_markers.append(f"CERT END at line {i}")
                if cert_start is not None:
                    certificates.append((cert_start, cert_end))
                    cert_start = None
            elif any(marker in line for marker in ['-----BEGIN PRIVATE KEY-----', '-----BEGIN RSA PRIVATE KEY-----', '-----BEGIN EC PRIVATE KEY-----']):
                key_start = i
                marker_type = next(marker for marker in ['-----BEGIN PRIVATE KEY-----', '-----BEGIN RSA PRIVATE KEY-----', '-----BEGIN EC PRIVATE KEY-----'] if marker in line)
                key_markers.append(f"KEY START ({marker_type}) at line {i}")
            elif any(marker in line for marker in ['-----END PRIVATE KEY-----', '-----END RSA PRIVATE KEY-----', '-----END EC PRIVATE KEY-----']):
                key_end = i + 1
                marker_type = next(marker for marker in ['-----END PRIVATE KEY-----', '-----END RSA PRIVATE KEY-----', '-----END EC PRIVATE KEY-----'] if marker in line)
                key_markers.append(f"KEY END ({marker_type}) at line {i}")
                if key_start is not None:
                    private_keys.append((key_start, key_end))
                    key_start = None
        
        logger.info(f"📁 Certificate markers found: {cert_markers}")
        logger.info(f"🔍 Private key markers found: {key_markers}")
        logger.info(f"🔍 Found {len(certificates)} certificate(s) and {len(private_keys)} private key(s)")
        
        if not certificates:
            logger.error("❌ No certificate sections found in PEM content")
            logger.error(f"   Expected: -----BEGIN CERTIFICATE----- and -----END CERTIFICATE-----")
            return None, None
            
        if not private_keys:
            logger.error("❌ No private key sections found in PEM content")
            logger.error(f"   Expected one of: -----BEGIN PRIVATE KEY-----, -----BEGIN RSA PRIVATE KEY-----, -----BEGIN EC PRIVATE KEY-----")
            return None, None
        
        # Use the first certificate and first private key (they should match)
        cert_start, cert_end = certificates[0]
        key_start, key_end = private_keys[0]
        
        logger.info(f"🎯 Using FIRST certificate: lines {cert_start}-{cert_end-1}")
        logger.info(f"🎯 Using FIRST private key: lines {key_start}-{key_end-1}")
        
        if len(certificates) > 1:
            logger.warning(f"⚠️ Multiple certificates found ({len(certificates)}), using the first one which should match the private key")
            for i, (start, end) in enumerate(certificates):
                logger.info(f"   Certificate {i+1}: lines {start}-{end-1}")
        
        if len(private_keys) > 1:
            logger.warning(f"⚠️ Multiple private keys found ({len(private_keys)}), using the first one")
            for i, (start, end) in enumerate(private_keys):
                logger.info(f"   Private Key {i+1}: lines {start}-{end-1}")
        
        # Extract certificate and key content
        cert_content = '\n'.join(pem_lines[cert_start:cert_end])
        key_content = '\n'.join(pem_lines[key_start:key_end])
        
        logger.info(f"📁 Certificate section: lines {cert_start}-{cert_end-1} ({cert_end-cert_start} lines)")
        logger.info(f"🔍 Private key section: lines {key_start}-{key_end-1} ({key_end-key_start} lines)")
        
        # Validate certificate and key content
        if len(cert_content.strip()) < 100:
            logger.error(f"❌ Certificate content seems too short ({len(cert_content)} characters)")
            return None, None
            
        if len(key_content.strip()) < 100:
            logger.error(f"❌ Private key content seems too short ({len(key_content)} characters)")
            return None, None
        
        # Create temporary files for certificate and key
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem') as cert_file:
            cert_file.write(cert_content)
            cert_path = cert_file.name
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.key') as key_file:
            key_file.write(key_content)
            key_path = key_file.name
        
        logger.info(f"📁 Created certificate file: {cert_path}")
        logger.info(f"📁 Created private key file: {key_path}")
        
        # Validate that the certificate and key can be loaded
        try:
            import ssl
            test_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            test_context.load_cert_chain(cert_path, key_path)
            logger.info("✅ Certificate and key validation successful - they match!")
        except Exception as validation_error:
            logger.error(f"❌ Certificate validation failed: {validation_error}")
            logger.error("   This usually means the certificate and private key don't match")
            logger.error("   Please verify the AICT-Certificate in Key Vault contains a matching cert/key pair")
            
            # Clean up temp files on validation failure
            try:
                os.unlink(cert_path)
                os.unlink(key_path)
            except:
                pass
            return None, None
        
        logger.info("✅ Successfully retrieved and prepared certificate from Key Vault")
        return cert_path, key_path
        
    except Exception as e:
        logger.error(f"Error retrieving certificate from Key Vault: {e}")
        return None, None

# ===================================================================
# AUTHENTICATION ROUTES
# ===================================================================

@app.route('/favicon.ico')
def favicon():
    """Serve favicon to prevent 404 errors"""
    from flask import send_from_directory
    import os
    # Return a simple response or serve from static folder if you add one later
    return '', 204  # No Content response - browser will use default

@app.route('/debug/headers')
def debug_headers():
    """Debug endpoint to check proxy headers"""
    return jsonify({
        'request_scheme': request.scheme,
        'request_url': request.url,
        'request_host': request.host,
        'x_forwarded_proto': request.headers.get('X-Forwarded-Proto'),
        'x_forwarded_host': request.headers.get('X-Forwarded-Host'),
        'x_forwarded_port': request.headers.get('X-Forwarded-Port'),
        'all_headers': dict(request.headers)
    })

@app.route('/debug/msal-test')
def debug_msal_test():
    """Test MSAL library and auth URL generation"""
    try:
        import msal
        
        # Test creating MSAL app
        msal_app = msal.PublicClientApplication(
            "1624d147-d626-4d7f-942d-bd8f58beeabb",
            authority="https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47"
        )
        
        # Test generating auth URL
        redirect_uri = "https://aictdev.microsoft.com/auth/callback"
        auth_url = msal_app.get_authorization_request_url(
            scopes=["user.read"],
            redirect_uri=redirect_uri
        )
        
        return jsonify({
            "status": "success",
            "msal_imported": True,
            "redirect_uri": redirect_uri,
            "auth_url": auth_url[:200] + "...",
            "auth_url_length": len(auth_url)
        })
    except Exception as e:
        import traceback
        return jsonify({
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500

@app.route('/debug/session')
def debug_session():
    """Debug endpoint to check session and cookie configuration"""
    import os
    session_dir = app.config.get('SESSION_FILE_DIR')
    
    # Check if session directory exists and is writable
    session_dir_exists = os.path.exists(session_dir) if session_dir else False
    session_dir_writable = False
    if session_dir_exists:
        try:
            test_file = os.path.join(session_dir, '.write_test_debug')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            session_dir_writable = True
        except:
            session_dir_writable = False
    
    # Count session files
    session_file_count = 0
    if session_dir_exists:
        try:
            session_file_count = len([f for f in os.listdir(session_dir) if not f.startswith('.')])
        except:
            pass
    
    return jsonify({
        'session_config': {
            'SESSION_TYPE': app.config.get('SESSION_TYPE'),
            'SESSION_PERMANENT': app.config.get('SESSION_PERMANENT'),
            'SESSION_COOKIE_SECURE': app.config.get('SESSION_COOKIE_SECURE'),
            'SESSION_COOKIE_HTTPONLY': app.config.get('SESSION_COOKIE_HTTPONLY'),
            'SESSION_COOKIE_SAMESITE': app.config.get('SESSION_COOKIE_SAMESITE'),
            'SESSION_COOKIE_DOMAIN': app.config.get('SESSION_COOKIE_DOMAIN'),
            'SESSION_FILE_DIR': session_dir,
            'PERMANENT_SESSION_LIFETIME': str(app.config.get('PERMANENT_SESSION_LIFETIME')),
        },
        'session_storage': {
            'directory_exists': session_dir_exists,
            'directory_writable': session_dir_writable,
            'session_file_count': session_file_count,
        },
        'current_session': {
            'authenticated': flask_session.get('authenticated', False),
            'has_user': 'user' in flask_session,
            'session_cookie_present': 'session' in request.cookies,
            'session_cookie_value': request.cookies.get('session', 'NOT_SET')[:20] + '...' if request.cookies.get('session') else 'NOT_SET',
        },
        'environment': {
            'FLASK_ENV': os.getenv('FLASK_ENV'),
            'PREFERRED_URL_SCHEME': os.getenv('PREFERRED_URL_SCHEME'),
            'BEHIND_PROXY': os.getenv('BEHIND_PROXY'),
            'FRONTEND_DOMAIN': os.getenv('FRONTEND_DOMAIN'),
        }
    })

@app.route('/login_page')
def login_page():
    """Show the login page"""
    return render_template('login.html')

@app.route('/login')
def login():
    """Initiate MSAL authentication flow"""
    try:
        # Build redirect URI - use HTTP for development, HTTPS for production
        is_development = os.getenv('FLASK_ENV', 'production') == 'development'
        scheme = 'http' if is_development else 'https'
        
        redirect_uri = url_for('auth_callback', _external=True, _scheme=scheme)
        
        logger.info(f"🔍 Building auth URL with redirect_uri: {redirect_uri}")
        
        # Get authorization URL
        auth_url = auth_manager.get_auth_url(redirect_uri)
        
        logger.info(f"🔍 Generated auth_url: {auth_url[:100]}...")
        logger.info(f"🔍 Redirecting to Microsoft login")
        return redirect(auth_url)
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"❌ Login error: {e}\n{error_trace}")
        return redirect(url_for('login_page', error=f'Authentication setup failed: {str(e)}'))

@app.route('/auth/callback')
def auth_callback():
    """Handle OAuth callback from Microsoft"""
    try:
        # Get authorization code from query parameters
        auth_code = request.args.get('code')
        
        if not auth_code:
            error_desc = request.args.get('error_description', 'No authorization code received')
            logger.error(f"❌ Auth callback error: {error_desc}")
            return redirect(url_for('login_page', error=error_desc))
        
        # Build redirect URI (must match the one used in login)
        # Use HTTP for development, HTTPS for production
        is_development = os.getenv('FLASK_ENV', 'production') == 'development'
        scheme = 'http' if is_development else 'https'
        
        redirect_uri = url_for('auth_callback', _external=True, _scheme=scheme)
        
        logger.info(f"🔄 Processing auth callback with redirect_uri: {redirect_uri}")
        
        # Acquire token using auth code
        token_response = auth_manager.acquire_token_by_auth_code(auth_code, redirect_uri)
        
        if not token_response:
            logger.error("❌ Failed to acquire token")
            return redirect(url_for('login_page', error='Failed to acquire authentication token'))
        
        # Store user session
        auth_manager.store_user_session(token_response)
        
        user_info = auth_manager.get_user_info()
        logger.info(f"✅ User authenticated: {user_info['username']}")
        
        # Debug session state
        logger.info(f"🔍 Session after auth: authenticated={flask_session.get('authenticated')}, user={flask_session.get('user')}")
        logger.info(f"🔍 Session ID: {request.cookies.get('session')}")
        
        # Redirect to main application
        return redirect(url_for('index'))
        
    except Exception as e:
        logger.error(f"❌ Auth callback error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return redirect(url_for('login_page', error='Authentication failed'))

@app.route('/logout')
def logout():
    """Logout and clear session"""
    user_info = auth_manager.get_user_info()
    username = user_info['username'] if user_info else 'Unknown'
    
    auth_manager.clear_session()
    logger.info(f"✅ User logged out: {username}")
    
    return redirect(url_for('login_page'))

if __name__ == '__main__':
    # Setup session-aware logging now that workflow_updates_queue is available
    setup_session_aware_logging()
    
    # Simplified configuration for Application Gateway backend
    # Application Gateway will handle SSL termination, so we run HTTP only
    port = int(os.getenv('PORT', 80))  # Standard HTTP port for backend service
    host = os.getenv('HOST', '0.0.0.0')
    
    print("� Starting AI Compliance Tool (Beta) - HTTP Backend for Application Gateway")
    print(f"� Running on {host}:{port}")
    print("⚠️ SSL termination will be handled by Azure Application Gateway")
    print("📁 Log output will appear below...")
    
    # Run HTTP only - Application Gateway handles HTTPS
    app.run(debug=False, host=host, port=port, use_reloader=False)
