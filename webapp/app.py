import os
import sys
import json
import logging
import inspect
import datetime
import tiktoken
from typing import Dict, Any, List, Optional, Union, Callable
import asyncio
from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from azure.identity import AzureCliCredential
from openai import AzureOpenAI
from dotenv import load_dotenv
import pandas as pd

# Add parent directory to path to import from src
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from src.server import SQLEndpoint, Fabric, TabularEditor, CopilotDataEvaluator, AuthenticationManager
except ImportError as e:
    print(f"Error importing classes from src.server: {e}")

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables from root folder
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

# PowerBI MCP Server wrapper
class PowerBIMCPServer:
    """Wrapper for the MCP Server specific to Power BI operations."""
    def __init__(self):
        try:
            logger.info("Initializing PowerBIMCPServer...")
            from mcp.server import Server
            from mcp.server.models import InitializationOptions
            
            # Configure the server
            options = InitializationOptions(
                tool_namespace="mcp",
                notification_options=None,
                server_name="Power BI MCP Server",  # Added required fields
                server_version="1.0.0",
                capabilities=[]
            )
            
            # Create the server instance
            self.server = Server(options)
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

# Initialize Azure OpenAI client using AzureCliCredential
from azure.identity import AzureCliCredential
from openai import AzureOpenAI
from webapp.prompt_manager import PromptManager

# Get Azure OpenAI credentials from environment variables
azure_endpoint = os.getenv("PROJECT_ENDPOINT")
azure_deployment = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o")
api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

# Initialize Azure OpenAI client with manual token acquisition
try:
    # Get the token manually from AzureCliCredential with the correct scope
    azure_credential = AzureCliCredential()
    # Get token for Azure OpenAI scope - using the correct audience for Azure AI services
    token = azure_credential.get_token("https://ai.azure.com/.default")
    
    # Use the token directly
    client = AzureOpenAI(
        azure_endpoint=azure_endpoint,
        azure_ad_token=token.token,
        api_version=api_version
    )
    logger.info(f"Successfully initialized Azure OpenAI client with Azure CLI token for endpoint {azure_endpoint}")
    
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
    raise RuntimeError(f"Failed to initialize Azure OpenAI client: {e}")

