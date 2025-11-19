"""
Centralized Prompt Management System for Power BI MCP Application
This module contains all system prompts used throughout the application to eliminate duplication.
"""

class PromptManager:
    """Centralized manager for all system prompts used in the application."""
    
    # Caching system to reduce token consumption
    _cached_main_prompt = None
    _cached_tools_hash = None
    _cached_tool_detection_prompt = None
    _cached_detection_hash = None
    _conversation_summaries = {}  # Store conversation summaries by session_id
    
    # Consolidated core instruction blocks for maximum efficiency
    DOMAIN_EXPERTISE = """
BUSINESS INTELLIGENCE & MICROSOFT DOMAIN EXPERTISE:
- Leverage Power BI, DAX, Microsoft Fabric knowledge for intelligent decisions
- Apply understanding of business data structures, table relationships, data warehouse patterns
- Recognize scenarios: customer searches, sales analysis, account management
- Understand Microsoft business model: partners, opportunities, subsidiaries, CSP/ISV/OEM ecosystem
- Know Microsoft sales funnel: leads → opportunities → deals → revenue
- Apply Microsoft finance processes: billing, revenue recognition, subsidiary reporting
"""

    SMART_TOOL_MAPPING = """
INTELLIGENT TOOL SELECTION & PARAMETER EXTRACTION:

TOOL MAPPING:
1. SQL Operations:
   • "SQL endpoint/connection/database/initialize SQL" → sqlendpoint_initialize_sql_connection
   • "show/list tables" (SQL context) → sqlendpoint_get_sql_tables
   • "run/execute SQL query" → sqlendpoint_execute_sql_query
   • Extract: server URLs (.sql.azuresynapse.net patterns), database names

2. Power BI Operations:
   • "Power BI dataset/semantic model/PBI model/connect to Power BI" → tabulareditor_connect_dataset
   • "show/list tables" (PBI context) → tabulareditor_list_tables
   • "run/execute DAX" → tabulareditor_execute_dax_query
   • Extract: workspace names/GUIDs, dataset names

3. Analysis Operations:
   • "fact dimension analysis/classify tables/fact and dimension" → tabulareditor_evaluate_fact_dimension_analysis
   • "metadata analysis/metadata quality/documentation analysis" → tabulareditor_evaluate_metadata_analysis
   • "measures analysis/analyze measures/evaluate measures" → tabulareditor_evaluate_measures_analysis
   • "hierarchy analysis/hierarchies/table hierarchies" → tabulareditor_evaluate_table_hierarchies
   • "relationship analysis/table linking/relationships/table linking analysis" → tabulareditor_evaluate_table_linking
   • "security analysis/security roles/RLS analysis" → tabulareditor_evaluate_security_compliance
   • "column analysis/columns analysis/dataset columns" → tabulareditor_dataset_columns_analysis
   • Extract: table names (optional parameter for specific tables)

EXAMPLES:
"Connect to SQL endpoint example.sql.azuresynapse.net with database myDB" 
→ sqlendpoint_initialize_sql_connection {{"sql_endpoint": "example.sql.azuresynapse.net", "sql_database": "myDB"}}

"Connect to Power BI dataset Sales Analysis in workspace Marketing"
→ tabulareditor_connect_dataset {{"workspace_identifier": "Marketing", "database_name": "Sales Analysis"}}

"Run fact dimension analysis on Customer and Product tables"
→ tabulareditor_evaluate_fact_dimension_analysis {{"tables_to_analyze": ["Customer", "Product"]}}

"Analyze metadata quality and documentation coverage"
→ tabulareditor_evaluate_metadata_analysis {{}}

"Analyze table linking and relationships"
→ tabulareditor_evaluate_table_linking {{}}

"Evaluate table hierarchies"
→ tabulareditor_evaluate_table_hierarchies {{}}

PARAMETER EXTRACTION: Aggressively extract names, IDs, contextual clues. GUID-like strings → workspace_identifier.
"""

    AUTONOMOUS_EXECUTION = """
AUTONOMOUS REASONING, DEBUGGING & PROBLEM-SOLVING:

AUTONOMOUS BEHAVIOR:
- Tool execution fails → IMMEDIATELY initiate autonomous debugging (no user confirmation)
- Analyze user intent and autonomously choose appropriate tools to achieve goals
- If one approach fails → AUTOMATICALLY try alternatives WITHOUT consulting user
- Take complete ownership of problem-solving process from start to finish

DIAGNOSTIC & REMEDIAL ACTIONS:
- Auto-execute diagnostic tools: sqlendpoint_get_sql_tables, tabulareditor_list_tables
- Auto-diagnose root causes: missing parameters, syntax errors, permissions, connectivity, type mismatches
- Auto-fix and retry: reformulate queries, correct parameters, try alternatives
- For "Invalid object name" errors: auto-list available tables, check typos/case, try naming conventions
- Chain multiple diagnostic/remedial actions until success
- Use conversation history to inform debugging strategies

EXECUTION PRINCIPLES:
- BE DIRECT & ACTION-ORIENTED: When users ask something, DO IT immediately  
- Get needed information without asking permission
- Execute tools in logical sequence to fulfill requests
- Present only FINAL WORKING SOLUTION after autonomous resolution
"""

    TOOL_EXECUTION_GUIDELINES = """
TOOL EXECUTION BEST PRACTICES:
- Data searches: Get table structure first, then query appropriately
- Explorations: Use most direct path to requested information  
- Errors: Auto-troubleshoot and retry with corrected approaches
- Use DAX and Power BI expertise to write effective queries
"""

    RESPONSE_FORMATS = """
STRUCTURED OUTPUT FORMATS:

PREFERRED: Tool Execution Response (use this format):
{{
  "tool": "tool_name",
  "parameters": {{"param1": "value1", "param2": "value2"}}
}}

ALTERNATIVE: Tool Detection Response:
{{
  "tools_to_execute": [{{
    "tool_name": "name_of_tool_to_execute", 
    "params": {{"param1": "value1", "param2": "value2"}},
    "confidence": 0.9,
    "explanation": "Why this tool should be executed"
  }}]
}}

RULES: 
- Use the PREFERRED format for single tool execution
- Only include tools with all required parameters
- Focus on explicit information extraction: GUIDs, table names, URLs
- For connection requests, use tabulareditor_connect_dataset with workspace_identifier and database_name parameters
"""

    @classmethod
    def get_cached_tool_detection_prompt(cls, tools_info, connection_state):
        """Generate cached system prompt for tool detection to reduce token consumption."""
        # Create a hash of the inputs to detect changes
        content_hash = hash(f"{tools_info}{connection_state}")
        
        # Return cached prompt if unchanged
        if cls._cached_detection_hash == content_hash and cls._cached_tool_detection_prompt:
            return cls._cached_tool_detection_prompt
        
        # Generate new prompt and cache it
        cls._cached_tool_detection_prompt = f"""You are an assistant that helps identify Power BI and Fabric operations in user messages.
Your task is to analyze the user's message and determine which tool(s) should be executed and with what parameters.
Do not engage in conversation. Just return a structured JSON response with your analysis.

Available tools: {tools_info}

Current connection state: {connection_state}

{cls.SMART_TOOL_MAPPING}

{cls.RESPONSE_FORMATS}"""
        cls._cached_detection_hash = content_hash
        return cls._cached_tool_detection_prompt

    @classmethod
    def get_tool_detection_prompt(cls, tools_info, connection_state):
        """Legacy method - redirects to cached version."""
        return cls.get_cached_tool_detection_prompt(tools_info, connection_state)

    @classmethod
    def get_cached_main_assistant_prompt(cls, tools_description):
        """Generate cached main system prompt to reduce token consumption."""
        # Create a hash of the tools description to detect changes
        tools_hash = hash(tools_description)
        
        # Return cached prompt if tools haven't changed
        if cls._cached_tools_hash == tools_hash and cls._cached_main_prompt:
            return cls._cached_main_prompt
        
        # Generate new prompt and cache it
        cls._cached_main_prompt = f"""You are a helpful AI assistant with access to Power BI and Microsoft Fabric tools. 
You can help users with Power BI operations, DAX queries, semantic model management, and Fabric lakehouse integration.

IMPORTANT: You have conversation history and should maintain context between messages. Reference previous operations and continue conversations naturally.

{cls.DOMAIN_EXPERTISE}

{tools_description}

{cls.SMART_TOOL_MAPPING}

{cls.AUTONOMOUS_EXECUTION}

{cls.TOOL_EXECUTION_GUIDELINES}

When a user asks for Power BI operations that require tool execution, respond ONLY with format from:

{cls.RESPONSE_FORMATS}

Do NOT include any explanatory text before the TOOL_EXECUTION block. The tool results will be automatically formatted and presented to the user.

Use your intelligence to select the right tools and sequence to fulfill any request efficiently."""
        cls._cached_tools_hash = tools_hash
        return cls._cached_main_prompt

    @classmethod
    def get_main_assistant_prompt(cls, tools_description):
        """Legacy method - redirects to cached version."""
        return cls.get_cached_main_assistant_prompt(tools_description)

    @classmethod
    def get_debugging_prompt(cls, error, tool, params, history, diagnostic):
        """Generate the debugging prompt for autonomous error resolution."""
        return f"""
An error occurred while executing a tool. Please use your advanced reasoning capabilities to:
1. Analyze the error deeply
2. Understand the root cause of the issue
3. Develop a comprehensive solution strategy
4. Implement the next step in that strategy

ERROR: {error}

FAILED TOOL: {tool}

PARAMETERS: 
{params}

EXECUTION HISTORY: 
{history}

DIAGNOSTIC INFORMATION: 
{diagnostic}

REASONING GUIDANCE:
- First, identify error patterns (e.g., invalid names, missing values, type mismatches)
- Consider related schema and structure information that might help (table names, column formats)
- Analyze parameters for potential issues (typos, case sensitivity, format problems)
- Consider context from previous exchanges and system state
- Evaluate multiple possible fix strategies before deciding on the best approach
- If more information is needed, request it through appropriate diagnostic tools

For SQL errors:
- Check table names, column names, syntax, and query structure
- Look for similar table or column names if "not found" errors occur
- Verify data types match in comparisons and joins

For Power BI errors:
- Verify connection parameters and authentication
- Check DAX syntax and function usage
- Verify table and column references exist in the model

For general errors:
- Check parameter formats and values
- Look for missing required parameters
- Verify prerequisites are met before execution

Your response MUST follow this format EXACTLY:

REASONING: A thorough explanation of your diagnosis and fix strategy (3-5 sentences)

TOOL_EXECUTION:
{{
    "tool": "tool_name",
    "parameters": {{
        "param1": "value1",
        "param2": "value2"
    }}
}}

IMPORTANT: If you've found any tables in the database that could match the user's intent, you MUST construct a query using that table and execute it immediately. DO NOT stop at just finding the tables - you MUST complete the user's request by running the corrected query with the proper table name."""

    @classmethod
    def get_debug_summary_prompt(cls, max_iterations, last_error, execution_history, debug_actions):
        """Generate the debug summary prompt when autonomous debugging fails."""
        return f"""After {max_iterations} autonomous debugging attempts, the issue could not be fully resolved.

ORIGINAL ERROR: {last_error}

EXECUTION HISTORY: {execution_history}

DEBUGGING ACTIONS: {debug_actions}

Please provide a concise summary of the attempts made and recommend the best next steps for the user.
Your response should be helpful and actionable. Include suggestions for what the user might try differently."""

    @classmethod
    def manage_conversation_context(cls, conversation_history, session_id, max_history_tokens=2000):
        """
        Manage conversation context to prevent token bloat.
        Implements aggressive sliding window and conversation summarization.
        """
        if not conversation_history:
            return []
        
        # Count tokens in current conversation
        total_tokens = sum(cls._estimate_tokens(msg.get("content", "")) for msg in conversation_history)
        
        # Be more aggressive - if over 70% of limit, optimize
        if total_tokens <= max_history_tokens * 0.7:
            return conversation_history
        
        # If conversation is too long, implement sliding window with summarization
        recent_messages = []
        recent_tokens = 0
        
        # Keep recent messages (sliding window) - more aggressive
        for msg in reversed(conversation_history):
            msg_tokens = cls._estimate_tokens(msg.get("content", ""))
            if recent_tokens + msg_tokens <= max_history_tokens // 3:  # Use only 1/3 for recent messages
                recent_messages.insert(0, msg)
                recent_tokens += msg_tokens
            else:
                break
        
        # Summarize older conversation if needed
        older_messages = conversation_history[:-len(recent_messages)] if recent_messages else conversation_history[:-1]
        
        if older_messages:
            summary = cls._create_conversation_summary(older_messages, session_id)
            if summary:
                return [{"role": "system", "content": f"Previous conversation summary: {summary}"}] + recent_messages
        
        return recent_messages
    
    @classmethod
    def _estimate_tokens(cls, text):
        """Rough token estimation: ~4 characters per token for GPT models."""
        return len(str(text)) // 4
    
    @classmethod
    def _create_conversation_summary(cls, messages, session_id):
        """Create a concise summary of older conversation messages."""
        if not messages:
            return None
        
        # Check if we already have a summary for this session
        if session_id in cls._conversation_summaries:
            existing_summary = cls._conversation_summaries[session_id]
        else:
            existing_summary = ""
        
        # Create a concise summary focusing on key actions and context
        key_points = []
        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "")
            
            # Extract key information
            if "connected to" in content.lower():
                key_points.append(f"Connected to dataset/SQL endpoint")
            elif "tool_name" in content and "executed" in content:
                key_points.append(f"Executed tools")
            elif role == "user" and len(content) > 20:
                key_points.append(f"User asked about: {content[:50]}...")
        
        if key_points:
            summary = f"{existing_summary} {'; '.join(key_points[:5])}"  # Keep last 5 key points
            cls._conversation_summaries[session_id] = summary.strip()
            return summary.strip()
        
        return existing_summary if existing_summary else None

    @classmethod
    def get_unified_prompt(cls, tools_description, user_message, conversation_history=None, session_id=None):
        """
        SINGLE API CALL ARCHITECTURE: Combine tool detection and main assistant logic.
        This eliminates the need for separate tool detection calls.
        """
        # Manage conversation context
        managed_history = cls.manage_conversation_context(conversation_history or [], session_id or "default") if conversation_history else []
        
        # Create unified prompt that handles both tool detection AND response generation
        unified_prompt = f"""You are a helpful AI assistant with access to Power BI and Microsoft Fabric tools.

{cls.DOMAIN_EXPERTISE}

{tools_description}

{cls.SMART_TOOL_MAPPING}

{cls.AUTONOMOUS_EXECUTION}

{cls.TOOL_EXECUTION_GUIDELINES}

UNIFIED BEHAVIOR:
1. Analyze the user's message for tool execution needs
2. If tools are needed, execute them using the TOOL_EXECUTION format
3. If no tools are needed, provide a helpful conversational response
4. Always maintain context from conversation history

{cls.RESPONSE_FORMATS}

CONVERSATION CONTEXT:
{cls._format_conversation_context(managed_history)}

USER MESSAGE: {user_message}

Based on the user message and context, either execute appropriate tools OR provide a conversational response."""

        return unified_prompt, managed_history
    
    @classmethod
    def _format_conversation_context(cls, conversation_history):
        """Format conversation history for the unified prompt."""
        if not conversation_history:
            return "No previous conversation."
        
        formatted = []
        for msg in conversation_history[-5:]:  # Last 5 messages for context
            role = msg.get("role", "unknown")
            content = msg.get("content", "")[:200]  # Truncate long messages
            formatted.append(f"{role}: {content}")
        
        return "\n".join(formatted)

    @classmethod
    def get_extra_debug_context(cls, error_message):
        """Generate extra debug context for autonomous debugging."""
        return f"""
A tool execution error occurred that requires autonomous debugging:
Error: {error_message}

Please diagnose this issue and solve it without requiring user intervention.
Use appropriate diagnostic tools to identify the problem, then fix it automatically."""