class MCPToolManager:
    """Manager for MCP tools that dynamically loads methods from different classes."""
    
    def __init__(self):
        """Initialize the tool manager with all necessary components."""
        self.available_tools = {}
        self.auth_manager = None
        self.sql_endpoint = None
        self.fabric = None
        self.tabular_editor = None
        self.copilot_evaluator = None
        self.mcp_server = None
        
        # Track connection state
        self.connected_dataset = None
        self.connected_workspace = None
        
        try:
            # Initialize core components
            self.auth_manager = AuthenticationManager()
            self.sql_endpoint = SQLEndpoint()
            self.fabric = Fabric()
            self.tabular_editor = TabularEditor()
            
            # Initialize evaluator after tabular_editor
            if self.tabular_editor and self.sql_endpoint:
                self.copilot_evaluator = CopilotDataEvaluator(self.tabular_editor, self.sql_endpoint)
            
            try:
                # Initialize MCP server connection
                from mcp.server import Server
                from mcp.types import ServerResult
                self.mcp_server = PowerBIMCPServer()
                logger.info("Successfully initialized MCP server connection")
            except Exception as mcp_err:
                logger.warning(f"Failed to initialize MCP server: {mcp_err}")
                self.mcp_server = None
            
            # Load all available tools
            self._initialize_tools()
            logger.info(f"MCP Tool Manager initialized with {len(self.available_tools)} tools")
            
        except Exception as e:
            logger.error(f"Error initializing MCP Tool Manager: {e}")
    
    def _initialize_tools(self) -> Dict[str, Dict]:
        """Initialize available MCP tools by fetching them dynamically from the MCP server."""
        try:
            # Get tools directly from TabularEditor server
            tools_from_server = self._fetch_tools_from_mcp_server()
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
            
            # Load tools from SQLEndpoint
            if self.sql_endpoint:
                self._extract_class_methods(
                    self.sql_endpoint, 
                    "SQLEndpoint", 
                    ["initialize_sql_connection", "get_sql_tables", "execute_sql_query", "get_sql_table_schema"]
                )
            
            # Load tools from Fabric
            if self.fabric:
                self._extract_class_methods(
                    self.fabric,
                    "Fabric",
                    ["get_workspace_info", "get_lakehouse_info", "refresh_sql_endpoint"]
                )
            
            # Load tools from TabularEditor
            if self.tabular_editor:
                self._extract_class_methods(
                    self.tabular_editor,
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
            if self.copilot_evaluator:
                self._extract_class_methods(
                    self.copilot_evaluator,
                    "CopilotDataEvaluator",
                    [
                        "dataset_columns_analysis", "evaluate_security_roles",
                        "evaluate_fact_and_dimension_analysis", "evaluate_table_linking"
                    ]
                )
            
            logger.info(f"Successfully loaded {len(self.available_tools)} tools")
            return self.available_tools
            
        except Exception as e:
            logger.error(f"Error initializing tools from classes: {e}")
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
                    self.available_tools[tool_name] = {
                        "description": docstring,
                        "method": method,
                        "parameters": parameters,
                        "required_params": required_params,
                        "optional_params": optional_params,
                        "source_class": class_name,
                        "function": self._create_wrapper_function(obj, method)
                    }
                    
                except Exception as e:
                    logger.warning(f"Error extracting method {method_name} from {class_name}: {e}")
    
    def _create_wrapper_function(self, obj: object, method: Callable) -> Callable:
        """Create a wrapper function for a method that handles arguments properly."""
        def wrapper(**kwargs):
            try:
                return method(**kwargs)
            except Exception as e:
                logger.error(f"Error executing {method.__name__}: {e}")
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
    
    def execute_tool(self, tool_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a specific MCP tool with parameters through the PowerBIMCPServer."""
        try:
            logger.info(f"Executing tool: {tool_name} with parameters: {parameters}")
            
            # Special case for connect_dataset: check if already connected and disconnect first
            if tool_name == "tabulareditor_connect_dataset" and self.connected_dataset:
                logger.info("Already connected to a dataset, disconnecting first...")
                try:
                    # Try to disconnect first using the tabular_editor directly
                    if hasattr(self.tabular_editor, 'disconnect_dataset'):
                        self.tabular_editor.disconnect_dataset()
                        logger.info("Successfully disconnected from previous dataset")
                except Exception as disconnect_error:
                    logger.warning(f"Error during disconnect: {str(disconnect_error)}")
                
                # Reset connection state
                self.connected_workspace = None
                self.connected_dataset = None
            
            if tool_name not in self.available_tools:
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' not found. Available tools: {list(self.available_tools.keys())}"
                }
            
            # Check if we have the MCP server available and try that first
            if self.mcp_server and hasattr(self.mcp_server, 'server') and hasattr(self.mcp_server.server, 'request_handlers'):
                try:
                    # Use the MCP server's call_tool handler
                    from mcp.types import CallToolRequest
                    
                    if CallToolRequest in self.mcp_server.server.request_handlers:
                        handler = self.mcp_server.server.request_handlers[CallToolRequest]
                        
                        # Create a CallToolRequest object
                        import asyncio
                        
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            # Create a proper request object with required fields
                            request = CallToolRequest(
                                method="tools/call",
                                params={
                                    "name": tool_name,
                                    "arguments": parameters
                                }
                            )
                            
                            # Call the handler
                            result = loop.run_until_complete(handler(request))
                            
                            # Extract the actual result
                            actual_result = result.root if hasattr(result, 'root') else result
                            
                            # Extract text from TextContent objects
                            if hasattr(actual_result, 'content') and actual_result.content:
                                result_text = actual_result.content[0].text
                                return {"success": True, "result": result_text}
                        finally:
                            loop.close()
                except Exception as mcp_error:
                    logger.warning(f"MCP server execution failed, falling back to direct method call: {mcp_error}")
                    # Continue to fallback method
            
            # Fallback to direct method call
            tool_info = self.available_tools[tool_name]
            
            # Check for required parameters
            missing_params = [param for param in tool_info.get('required_params', []) 
                             if param not in parameters]
            
            if missing_params:
                return {
                    "success": False,
                    "error": f"Missing required parameters: {', '.join(missing_params)}"
                }
            
            # If the tool has a function property (from class method extraction), use that
            if 'function' in tool_info:
                result = tool_info['function'](**parameters)
                
                # Update connection state if this was a connect_dataset call
                if tool_name == "tabulareditor_connect_dataset" and isinstance(result, bool) and result:
                    self.connected_dataset = parameters.get("database_name")
                    self.connected_workspace = parameters.get("workspace_identifier")
                    logger.info(f"Updated connection state: workspace={self.connected_workspace}, dataset={self.connected_dataset}")
                
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
    
    def auto_detect_and_execute_tools(self, user_message: str) -> List[Dict[str, Any]]:
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
            
            # Current connection state
            connection_state = {
                "connected_workspace": self.connected_workspace,
                "connected_dataset": self.connected_dataset
            }
            
            # Create the prompt for GPT-4o to analyze the user message using PromptManager
            tool_detection_prompt = PromptManager.get_tool_detection_prompt().format(
                tools_info=json.dumps(tools_info, indent=2),
                connection_state=json.dumps(connection_state, indent=2)
            )
            
            messages = [
                PromptManager.create_system_message(tool_detection_prompt),
                PromptManager.create_user_message(user_message)
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
            
            # Execute the tools based on the analysis
            if "tools_to_execute" in tool_analysis and tool_analysis["tools_to_execute"]:
                for tool_execution in tool_analysis["tools_to_execute"]:
                    tool_name = tool_execution.get("tool_name")
                    params = tool_execution.get("params", {})
                    confidence = tool_execution.get("confidence", 0)
                    explanation = tool_execution.get("explanation", "")
                    
                    # Only execute if we have sufficient confidence (0.6 or higher)
                    if tool_name in self.available_tools and confidence >= 0.6:
                        logger.info(f"Auto-executing tool: {tool_name} with params: {params}")
                        logger.info(f"Reason: {explanation} (confidence: {confidence})")
                        
                        # Execute the tool
                        execution_result = self.execute_tool(tool_name, params)
                        
                        # Update connection state if this was a successful connect_dataset call
                        if tool_name == "tabulareditor_connect_dataset" and execution_result.get("success", False):
                            self.connected_workspace = params.get("workspace_identifier")
                            self.connected_dataset = params.get("database_name")
                        
                        # Add to results
                        results.append({
                            "tool_name": tool_name,
                            "params": params,
                            "confidence": confidence,
                            "explanation": explanation,
                            "result": execution_result
                        })
            
            return results
            
        except Exception as e:
            logger.error(f"Error in GPT-based tool detection: {e}")
            return []

# Initialize the MCP Tool Manager
tool_manager = MCPToolManager()

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
        
        # Use PromptManager to get the system prompt with tools description
        system_message = PromptManager.get_system_prompt(tool_manager.get_tools_description())
        
        # Build messages with conversation history using PromptManager
        messages = [PromptManager.create_system_message(system_message)]
        
        # Add conversation history
        for msg in conversation_history:
            if msg.get("role") in ["user", "assistant"]:
                messages.append(PromptManager.create_message(msg["role"], msg["content"]))
                # Count tokens in conversation history
                input_tokens += count_tokens(msg["content"])
        
        # Add the current user message
        messages.append(PromptManager.create_user_message(message))
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
                    
                    # Create an enhanced debug prompt that leverages GPT-4o's general reasoning capabilities
                    # Use PromptManager for the debug prompt
                    debug_prompt = PromptManager.get_debug_prompt().format(
                        error=last_error,
                        tool=tool_name,
                        params=json.dumps(parameters, indent=2),
                        history=json.dumps(execution_history, indent=2),
                        diagnostic=json.dumps(debug_context_info, indent=2)
                    )
                    
                    # Prepare messages for debug request using PromptManager
                    debug_messages = [
                        PromptManager.create_system_message(system_message),
                        PromptManager.create_user_message(message),  # Original user message
                        PromptManager.create_assistant_message(response),  # Original response with TOOL_EXECUTION
                        PromptManager.create_system_message(f"Tool execution failed with error: {last_error}"),
                        PromptManager.create_user_message(debug_prompt)
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
            
            # Prepare a final message about the debugging attempts
            debug_summary_prompt = "After " + str(max_tool_iterations) + " autonomous debugging attempts, the issue could not be fully resolved.\n\n"
            debug_summary_prompt += "ORIGINAL ERROR: " + last_error + "\n\n"
            debug_summary_prompt += "EXECUTION HISTORY: " + json.dumps(execution_history, indent=2) + "\n\n"
            debug_summary_prompt += "DEBUGGING ACTIONS: " + json.dumps(debug_actions_taken, indent=2) + "\n\n"
            debug_summary_prompt += "Please provide a concise summary of the attempts made and recommend the best next steps for the user.\n"
            debug_summary_prompt += "Your response should be helpful and actionable. Include suggestions for what the user might try differently."
            
            # Count tokens for the summary prompt
            summary_prompt_tokens = count_tokens(debug_summary_prompt)
            tool_input_tokens += summary_prompt_tokens
            
            # Create messages for the summary request using PromptManager
            summary_messages = [
                PromptManager.create_system_message(system_message),
                PromptManager.create_user_message(message),  # Original user message
                PromptManager.create_system_message(debug_summary_prompt)
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

# Convert the tool manager's tools to the format expected by the app
for name, info in tool_manager.available_tools.items():
    MCP_TOOLS[name] = {
        "description": info["description"],
        "required_params": info["required_params"],
        "optional_params": info["optional_params"],
        "function": info.get("function")
    }

@app.route('/')
def index():
    """Render the chat interface"""
    return render_template('index.html', tools=MCP_TOOLS)

# Global dictionary to store conversation history by session_id
conversation_sessions = {}

# Global dictionary to store token usage by session_id
token_usage = {}

@app.route('/api/chat', methods=['POST', 'GET'])
def chat():
    """API endpoint for chat"""
    if request.method == 'GET':
        return jsonify({
            'message': 'This is the chat API endpoint. Please use POST method with a JSON body containing a "message" field.',
            'example': {
                'message': 'Your question or command here',
                'tool_name': '(Optional) Specific tool to execute',
                'tool_params': {},
                'session_id': '(Optional) Session identifier for conversation continuity'
            }
        })
    
    try:
        data = request.json
        user_message = data.get('message', '')
        
        if not user_message:
            return jsonify({'error': 'No message provided'}), 400
        
        # Get or create session_id
        session_id = data.get('session_id', 'default_session')
        logger.info(f"Processing message for session: {session_id}")
        logger.info(f"User message: {user_message}")
        
        # Create a progress tracker
        progress_updates = []
        def track_progress(message):
            progress_updates.append(message)
            logger.info(f"Progress: {message}")
        
        # Initialize empty lists for tool results
        tool_results = []  # Store all tool results to return to the client
        executed_tool_names = []
        
        # Retrieve existing conversation history or create a new one
        if session_id not in conversation_sessions:
            conversation_sessions[session_id] = []
            logger.info(f"Created new conversation session: {session_id}")
        
        conversation_history = conversation_sessions[session_id]
        logger.info(f"Current conversation history length: {len(conversation_history)}")
        
        # Auto-detect and execute tools based on the user message
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
        
        # Flag to track autonomous debugging
        autonomous_debugging_occurred = False
        debug_logs = []
        
        # Call get_enhanced_chat_response to generate the response with autonomous debugging
        try:
            # Check if we have an error that needs autonomous debugging
            extra_context = None
            if 'error_message' in locals():
                logger.info(f"Passing error to autonomous debugging: {error_message}")
                extra_context = f"""
                A tool execution error occurred that requires autonomous debugging:
                Error: {error_message}
                
                Please diagnose this issue and solve it without requiring user intervention.
                Use appropriate diagnostic tools to identify the problem, then fix it automatically.
                """
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
            
            # Add the assistant's response to the conversation history
            conversation_history.append({
                "role": "assistant",
                "content": assistant_message
            })
            
            # Save the updated conversation history
            conversation_sessions[session_id] = conversation_history
            
            # Include token usage in the response
            session_token_usage = token_usage.get(session_id, {"input": 0, "output": 0})
            
        except Exception as e:
            logger.error(f"Azure OpenAI API error: {str(e)}")
            assistant_message = f"I couldn't generate a response using Azure OpenAI API. Error: {str(e)}"
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
def get_tools():
    """API endpoint to get the list of available tools"""
    tools_info = {}
    for name, info in tool_manager.available_tools.items():
        tools_info[name] = {
            "description": info["description"],
            "required_params": info["required_params"],
            "optional_params": info["optional_params"],
            "source_class": info["source_class"]
        }
    return jsonify(tools_info)

@app.route('/api/health', methods=['GET'])
def health_check():
    """API endpoint for health check and tool status"""
    status = {
        "status": "healthy",
        "tool_count": len(tool_manager.available_tools),
        "components": {
            "auth_manager": tool_manager.auth_manager is not None,
            "sql_endpoint": tool_manager.sql_endpoint is not None,
            "fabric": tool_manager.fabric is not None,
            "tabular_editor": tool_manager.tabular_editor is not None,
            "copilot_evaluator": tool_manager.copilot_evaluator is not None,
            "azure_openai": client is not None
        },
        "azure_openai": {
            "endpoint_configured": azure_endpoint is not None,
            "deployment": azure_deployment,
            "api_version": api_version
        },
        "tools_by_class": {}
    }
    
    # Count tools by class
    class_counts = {}
    for name, info in tool_manager.available_tools.items():
        class_name = info["source_class"]
        if class_name not in class_counts:
            class_counts[class_name] = 0
        class_counts[class_name] += 1
    
    status["tools_by_class"] = class_counts
    
    return jsonify(status)

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
    conversation_text = f"Power BI Assistant Chat - Session: {session_id}\n"
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

if __name__ == '__main__':
    print("Starting Power BI Assistant on http://localhost:5000")
    app.run(debug=False, port=5000)
