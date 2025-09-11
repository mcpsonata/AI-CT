# Standard library imports
import asyncio
import codecs
import csv
import inspect
import io
import json
import logging
import os
import re
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

# Third-party imports
import anyio
import clr
try:
    import nltk  # type: ignore
    from nltk.corpus import wordnet  # type: ignore
    from nltk.tokenize import word_tokenize  # type: ignore
    from nltk.tag import pos_tag  # type: ignore
    NLTK_AVAILABLE = True
except ImportError:
    NLTK_AVAILABLE = False
import pandas as pd
import pyodbc
import requests
import sqlalchemy
from azure.identity import DefaultAzureCredential, ClientSecretCredential
from dotenv import load_dotenv
from azure.keyvault.secrets import SecretClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stderr,
    encoding='utf-8',
    errors='replace'
)
logger = logging.getLogger(__name__)

# Visualization imports
try:
    import graphviz
    VISUALIZATION_AVAILABLE = True
    logger.info("Graphviz library available for ERD generation")
except ImportError:
    VISUALIZATION_AVAILABLE = False
    logger.warning("Graphviz library not available. Install graphviz for PNG diagram generation: pip install graphviz")


# MCP imports
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.elicitation import AcceptedElicitation, DeclinedElicitation, CancelledElicitation
from mcp.types import Tool, TextContent
import mcp.server.stdio
# Timing constants for Fabric operations
SHORTCUT_PROPAGATION_WAIT = 0  # seconds to wait after shortcut creation
# Configure stdout and stderr for UTF-8 encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stderr,
    encoding='utf-8',
    errors='replace'
)
logger = logging.getLogger(__name__)

# Load environment
load_dotenv()
dll_folder = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lib")
clr.AddReference(os.path.join(dll_folder, "Microsoft.AnalysisServices.Tabular.dll"))
clr.AddReference(os.path.join(dll_folder, "Microsoft.AnalysisServices.Core.dll"))
clr.AddReference(os.path.join(dll_folder, "Microsoft.AnalysisServices.AdomdClient.dll"))

from Microsoft.AnalysisServices.Tabular import Server as TabularServer, Table as TabularTable,ModelRoleMember, EntityPartitionSource, RefreshType, DataType, DataColumn, Partition as TabularPartition, ModeType, ModelRole, TablePermission, MetadataPermission,SingleColumnRelationship,ModelPermission, CrossFilteringBehavior,CalculatedPartitionSource,Measure, Annotation # type: ignore
from Microsoft.AnalysisServices.AdomdClient import AdomdSchemaGuid  # type: ignore
from Microsoft.AnalysisServices.AdomdClient import AdomdConnection, AdomdCommand  # type: ignore

class AuthenticationManager:
    """Centralized authentication manager for Azure tokens."""
    
    def __init__(self):
        self._access_token = None
        self._token_expiry = None
        self._lock = threading.Lock()
        self._client_secret = self.get_client_secret()
        self._credential = ClientSecretCredential(
            tenant_id=os.getenv("AZURE_TENANT_ID"),
            client_id=os.getenv("AZURE_CLIENT_ID"),
            client_secret=self._client_secret
        )
    
    def get_client_secret(self) -> str:
        """Get the client secret for Azure authentication."""
        with self._lock:
            keyVaultName = os.getenv("Key_Vault_Name")
            secretName = os.getenv("Secret_Name")
            KVUri = f"https://{keyVaultName}.vault.azure.net"
            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=KVUri, credential=credential)
            retrieved_secret = client.get_secret(secretName)
            self._client_secret = retrieved_secret.value
        return self._client_secret
    
    def get_access_token(self, force_refresh: bool = False) -> str:
        """Get a valid access token, refreshing if necessary."""
        with self._lock:
            if (force_refresh or 
                self._access_token is None or 
                self._token_expiry is None or 
                datetime.now() >= self._token_expiry):
                
                token_result = self._credential.get_token("https://analysis.windows.net/powerbi/api/.default")
                self._access_token = token_result.token
                self._token_expiry = datetime.now() + timedelta(minutes=60)
                logger.info("Access token refreshed successfully")
            
            return self._access_token

class SQLEndpoint:
    """Handles SQL endpoint connections and queries with shared authentication."""
    
    def __init__(self):
        self.auth_manager = AuthenticationManager()
        self.engine = None
        self.sql_endpoint = None
        self.sql_database = None
        self.driver = None
        self.access_token = None
        self._connection_lock = threading.Lock()
    
    def initialize_sql_connection(self, sql_endpoint: str, sql_database: str):
        """Initialize the SQL engine with authentication and drivers."""
        with self._connection_lock:
            if not sql_endpoint or not sql_database:
                raise ValueError("sql_endpoint and sql_database must be provided")

            self.sql_endpoint = sql_endpoint
            self.sql_database = sql_database

            # Get available SQL Server drivers
            drivers = [d for d in pyodbc.drivers() if 'SQL Server' in d]
            if not drivers:
                raise RuntimeError("No SQL Server ODBC drivers found. Please install ODBC Driver for SQL Server.")

            # Prefer newer drivers
            preferred_drivers = ['ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server']
            self.driver = next((d for d in preferred_drivers if d in drivers), drivers[0])
            logger.info(f"Using driver: {self.driver}")

            if not self.access_token:
               self.access_token = self.auth_manager.get_access_token(force_refresh=True)

            # Prepare access token
            token_bytes = self.access_token.encode("utf-16-le")
            token_struct = struct.pack(f'<I{len(token_bytes)}s', len(token_bytes), token_bytes)
            SQL_COPT_SS_ACCESS_TOKEN = 1256

            # Build connection string
            connection_string = (
                f"Driver={{{self.driver}}};"
                f"Server={self.sql_endpoint},1433;"
                f"Database={self.sql_database};"
                f"Encrypt=Yes;"
                f"TrustServerCertificate=No;"
            )

            self.engine = sqlalchemy.create_engine(
                "mssql+pyodbc://",
                creator=lambda: pyodbc.connect(
                    connection_string,
                    attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}
                )
            )
            return self.engine
    
    def get_sql_tables(self) -> pd.DataFrame:
        """Get SQL tables using the pre-authenticated SQL engine."""
        if not self.engine:
            raise Exception("Please provide SQL Endpoint Server and Database details.")
        
        df = pd.read_sql_query("SELECT name as table_name FROM sys.tables", self.engine)
        logger.info(f"Retrieved {len(df)} tables from SQL database")
        return df
    
    def execute_sql_query(self, query: str) -> pd.DataFrame:
        """Execute any SQL query using the pre-authenticated SQL engine."""
        if not self.engine:
            raise Exception("Please provide SQL Endpoint Server and Database details.")

        logger.info(f"Executing SQL query: {query[:100]}...")  # Log first 100 chars
        df = pd.read_sql_query(query, self.engine)
        logger.info(f"Query executed successfully, returned {len(df)} rows")
        return df
    
    def get_sql_table_schema(self, table_name: str) -> pd.DataFrame:
        """Get column information for a specific table."""
        query = f"""
        SELECT 
            COLUMN_NAME as column_name,
            DATA_TYPE as data_type
        FROM INFORMATION_SCHEMA.COLUMNS 
        WHERE TABLE_NAME = '{table_name}'
        ORDER BY ORDINAL_POSITION
        """
        return self.execute_sql_query(query)

class Fabric:
    """Handles Microsoft Fabric REST API operations with centralized authentication."""
    def __init__(self):
        self.auth_manager = AuthenticationManager()
        self.access_token = self.auth_manager.get_access_token()
    
    def get_workspace_info(self, workspace_identifier: str) -> Dict[str, Any]:
        if not self.access_token:
            self.access_token = self.auth_manager.get_access_token(force_refresh=True)
        info = None
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        # Try to fetch by ID first
        url_by_id = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_identifier}"
        response = requests.get(url_by_id, headers=headers)

        if response.status_code == 200:
            data = response.json()
            info = {
                "workspace_id": data.get("id"),
                "workspace_name": data.get("displayName")
            }

        # If not found by ID, try searching by name
        url_list = f"https://api.fabric.microsoft.com/v1/workspaces/"
        response = requests.get(url_list, headers=headers)

        if response.status_code == 200:
            workspaces = response.json().get("value", [])
            for ws in workspaces:
                if ws.get("displayName", "").lower() == workspace_identifier.lower():
                    info = {
                        "workspace_id": ws.get("id"),
                        "workspace_name": ws.get("displayName")
                    }

        if not info:
            raise ValueError(f"Workspace '{workspace_identifier}' not found by ID or name.")
        return info
    
    def get_lakehouse_info(self, workspace_identifier: str, lakehouse_identifier: str) -> Dict[str, Any]:
        if not self.access_token:
            self.access_token = self.auth_manager.get_access_token(force_refresh=True)
        info = None
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        workspace_info = self.get_workspace_info(workspace_identifier)
        workspace_id = workspace_info.get("workspace_id", None)
        workspace_name = workspace_info.get("workspace_name", None)

        if not workspace_id:
            raise ValueError(f"Workspace '{workspace_identifier}' not found.")
        url_by_id = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/lakehouses/{lakehouse_identifier}"
        response = requests.get(url_by_id, headers=headers)

        if response.status_code == 200:
            data = response.json()
            info = {
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "lakehouse_id": data.get("id"),
                "lakehouse_name": data.get("displayName"),
                "sql_endpoint": data.get("properties",{}).get("sqlEndpointProperties",{}).get("connectionString",""),
                "sql_database":data.get("properties",{}).get("sqlEndpointProperties",{}).get("id","")
            }
        
        url_list = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/lakehouses"
        response = requests.get(url_list, headers=headers)

        if response.status_code == 200:
            lakehouses = response.json().get("value", [])
            for lh in lakehouses:
                if lh.get("displayName", "").lower() == lakehouse_identifier.lower():
                    info = {
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                        "lakehouse_id": lh.get("id"),
                        "lakehouse_name": lh.get("displayName"),
                        "sql_endpoint": lh.get("properties",{}).get("sqlEndpointProperties",{}).get("connectionString",""),
                        "sql_database":lh.get("properties",{}).get("sqlEndpointProperties",{}).get("id","")
                    }

        if not info:
            raise ValueError(f"Lakehouse '{lakehouse_identifier}' not found by ID or name.")

        return info
        
    def refresh_sql_endpoint(self, workspace_identifier: str, lakehouse_identifier: str) -> Dict[str, Any]:
        """Refresh the SQL endpoint for a lakehouse using Fabric REST API"""
        try:
            if not self.access_token:
               self.access_token = self.auth_manager.get_access_token(force_refresh=True)

            sql_endpoint_id = self.get_lakehouse_info(workspace_identifier, lakehouse_identifier).get("sql_database", None)
            workspace_id = self.get_lakehouse_info(workspace_identifier, lakehouse_identifier).get("workspace_id", None)
            if not sql_endpoint_id:
                raise ValueError(f"SQL Endpoint for Lakehouse '{lakehouse_identifier}' not found.")

            endpoint = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/sqlEndpoints/{sql_endpoint_id}/refreshMetadata"

            body = {
                "sqlEndpointId": sql_endpoint_id,
                "workspaceId": workspace_id
            }

            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }

            response = requests.post(endpoint, headers=headers, json=body)
            return response.text
        
        except Exception as e:
            pass
            return f"{str(e)}"

class TabularEditor:
    def __init__(self):
        self.connection_string = None
        self.connected = False
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.model = None
        self.connection_lock = threading.Lock()
        self.tabularserver = TabularServer()
        self.fabric = Fabric()
        self.sql_metadata = SQLEndpoint()   

    def connect_dataset(self, workspace_identifier: str, database_name: str) -> bool:
        """Establish connection to Power BI dataset with automatic SQL endpoint discovery"""
        try: 
            workspace_info = self.fabric.get_workspace_info(workspace_identifier)
            workspace_name = workspace_info.get("workspace_name", None)
            workspace_id = workspace_info.get("workspace_id", None)
            if not workspace_name:
                raise ValueError(f"Workspace '{workspace_identifier}' not found.")
            
            # Store workspace and dataset information for CSV generation
            self.current_workspace = workspace_name
            self.current_workspace_id = workspace_id if workspace_id else 'Unknown'
            self.current_database = database_name
            self.current_database_id = 'Unknown'  # Dataset ID will be updated if available
            
            # Get client secret from AuthenticationManager
            client_secret = self.fabric.auth_manager.get_client_secret()
            
            self.connection_string = (
            f"Provider=MSOLAP;"
            f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{workspace_name};"
            f"Initial Catalog={database_name};"
            f"User ID=app:{os.getenv('AZURE_CLIENT_ID')}@{os.getenv('AZURE_TENANT_ID')};"
            f"Password={client_secret};"
            )
            self.tabularserver.Connect(self.connection_string)
            isexists = False
            for database in self.tabularserver.Databases:
                if database.Name == database_name:
                     db = database
                     isexists = True
                     break
            if not isexists:
                raise f"{database_name} not found in the provided server."

            self.model = db.Model
            self.connected = True
            
            # Try to get dataset ID from the database object if available
            try:
                if hasattr(db, 'ID') and db.ID:
                    self.current_database_id = str(db.ID)
                elif hasattr(db, 'Id') and db.Id:
                    self.current_database_id = str(db.Id)
            except:
                pass  # Keep default 'Unknown' if ID not accessible
            
            logger.info(f"✅ Connected to model '{db.Name}'.")
            logger.info(f"📊 Workspace: {self.current_workspace} (ID: {self.current_workspace_id})")
            logger.info(f"📊 Dataset: {self.current_database} (ID: {self.current_database_id})")
            
            # Automatically discover and initialize SQL endpoint from semantic model
            try:
                sql_endpoint_info = self._extract_sql_endpoint_from_model()
                if sql_endpoint_info:
                    logger.info(f"🔍 Auto-discovered SQL endpoint: {sql_endpoint_info['sql_endpoint']}")
                    logger.info(f"🔍 Auto-discovered SQL database: {sql_endpoint_info['sql_database']}")
                    
                    # Initialize SQL connection automatically
                    self.sql_metadata.initialize_sql_connection(
                        sql_endpoint_info['sql_endpoint'], 
                        sql_endpoint_info['sql_database']
                    )
                    logger.info("✅ SQL endpoint automatically initialized from semantic model")
                else:
                    logger.warning("⚠️ No SQL endpoint information found in semantic model expressions")
            except Exception as sql_error:
                logger.warning(f"⚠️ Could not auto-initialize SQL endpoint: {str(sql_error)}")
                # Connection to semantic model still successful, just no auto SQL setup
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            raise Exception(f"Connection failed: {str(e).encode('ascii', 'replace').decode('ascii')}")

    def disconnect_dataset(self):
        self.tabularserver.Disconnect()
        logger.info("Disconnected from server.")
        return "Disconnected from server is successfull"
    
    def _extract_sql_endpoint_from_model(self) -> Optional[Dict[str, str]]:
        """Extract SQL endpoint and database from semantic model expressions automatically."""
        if not self.connected or not self.model:
            return None
            
        try:
            # Query for model expressions that contain database connections
            dax_query = """
            EVALUATE 
            INFO.EXPRESSIONS()
            """
            
            expressions_result = self.execute_dax_query(dax_query)
            
            # Look for expressions containing Sql.Database connections
            for expression in expressions_result:
                expression_text = expression.get('Expression', '')
                
                # Parse DirectLake connection expressions
                if 'Sql.Database(' in expression_text:
                    # Extract server and database from expressions like:
                    # Sql.Database("server.domain.com", "database_name")
                    import re
                    
                    # Pattern to match Sql.Database("server", "database")
                    pattern = r'Sql\.Database\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'
                    match = re.search(pattern, expression_text)
                    
                    if match:
                        sql_endpoint = match.group(1)
                        sql_database = match.group(2)
                        
                        logger.info(f"🔍 Discovered SQL connection in expression '{expression.get('Name', 'Unknown')}'")
                        return {
                            'sql_endpoint': sql_endpoint,
                            'sql_database': sql_database,
                            'source_expression': expression.get('Name', 'Unknown')
                        }
            
            # Alternative: Check partition sources for DirectLake tables
            for table in self.model.Tables:
                for partition in table.Partitions:
                    if hasattr(partition, 'Source') and hasattr(partition.Source, 'Expression'):
                        source_expr = getattr(partition.Source, 'Expression', '')
                        if 'Sql.Database(' in source_expr:
                            import re
                            pattern = r'Sql\.Database\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'
                            match = re.search(pattern, source_expr)
                            
                            if match:
                                sql_endpoint = match.group(1)
                                sql_database = match.group(2)
                                
                                logger.info(f"🔍 Discovered SQL connection in table '{table.Name}' partition")
                                return {
                                    'sql_endpoint': sql_endpoint,
                                    'sql_database': sql_database,
                                    'source_expression': f'{table.Name}_partition'
                                }
            
            logger.warning("⚠️ No SQL endpoint information found in model expressions or partitions")
            return None
            
        except Exception as e:
            logger.error(f"❌ Error extracting SQL endpoint from model: {str(e)}")
            return None
    
    def list_tables(self) -> List[str]:
        """List all tables in the connected model."""
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        return [t.Name for t in self.model.Tables]
    
    def list_table_columns(self, table_name: str, include_data_type: bool = False) -> Union[List[str], List[Dict[str, str]]]:
        """
        List all columns in a specific table.
        
        Args:
            table_name: Name of the table to get columns from
            include_data_type: If True, returns list of dicts with column_name and data_type.
                              If False, returns list of column names (strings).
        
        Returns:
            List of column names or list of dictionaries with column info
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        # Find the table
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found in the model.")
        
        if include_data_type:
            # Return list of dictionaries with column_name and data_type
            return [
                {
                    "column_name": column.Name,
                    "data_type": str(column.DataType) if hasattr(column, 'DataType') else "Unknown"
                }
                for column in table.Columns
            ]
        else:
            # Return list of column names (strings)
            return [column.Name for column in table.Columns]
    
    def get_multiple_sql_tables_schema(self, table_names: List[str]) -> Dict[str, List[Dict[str, str]]]:
        """Helper function to get schema information for multiple sql tables."""
        tables_schema = {}
        for table_name in table_names:
            try:
                schema_df = self.sql_metadata.get_sql_table_schema(table_name)
                tables_schema[table_name] = [
                    {
                        "column_name": row["column_name"],
                        "data_type": row["data_type"]
                    }
                    for _, row in schema_df.iterrows()
                ]
                logger.info(f"Retrieved schema for table: {table_name} with {len(tables_schema[table_name])} columns")
            except Exception as e:
                error_msg = str(e).encode('ascii', 'replace').decode('ascii')
                logger.error(f"Error retrieving schema for table {table_name}: {error_msg}")
                tables_schema[table_name] = []
        return tables_schema
    
    def generate_tmsl_columns(self, columns_schema: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """Helper function to generate TMSL column definitions from schema information."""
        tmsl_columns = []
        
        # SQL to Analysis Services data type mapping
        type_mapping = {
            'bigint': 'int64',
            'int': 'int64',
            'smallint': 'int64',
            'tinyint': 'int64',
            'bit': 'boolean',
            'decimal': 'decimal',
            'numeric': 'decimal',
            'money': 'decimal',
            'smallmoney': 'decimal',
            'float': 'double',
            'real': 'double',
            'datetime': 'dateTime',
            'datetime2': 'dateTime',
            'smalldatetime': 'dateTime',
            'date': 'dateTime',
            'time': 'dateTime',
            'datetimeoffset': 'dateTime',
            'char': 'string',
            'varchar': 'string',
            'text': 'string',
            'nchar': 'string',
            'nvarchar': 'string',
            'ntext': 'string',
            'binary': 'binary',
            'varbinary': 'binary',
            'image': 'binary',
            'uniqueidentifier': 'string'
        }
        
        for column in columns_schema:
            column_name = column['column_name']
            sql_data_type = column['data_type'].lower()
            
            # Map SQL data type to Analysis Services data type
            as_data_type = type_mapping.get(sql_data_type, 'string')
            
            column_def = {
                "name": column_name,
                "dataType": as_data_type,
                "sourceColumn": column_name
            }
            
            tmsl_columns.append(column_def)
        
        return tmsl_columns
    
    def select_tables_with_schema(self, selected_table_names: List[str] = None) -> Dict[str, Any]:
        """Select specific tables and return their schemas, or return all tables if none specified."""
        available_tables_df = self.sql_metadata.get_sql_tables()
        available_tables = available_tables_df['table_name'].tolist()
        
        if selected_table_names:
            # Validate that all selected tables exist
            invalid_tables = [table for table in selected_table_names if table not in available_tables]
            if invalid_tables:
                raise ValueError(f"The following tables do not exist: {invalid_tables}")
            tables_to_process = selected_table_names
        else:
            tables_to_process = available_tables
        
        logger.info(f"Processing {len(tables_to_process)} tables: {tables_to_process}")
        tables_with_schema = self.get_multiple_sql_tables_schema(tables_to_process)
        
        return {
            "available_tables": available_tables,
            "selected_tables": tables_to_process,
            "tables_schema": tables_with_schema
        }

    def execute_dax_query(self, dax_query: str) -> List[Dict[str, Any]]:
        """Execute a DAX query using AdomdClient"""
        if not self.connection_string:
            raise Exception("Not connected to Power BI.")
        logger.info(f"Executing DAX query:\n{dax_query}")
        results = []
        try:
            conn = AdomdConnection(self.connection_string)
            conn.Open()
            cmd = AdomdCommand(dax_query, conn)
            reader = cmd.ExecuteReader()
            columns = [reader.GetName(i) for i in range(reader.FieldCount)]
            while reader.Read():
                row = {columns[i]: reader.GetValue(i) for i in range(len(columns))}
                results.append(row)
            reader.Close()
            conn.Close()
            logger.info(f"Returned {len(results)} rows.")
            return results
        except Exception as e:
            logger.error(f"DAX query execution failed: {str(e)}")
            raise Exception(f"DAX query execution failed: {str(e)}")
        
    def check_date_table_exists(self, table_name: str = None) -> Dict[str, Any]:
        """Check if a date table exists in the model and return its details."""
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            date_tables_info = []
            current_date_table = None
            
            # If table_name is specified, check that specific table
            if table_name:
                table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
                if not table:
                    raise Exception(f"Table '{table_name}' not found in the model.")
                
                # Check if it's marked as a date table
                is_date_table = hasattr(table, 'DataCategory') and table.DataCategory == 'Time'
                
                # Look for date columns
                date_columns = []
                for column in table.Columns:
                    if hasattr(column, 'DataType') and column.DataType == DataType.DateTime:
                        date_columns.append({
                            "name": column.Name,
                            "is_key": hasattr(column, 'IsKey') and column.IsKey,
                            "is_hidden": hasattr(column, 'IsHidden') and column.IsHidden
                        })
                
                date_tables_info.append({
                    "table_name": table.Name,
                    "is_date_table": is_date_table,
                    "date_columns": date_columns,
                    "column_count": len(table.Columns)
                })
                
                if is_date_table:
                    current_date_table = table.Name
            else:
                # Check all tables for potential date tables
                for table in self.model.Tables:
                    # Check if it's marked as a date table
                    is_date_table = hasattr(table, 'DataCategory') and table.DataCategory == 'Time'
                    
                    # Look for date columns
                    date_columns = []
                    for column in table.Columns:
                        if hasattr(column, 'DataType') and column.DataType == DataType.DateTime:
                            date_columns.append({
                                "name": column.Name,
                                "is_key": hasattr(column, 'IsKey') and column.IsKey,
                                "is_hidden": hasattr(column, 'IsHidden') and column.IsHidden
                            })
                    
                    # Consider it a potential date table if it has date columns or is marked as Time category
                    if is_date_table or len(date_columns) > 0:
                        date_tables_info.append({
                            "table_name": table.Name,
                            "is_date_table": is_date_table,
                            "date_columns": date_columns,
                            "column_count": len(table.Columns)
                        })
                        
                        if is_date_table:
                            current_date_table = table.Name
            
            result = {
                "current_date_table": current_date_table,
                "potential_date_tables": date_tables_info,
                "total_tables_checked": len(self.model.Tables) if not table_name else 1,
                "has_date_table": current_date_table is not None
            }
            
            logger.info(f"Date table check completed. Found {len(date_tables_info)} potential date tables.")
            return result
            
        except Exception as e:
            logger.error(f"Failed to check date table: {e}")
            raise Exception(f"Failed to check date table: {e}")
    
    def get_column_properties(self, table_name: str, column_name: str) -> Dict[str, Any]:
        """
        Get all available properties for a specific column with their current values and metadata.
        
        Args:
            table_name: Name of the table containing the column
            column_name: Name of the column to inspect
            
        Returns:
            Dictionary with property details including current values, types, and descriptions
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Find the column
            column = next((c for c in table.Columns if c.Name.lower() == column_name.lower()), None)
            if not column:
                raise Exception(f"Column '{column_name}' not found in table '{table_name}'.")
            
            # Define all possible column properties with their metadata
            property_definitions = {
                # Core Properties
                'Name': {'type': 'string', 'description': 'The name of the column', 'editable': True},
                'Description': {'type': 'string', 'description': 'Description of the column', 'editable': True},
                'DataType': {'type': 'DataType', 'description': 'Data type (String, Int64, DateTime, etc.)', 'editable': True},
                'SourceColumn': {'type': 'string', 'description': 'Source column name from data source', 'editable': True},
                
                # Visibility and Behavior
                'IsHidden': {'type': 'boolean', 'description': 'Whether column is hidden from client tools', 'editable': True},
                'IsKey': {'type': 'boolean', 'description': 'Whether column is marked as a key column', 'editable': True},
                'IsUnique': {'type': 'boolean', 'description': 'Whether column contains unique values', 'editable': True},
                'IsAvailableInMdx': {'type': 'boolean', 'description': 'Whether available in MDX queries', 'editable': True},
                'IsNullable': {'type': 'boolean', 'description': 'Whether column can contain null values', 'editable': True},
                
                # Calculated Columns
                'Expression': {'type': 'string', 'description': 'DAX expression for calculated columns', 'editable': True},
                'IsCalculated': {'type': 'boolean', 'description': 'Whether this is a calculated column', 'editable': False},
                
                # Formatting and Display
                'FormatString': {'type': 'string', 'description': 'Format string for display (e.g., "#,0", "mm/dd/yyyy")', 'editable': True},
                'DisplayFolder': {'type': 'string', 'description': 'Display folder in client tools', 'editable': True},
                'SortByColumn': {'type': 'Column', 'description': 'Reference to column to sort by', 'editable': True},
                'DisplayOrdinal': {'type': 'integer', 'description': 'Display order in client tools', 'editable': True},
                
                # Data Category and Summarization
                'DataCategory': {'type': 'string', 'description': 'Data category (e.g., "Time", "Geography")', 'editable': True},
                'SummarizeBy': {'type': 'AggregateFunction', 'description': 'Default aggregation function', 'editable': True},
                'IsDefaultImage': {'type': 'boolean', 'description': 'Whether this is the default image column', 'editable': True},
                'IsDefaultLabel': {'type': 'boolean', 'description': 'Whether this is the default label column', 'editable': True},
                
                # Encoding and Storage
                'EncodingHint': {'type': 'EncodingHintType', 'description': 'Storage encoding hint', 'editable': True},
                'State': {'type': 'ObjectState', 'description': 'Current state of the column', 'editable': False},
                
                # Row Level Security
                'IsPrivate': {'type': 'boolean', 'description': 'Whether column is private (RLS)', 'editable': True},
                
                # Lineage and Dependencies
                'ModifiedTime': {'type': 'DateTime', 'description': 'Last modification time', 'editable': False},
                'RefreshedTime': {'type': 'DateTime', 'description': 'Last refresh time', 'editable': False},
                'StructureModifiedTime': {'type': 'DateTime', 'description': 'Structure modification time', 'editable': False},
                
                # Annotations and Extended Properties
                'Annotations': {'type': 'AnnotationCollection', 'description': 'Custom metadata annotations', 'editable': True},
                'ExtendedProperties': {'type': 'ExtendedPropertyCollection', 'description': 'Extended properties for Power BI', 'editable': True},
                
                # Variations (Advanced)
                'Variations': {'type': 'VariationCollection', 'description': 'Column variations for different contexts', 'editable': True}
            }
            
            result = {
                'column_info': {
                    'table_name': table_name,
                    'column_name': column_name,
                    'column_type': str(type(column).__name__)
                },
                'available_properties': {},
                'current_values': {},
                'editable_properties': [],
                'readonly_properties': []
            }
            
            # Check each property and get its current value
            for prop_name, prop_info in property_definitions.items():
                try:
                    if hasattr(column, prop_name):
                        current_value = getattr(column, prop_name)
                        
                        # Convert complex objects to string representation
                        if current_value is not None:
                            if hasattr(current_value, 'Name'):  # For referenced objects
                                display_value = f"Reference: {current_value.Name}"
                            elif hasattr(current_value, '__str__') and not isinstance(current_value, (str, int, float, bool)):
                                display_value = str(current_value)
                            else:
                                display_value = current_value
                        else:
                            display_value = None
                        
                        result['available_properties'][prop_name] = prop_info
                        result['current_values'][prop_name] = display_value
                        
                        if prop_info['editable']:
                            result['editable_properties'].append(prop_name)
                        else:
                            result['readonly_properties'].append(prop_name)
                    else:
                        result['available_properties'][prop_name] = {
                            **prop_info,
                            'status': 'Not available in this version'
                        }
                except Exception as e:
                    result['available_properties'][prop_name] = {
                        **prop_info,
                        'status': f'Error accessing: {str(e)}'
                    }
            
            logger.info(f"Retrieved {len(result['available_properties'])} properties for column '{column_name}' in table '{table_name}'")
            return result
            
        except Exception as e:
            logger.error(f"Failed to get column properties: {e}")
            raise Exception(f"Failed to get column properties: {e}")
    
    def get_measure_properties(self, table_name: str, measure_name: str) -> Dict[str, Any]:
        """
        Get all available properties for a specific measure with their current values and metadata.
        
        Args:
            table_name: Name of the table containing the measure
            measure_name: Name of the measure to inspect
            
        Returns:
            Dictionary with property details including current values, types, and descriptions
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Find the measure
            measure = next((m for m in table.Measures if m.Name.lower() == measure_name.lower()), None)
            if not measure:
                raise Exception(f"Measure '{measure_name}' not found in table '{table_name}'.")
            
            # Define all possible measure properties with their metadata
            property_definitions = {
                # Core Properties
                'Name': {'type': 'string', 'description': 'The name of the measure', 'editable': True},
                'Description': {'type': 'string', 'description': 'Description of the measure', 'editable': True},
                'Expression': {'type': 'string', 'description': 'DAX expression for the measure', 'editable': True},
                
                # Visibility and Behavior
                'IsHidden': {'type': 'boolean', 'description': 'Whether measure is hidden from client tools', 'editable': True},
                'IsSimpleMeasure': {'type': 'boolean', 'description': 'Whether this is a simple measure', 'editable': False},
                
                # Formatting and Display
                'FormatString': {'type': 'string', 'description': 'Format string for display (e.g., "#,0.00", "0.0%")', 'editable': True},
                'DisplayFolder': {'type': 'string', 'description': 'Display folder in client tools', 'editable': True},
                'DisplayOrdinal': {'type': 'integer', 'description': 'Display order in client tools', 'editable': True},
                
                # KPI Properties
                'KPI': {'type': 'KPI', 'description': 'Associated KPI object if this measure is a KPI', 'editable': True},
                
                # Data Category
                'DataCategory': {'type': 'string', 'description': 'Data category for the measure', 'editable': True},
                
                # State and Metadata
                'State': {'type': 'ObjectState', 'description': 'Current state of the measure', 'editable': False},
                'ModifiedTime': {'type': 'DateTime', 'description': 'Last modification time', 'editable': False},
                'RefreshedTime': {'type': 'DateTime', 'description': 'Last refresh time', 'editable': False},
                'StructureModifiedTime': {'type': 'DateTime', 'description': 'Structure modification time', 'editable': False},
                
                # Lineage Information
                'LineageTag': {'type': 'string', 'description': 'Unique lineage identifier', 'editable': True},
                'SourceLineageTag': {'type': 'string', 'description': 'Source lineage identifier', 'editable': True},
                
                # Annotations and Extended Properties
                'Annotations': {'type': 'AnnotationCollection', 'description': 'Custom metadata annotations', 'editable': True},
                'ExtendedProperties': {'type': 'ExtendedPropertyCollection', 'description': 'Extended properties for Power BI', 'editable': True},
                
                # Dependencies and References
                'DependsOn': {'type': 'DependsOnCollection', 'description': 'Objects this measure depends on', 'editable': False},
                'ReferencedBy': {'type': 'ReferencedByCollection', 'description': 'Objects that reference this measure', 'editable': False}
            }
            
            result = {
                'measure_info': {
                    'table_name': table_name,
                    'measure_name': measure_name,
                    'measure_type': str(type(measure).__name__)
                },
                'available_properties': {},
                'current_values': {},
                'editable_properties': [],
                'readonly_properties': []
            }
            
            # Check each property and get its current value
            for prop_name, prop_info in property_definitions.items():
                try:
                    if hasattr(measure, prop_name):
                        current_value = getattr(measure, prop_name)
                        
                        # Convert complex objects to string representation
                        if current_value is not None:
                            if hasattr(current_value, 'Name'):  # For referenced objects
                                display_value = f"Reference: {current_value.Name}"
                            elif hasattr(current_value, '__str__') and not isinstance(current_value, (str, int, float, bool)):
                                display_value = str(current_value)
                            else:
                                display_value = current_value
                        else:
                            display_value = None
                        
                        result['available_properties'][prop_name] = prop_info
                        result['current_values'][prop_name] = display_value
                        
                        if prop_info['editable']:
                            result['editable_properties'].append(prop_name)
                        else:
                            result['readonly_properties'].append(prop_name)
                    else:
                        result['available_properties'][prop_name] = {
                            **prop_info,
                            'status': 'Not available in this version'
                        }
                except Exception as e:
                    result['available_properties'][prop_name] = {
                        **prop_info,
                        'status': f'Error accessing: {str(e)}'
                    }
            
            logger.info(f"Retrieved {len(result['available_properties'])} properties for measure '{measure_name}' in table '{table_name}'")
            return result
            
        except Exception as e:
            logger.error(f"Failed to get measure properties: {e}")
            raise Exception(f"Failed to get measure properties: {e}")

    def get_table_properties(self, table_name: str) -> Dict[str, Any]:
        """
        Get all available properties for a specific table with their current values and metadata.
        
        Args:
            table_name: Name of the table to inspect
            
        Returns:
            Dictionary with property details including current values, types, and descriptions
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Define all possible table properties with their metadata
            property_definitions = {
                # Core Properties
                'Name': {'type': 'string', 'description': 'The name of the table', 'editable': True},
                'Description': {'type': 'string', 'description': 'Description of the table', 'editable': True},
                
                # Visibility and Behavior
                'IsHidden': {'type': 'boolean', 'description': 'Whether table is hidden from client tools', 'editable': True},
                'IsPrivate': {'type': 'boolean', 'description': 'Whether table is private (for calculated tables)', 'editable': True},
                
                # Data Source Properties
                'Source': {'type': 'PartitionSource', 'description': 'Data source configuration for the table', 'editable': False},
                'Mode': {'type': 'ModeType', 'description': 'Storage mode (Import, DirectQuery, Dual, DirectLake)', 'editable': False},
                
                # Data Category and Organization
                'DataCategory': {'type': 'string', 'description': 'Data category (e.g., "Time", "Geography")', 'editable': True},
                'DisplayFolder': {'type': 'string', 'description': 'Display folder in client tools', 'editable': True},
                'DisplayOrdinal': {'type': 'integer', 'description': 'Display order in client tools', 'editable': True},
                
                # Date Table Properties
                'DataView': {'type': 'DataViewType', 'description': 'Data view type (Default, Sample)', 'editable': True},
                
                # State and Metadata
                'State': {'type': 'ObjectState', 'description': 'Current state of the table', 'editable': False},
                'ModifiedTime': {'type': 'DateTime', 'description': 'Last modification time', 'editable': False},
                'RefreshedTime': {'type': 'DateTime', 'description': 'Last refresh time', 'editable': False},
                'StructureModifiedTime': {'type': 'DateTime', 'description': 'Structure modification time', 'editable': False},
                
                # Lineage Information
                'LineageTag': {'type': 'string', 'description': 'Unique lineage identifier', 'editable': True},
                'SourceLineageTag': {'type': 'string', 'description': 'Source lineage identifier', 'editable': True},
                
                # Collections (Read-only references)
                'Columns': {'type': 'ColumnCollection', 'description': 'Collection of columns in the table', 'editable': False},
                'Measures': {'type': 'MeasureCollection', 'description': 'Collection of measures in the table', 'editable': False},
                'Partitions': {'type': 'PartitionCollection', 'description': 'Collection of partitions in the table', 'editable': False},
                'Hierarchies': {'type': 'HierarchyCollection', 'description': 'Collection of hierarchies in the table', 'editable': False},
                
                # Annotations and Extended Properties
                'Annotations': {'type': 'AnnotationCollection', 'description': 'Custom metadata annotations', 'editable': True},
                'ExtendedProperties': {'type': 'ExtendedPropertyCollection', 'description': 'Extended properties for Power BI', 'editable': True},
                
                # Dependencies and References
                'DependsOn': {'type': 'DependsOnCollection', 'description': 'Objects this table depends on', 'editable': False},
                'ReferencedBy': {'type': 'ReferencedByCollection', 'description': 'Objects that reference this table', 'editable': False},
                
                # Advanced Properties
                'ShowAsVariationsOnly': {'type': 'boolean', 'description': 'Whether to show only as variations', 'editable': True},
                'RequestId': {'type': 'string', 'description': 'Request ID for tracking', 'editable': False}
            }
            
            result = {
                'table_info': {
                    'table_name': table_name,
                    'table_type': str(type(table).__name__),
                    'column_count': len(table.Columns) if hasattr(table, 'Columns') else 0,
                    'measure_count': len(table.Measures) if hasattr(table, 'Measures') else 0
                },
                'available_properties': {},
                'current_values': {},
                'editable_properties': [],
                'readonly_properties': []
            }
            
            # Check each property and get its current value
            for prop_name, prop_info in property_definitions.items():
                try:
                    if hasattr(table, prop_name):
                        current_value = getattr(table, prop_name)
                        
                        # Convert complex objects to string representation
                        if current_value is not None:
                            if hasattr(current_value, 'Name'):  # For referenced objects
                                display_value = f"Reference: {current_value.Name}"
                            elif hasattr(current_value, 'Count'):  # For collections
                                display_value = f"Collection with {current_value.Count} items"
                            elif hasattr(current_value, '__str__') and not isinstance(current_value, (str, int, float, bool)):
                                display_value = str(current_value)
                            else:
                                display_value = current_value
                        else:
                            display_value = None
                        
                        result['available_properties'][prop_name] = prop_info
                        result['current_values'][prop_name] = display_value
                        
                        if prop_info['editable']:
                            result['editable_properties'].append(prop_name)
                        else:
                            result['readonly_properties'].append(prop_name)
                    else:
                        result['available_properties'][prop_name] = {
                            **prop_info,
                            'status': 'Not available in this version'
                        }
                except Exception as e:
                    result['available_properties'][prop_name] = {
                        **prop_info,
                        'status': f'Error accessing: {str(e)}'
                    }
            
            logger.info(f"Retrieved {len(result['available_properties'])} properties for table '{table_name}'")
            return result
            
        except Exception as e:
            logger.error(f"Failed to get table properties: {e}")
            raise Exception(f"Failed to get table properties: {e}")

    def list_all_relationships(self) -> Dict[str, Any]:
        """List all relationships and include the count with cardinality and cross filter direction."""
        if not self.connected:
            raise Exception("Tabular server is not connected")
           
        relationships = []
        for rel in self.model.Relationships:
            rel_id = getattr(rel, 'Name', None) or getattr(rel, 'ID', None)
           
            # Debug: Print available properties (remove this after testing)
            logger.info(f"Available relationship properties: {[attr for attr in dir(rel) if not attr.startswith('_')]}")
           
            # Map cardinality to readable format - try different property names
            cardinality = "unknown"
           
            # Try different possible cardinality property names
            if hasattr(rel, 'FromCardinality') and hasattr(rel, 'ToCardinality'):
                from_card = rel.FromCardinality
                to_card = rel.ToCardinality
                logger.info(f"FromCardinality: {from_card}, ToCardinality: {to_card}")
            elif hasattr(rel, 'Cardinality'):
                cardinality_value = rel.Cardinality
                logger.info(f"Cardinality property: {cardinality_value}")
                # Map enum values to strings
                if str(cardinality_value) == "OneToMany":
                    cardinality = "one_to_many"
                elif str(cardinality_value) == "ManyToOne":
                    cardinality = "many_to_one"
                elif str(cardinality_value) == "OneToOne":
                    cardinality = "one_to_one"
                elif str(cardinality_value) == "ManyToMany":
                    cardinality = "many_to_many"
           
            # Map cross filter direction to readable format
            cross_filter_direction = "unknown"
            if hasattr(rel, 'CrossFilteringBehavior'):
                filter_behavior = rel.CrossFilteringBehavior
                if str(filter_behavior) == "OneDirection":
                    cross_filter_direction = "single"
                elif str(filter_behavior) == "BothDirections":
                    cross_filter_direction = "both"
                elif str(filter_behavior) == "Automatic":
                    cross_filter_direction = "automatic"
           
            # Get active status
            is_active = getattr(rel, 'IsActive', True)
           
            relationships.append({
                "from_table": rel.FromTable.Name,
                "from_column": rel.FromColumn.Name,
                "to_table": rel.ToTable.Name,
                "to_column": rel.ToColumn.Name,
                "relationship_id": rel_id,
                "cardinality": cardinality,
                "cross_filter_direction": cross_filter_direction,
                "is_active": is_active
            })
       
        logger.info(f"Found {len(relationships)} relationships in total.")
        return {"relationships": relationships, "count": len(relationships)}
    
    def _generate_erd_diagram(self, diagram_data: Dict[str, Any]) -> Optional[str]:
        """Generate a clean ERD (Entity Relationship Diagram) using Graphviz with column counts."""
        try:
            import graphviz
            
            logger.info("Starting ERD diagram generation with Graphviz...")

            # Try to find Graphviz installation and add to PATH if needed
            self._ensure_graphviz_path()

            # Create Graphviz Digraph object with PNG format
            dot = graphviz.Digraph(comment=f"ERD: {diagram_data['model_name']}")
            dot.format = 'png'  # Explicitly set format
            dot.attr(rankdir="LR", fontsize="10", newrank="true")
            dot.attr("node", shape="record", fontname="Arial")

            # Add tables with column counts instead of detailed listings
            for table_name, table_info in diagram_data["tables"].items():
                
                # Count different types of columns
                visible_columns = [col for col in table_info['columns'] if not col.get('is_hidden', False)]
                pk_columns = [col for col in visible_columns if col.get('is_key', False)]
                fk_columns = [col for col in visible_columns if col.get('relationships') and not col.get('is_key', False)]
                measures = table_info.get('measures', [])
                
                # Build summary information
                summary_info = []
                summary_info.append(f"Type: {table_info.get('table_type', 'Other')}")
                summary_info.append(f"Total Columns: {len(visible_columns)}")
                
                if pk_columns:
                    summary_info.append(f"Primary Keys: {len(pk_columns)}")
                if fk_columns:
                    summary_info.append(f"Foreign Keys: {len(fk_columns)}")
                if measures:
                    summary_info.append(f"Measures: {len(measures)}")

                # Create table label with counts
                if summary_info:
                    label = f"{{ {table_name} | {{ {' | '.join(summary_info)} }} }}"
                else:
                    label = f"{{ {table_name} | {{ No columns found }} }}"

                # Color coding based on table type
                table_type = table_info.get('table_type', 'Other')
                if table_type == 'Fact Table':
                    dot.node(table_name, label=label, fillcolor="lightblue", style="filled")
                elif table_type == 'Dimension Table':
                    dot.node(table_name, label=label, fillcolor="lightgreen", style="filled")
                else:
                    dot.node(table_name, label=label, fillcolor="lightyellow", style="filled")

            # Add relationships
            for rel in diagram_data["relationships"]:
                from_table = rel["from_table"]
                to_table = rel["to_table"]
                from_col = rel.get("from_column", "")
                to_col = rel.get("to_column", "")
                cardinality = rel.get("cardinality", "")

                # Edge label for relationship details
                edge_label = f"{from_col} → {to_col}"
                if cardinality:
                    edge_label += f" ({cardinality})"

                # Style based on active/inactive status
                is_active = rel.get("is_active", True)
                if is_active:
                    dot.edge(from_table, to_table, label=edge_label, fontsize="8", color="blue")
                else:
                    dot.edge(from_table, to_table, label=edge_label, fontsize="8", 
                           color="gray", style="dashed")

            # Save diagram
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            workspace_name = diagram_data["workspace_info"].get("workspace_name", "Unknown")
            model_name = diagram_data["model_name"]

            # Clean up names for filename
            clean_workspace = re.sub(r'[^\w\-_\.]', '_', workspace_name)
            clean_model = re.sub(r'[^\w\-_\.]', '_', model_name)

            diagram_filename = f"ERD_Diagram_{clean_workspace}_{clean_model}_{timestamp}"
            
            # Create the output directory
            output_dir = "c:\\MCP-Ai"
            os.makedirs(output_dir, exist_ok=True)
            
            # Full path without extension (graphviz will add .png)
            diagram_filepath = os.path.join(output_dir, diagram_filename)
            
            logger.info(f"Rendering ERD diagram to: {diagram_filepath}.png")
            
            # Render the diagram - this creates a .png file
            try:
                rendered_path = dot.render(diagram_filepath, format='png', cleanup=True)
                logger.info(f"Graphviz render completed. Rendered path: {rendered_path}")
                
                # The rendered path should be the PNG file
                if os.path.exists(rendered_path):
                    logger.info(f"ERD PNG diagram saved successfully to: {rendered_path}")
                    return rendered_path
                else:
                    # Try with .png extension added
                    png_path = f"{diagram_filepath}.png"
                    if os.path.exists(png_path):
                        logger.info(f"ERD PNG diagram saved successfully to: {png_path}")
                        return png_path
                    else:
                        logger.error(f"ERD PNG diagram file was not created. Checked: {rendered_path} and {png_path}")
                        return None
                        
            except Exception as render_error:
                logger.error(f"Graphviz rendering failed: {render_error}")
                return None

        except ImportError as e:
            logger.error(f"Graphviz library not available. Please install graphviz: pip install graphviz. Error: {e}")
            return None
        except Exception as e:
            error_msg = str(e).lower()
            if "failed to execute" in error_msg and "dot" in error_msg:
                logger.error("❌ Graphviz executables not found in PATH!")
                logger.error("📥 Please install Graphviz from: https://graphviz.org/download/")
                logger.error("🔧 For Windows: Download and install from https://graphviz.org/download/#windows")
                logger.error("🔧 Make sure to add Graphviz\\bin to your system PATH environment variable")
                logger.error("🔧 Alternative: Use 'winget install graphviz' or 'choco install graphviz'")
            else:
                logger.error(f"Failed to generate ERD diagram: {e}")
                import traceback
                logger.error(f"Full traceback: {traceback.format_exc()}")
            return None

    def _ensure_graphviz_path(self):
        """Ensure Graphviz executables are in PATH, add common installation paths if needed."""
        import shutil
        
        # Check if dot is already available in PATH
        if shutil.which('dot') is not None:
            logger.info("Graphviz 'dot' executable found in PATH")
            return
        
        # Common Graphviz installation paths on Windows
        common_paths = [
            r"C:\Program Files\Graphviz\bin",
            r"C:\Program Files (x86)\Graphviz\bin",
            r"C:\Graphviz\bin"
        ]
        
        for path in common_paths:
            dot_path = os.path.join(path, "dot.exe")
            if os.path.exists(dot_path):
                logger.info(f"Found Graphviz at {path}, adding to PATH for this session")
                if path not in os.environ.get('PATH', ''):
                    os.environ['PATH'] += f";{path}"
                return
        
        logger.warning("Graphviz not found in common installation paths")
        logger.warning("Please ensure Graphviz is installed and 'dot' is available in PATH")

class CopilotDataEvaluator:
    """
    Streamlined class for Power BI dataset column analysis.
    Generates one comprehensive CSV file with 14 focused columns for optimal analysis.
    """
    def __init__(self, tabular_editor: TabularEditor, sql_endpoint: SQLEndpoint):
        self.tabular_editor = tabular_editor
        self.sql_endpoint = sql_endpoint

    def dataset_columns_analysis(self) -> Dict[str, Any]:
        """
        Comprehensive dataset column analysis with NLTK linguistic assessment and DAX data validation.
        """
        import re
        import pandas as pd
        import os
        from datetime import datetime
        
        def ensure_nltk_data():
            """Ensure required NLTK data is downloaded"""
            try:
                import nltk
                required_data = [
                    ('tokenizers/punkt_tab', 'punkt_tab'),
                    ('corpora/stopwords', 'stopwords'),
                    ('corpora/wordnet', 'wordnet'),
                    ('taggers/averaged_perceptron_tagger_eng', 'averaged_perceptron_tagger_eng')
                ]
                
                for path, name in required_data:
                    try:
                        nltk.data.find(path)
                    except LookupError:
                        logger.info(f"Downloading NLTK data: {name}")
                        nltk.download(name, quiet=True)
            except Exception as e:
                logger.warning(f"NLTK data download failed: {e}")
                raise
        
        def detect_business_domain(column_name: str) -> str:
            """Detect business domain based on column name patterns"""
            name_lower = column_name.lower()
            domain_indicators = {
                'financial': ['price', 'cost', 'amount', 'budget', 'currency', 'money', 'payment', 'billing'],
                'temporal': ['date', 'time', 'period', 'year', 'month', 'day', 'timestamp', 'created', 'modified'],
                'identity': ['id', 'key', 'identifier', 'name', 'title', 'label', 'code'],
                'quantity': ['count', 'number', 'quantity', 'amount', 'total', 'sum', 'qty'],
                'status': ['status', 'state', 'flag', 'active', 'enabled', 'valid'],
                'hierarchy': ['level', 'tier', 'hierarchy', 'parent', 'child', 'group'],
                'classification': ['type', 'category', 'class', 'segment', 'area', 'region']
            }
            
            for domain, indicators in domain_indicators.items():
                if any(indicator in name_lower for indicator in indicators):
                    return domain
            return "general"
        
        def format_column_name(column_name: str) -> str:
            """Format column name with proper space separation and capitalization"""
            # Handle lowercase column names - convert to proper case
            if column_name.islower() and len(column_name) > 2:
                # Special handling for common patterns like 'employeeid' -> 'Employee ID'
                if 'id' in column_name.lower() and column_name.lower().endswith('id'):
                    base_name = column_name[:-2]
                    return f"{base_name.title()} ID"
                else:
                    return column_name.title()
            
            # First, handle underscores - convert to spaces for readability
            if '_' in column_name:
                return column_name.replace('_', ' ')
            
            # Check if has separators or numbers - be conservative
            if '-' in column_name or re.search(r'\d', column_name):
                if re.search(r'[a-z][A-Z]', column_name) and column_name.count('-') == 0:
                    # Pure camelCase - split it with spaces
                    formatted = re.sub(r'([a-z])([A-Z])', r'\1 \2', column_name)
                    return re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', formatted)
                else:
                    return column_name  # Keep as-is if has separators/numbers
            elif re.search(r'[a-z][A-Z]', column_name):
                # camelCase/PascalCase - add spaces
                # First handle lowercase-to-uppercase transitions (e.g., "big" + "A" -> "big A")
                formatted = re.sub(r'([a-z])([A-Z])', r'\1 \2', column_name)
                # Then handle sequences of uppercase letters followed by lowercase (e.g., "Area" + "Name" -> "Area Name")
                formatted = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', formatted)
                # Handle multiple consecutive uppercase letters properly (e.g., "XMLParser" -> "XML Parser")
                formatted = re.sub(r'([A-Z])([A-Z][a-z])', r'\1 \2', formatted)
                return formatted
            elif column_name.isupper() and len(column_name) > 3:
                return column_name  # Keep uppercase abbreviations
            else:
                return column_name  # Single word or well-formatted
        
        def basic_fallback_analysis(column_name: str) -> Dict[str, Any]:
            """Fallback analysis when NLTK is unavailable"""
            issues = []
            confidence = 50
            
            # Check for lowercase column names
            if column_name.islower() and len(column_name) > 2:
                issues.append("Lowercase column name - needs proper capitalization")
                confidence = 40
            
            if len(column_name) <= 2:
                issues.append("Column name extremely short")
                confidence = 20
            elif len(column_name) <= 3:
                issues.append("Very short column name - may need expansion")
                confidence = 40
            
            business_domain = detect_business_domain(column_name)
            
            return {
                "is_unambiguous": len(issues) == 0,
                "is_self_explanatory": len(column_name) > 3 and len(issues) == 0,
                "name_issues": issues,
                "suggested_improvements": [format_column_name(column_name)],
                "linguistic_analysis": {"business_domain": business_domain},
                "confidence_score": confidence,
                "primary_issue": issues[0] if issues else "None"
            }
        
        def analyze_column_name_with_nltk(column_name: str) -> Dict[str, Any]:
            """Analyze column name using NLTK for linguistic assessment"""
            try:
                if not NLTK_AVAILABLE:
                    return basic_fallback_analysis(column_name)
                
                # Import and initialize NLTK
                import nltk
                from nltk.corpus import wordnet
                from nltk.tokenize import word_tokenize
                from nltk.tag import pos_tag
                
                ensure_nltk_data()
                
                # Clean and tokenize column name
                name_cleaned = re.sub(r'([a-z])([A-Z])', r'\1 \2', column_name)
                name_cleaned = re.sub(r'[_-]', ' ', name_cleaned)
                name_cleaned = re.sub(r'[^a-zA-Z\s]', ' ', name_cleaned)
                name_cleaned = re.sub(r'\s+', ' ', name_cleaned).strip().lower()
                
                if not name_cleaned:
                    return {
                        "is_unambiguous": False, "is_self_explanatory": False,
                        "name_issues": ["Column name contains no meaningful text"],
                        "suggested_improvements": [format_column_name(column_name)],
                        "linguistic_analysis": {"business_domain": "general"},
                        "confidence_score": 0, "primary_issue": "No meaningful text"
                    }
                
                tokens = word_tokenize(name_cleaned)
                tokens = [token for token in tokens if token.isalpha() and len(token) > 1]
                
                if not tokens:
                    return {
                        "is_unambiguous": False, "is_self_explanatory": False,
                        "name_issues": ["No meaningful words found"],
                        "suggested_improvements": [format_column_name(column_name)],
                        "linguistic_analysis": {"business_domain": "general"},
                        "confidence_score": 20, "primary_issue": "No meaningful words"
                    }
                
                # Analyze tokens
                abbreviations = []
                unclear_words = []
                meaningful_words = []
                
                for token in tokens:
                    synsets = wordnet.synsets(token)
                    
                    if len(token) <= 2:
                        abbreviations.append(token)
                    elif len(token) == 3 and not synsets:
                        abbreviations.append(token)
                    elif not synsets and len(token) > 3:
                        # Check if it's a business compound term
                        business_patterns = ['level', 'code', 'name', 'type', 'amount', 'date', 'status', 'price', 'group']
                        if any(pattern in token.lower() for pattern in business_patterns):
                            meaningful_words.append(token)
                        else:
                            unclear_words.append(token)
                    else:
                        meaningful_words.append(token)
                
                # Calculate confidence and determine issues
                confidence = 100
                issues = []
                
                # Check for lowercase column names
                if column_name.islower() and len(column_name) > 2:
                    confidence -= 15
                    issues.append("Lowercase column name - needs proper capitalization")
                
                if abbreviations:
                    confidence -= len(abbreviations) * 20
                    issues.extend([f"Abbreviation '{abbr}'" for abbr in abbreviations])
                
                if unclear_words:
                    confidence -= len(unclear_words) * 15
                    issues.extend([f"Unclear term '{word}'" for word in unclear_words])
                
                if len(meaningful_words) == 1 and meaningful_words[0] in ['data', 'info', 'value', 'field']:
                    confidence -= 20
                    issues.append(f"Generic term '{meaningful_words[0]}'")
                
                # Determine business domain
                detected_domain = detect_business_domain(column_name)
                
                # Generate suggestions
                if issues:
                    suggested_name = format_column_name(column_name)
                else:
                    suggested_name = format_column_name(column_name)
                
                return {
                    "is_unambiguous": len(issues) == 0,
                    "is_self_explanatory": len(meaningful_words) > 0 and len(issues) == 0,
                    "name_issues": issues,
                    "suggested_improvements": [suggested_name],
                    "linguistic_analysis": {"business_domain": detected_domain},
                    "confidence_score": max(0, confidence),
                    "primary_issue": issues[0] if issues else "None"
                }
                
            except Exception as e:
                logger.warning(f"NLTK analysis failed for '{column_name}': {e}")
                return basic_fallback_analysis(column_name)
        
        def analyze_column_data_with_dax(table_name: str, column_name: str, data_type: str) -> Dict[str, Any]:
            """Analyze actual column data using DAX queries"""
            try:
                escaped_table = f"'{table_name}'" if ' ' in table_name else table_name
                escaped_column = f"'{column_name}'" if ' ' in column_name else column_name
                
                analysis_result = {
                    "actual_data_type": data_type,
                    "data_type_issues": [],
                    "suggested_data_type": data_type,
                    "sample_values": [],
                    "data_analysis": {}
                }
                
                # Get sample values
                sample_query = f"""
                EVALUATE
                TOPN(10, SUMMARIZE({escaped_table}, {escaped_table}[{escaped_column}]))
                """
                
                try:
                    sample_result = self.sql_endpoint.execute_dax_query(sample_query)
                    if sample_result and len(sample_result) > 0:
                        sample_values = [str(row[0]) if row[0] is not None else "NULL" for row in sample_result[:5]]
                        analysis_result["sample_values"] = sample_values
                        analyze_sample_values(sample_values, analysis_result, data_type)
                except Exception as e:
                    logger.warning(f"Could not get sample values for {table_name}[{column_name}]: {e}")
                
                # Check for type issues based on current type
                if data_type in ['String', 'Text'] or 'string' in data_type.lower():
                    check_text_column_issues(escaped_table, escaped_column, analysis_result)
                elif data_type in ['Integer', 'Decimal', 'Double', 'Currency']:
                    check_numeric_column_issues(escaped_table, escaped_column, analysis_result, data_type)
                
                return analysis_result
                
            except Exception as e:
                logger.error(f"Error analyzing column data with DAX: {e}")
                return {
                    "actual_data_type": data_type,
                    "data_type_issues": [f"Could not analyze actual data: {str(e)}"],
                    "suggested_data_type": data_type,
                    "sample_values": [],
                    "data_analysis": {}
                }
        
        def analyze_sample_values(sample_values: list, analysis_result: dict, current_type: str):
            """Analyze sample values to detect type inconsistencies"""
            numeric_count = date_count = null_count = 0
            
            for value in sample_values:
                if value == "NULL" or value is None:
                    null_count += 1
                    continue
                    
                # Check numeric
                try:
                    float(str(value).replace(',', '').replace('$', '').replace('%', ''))
                    numeric_count += 1
                except ValueError:
                    pass
                
                # Check date patterns
                if looks_like_date(str(value)):
                    date_count += 1
            
            total_non_null = len(sample_values) - null_count
            if total_non_null > 0:
                if numeric_count / total_non_null >= 0.8 and current_type in ['String', 'Text']:
                    analysis_result["data_type_issues"].append("Most values appear numeric but stored as text")
                    has_decimals = any('.' in str(v) for v in sample_values if v != "NULL")
                    analysis_result["suggested_data_type"] = "Decimal" if has_decimals else "Integer"
                
                elif date_count / total_non_null >= 0.8 and current_type in ['String', 'Text']:
                    analysis_result["data_type_issues"].append("Most values appear to be dates but stored as text")
                    analysis_result["suggested_data_type"] = "DateTime"
        
        def looks_like_date(value: str) -> bool:
            """Check if a string value looks like a date"""
            date_patterns = [
                r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}',  # MM/DD/YYYY
                r'\d{4}[/-]\d{1,2}[/-]\d{1,2}',    # YYYY/MM/DD
                r'\d{4}-\d{2}-\d{2}',              # YYYY-MM-DD
                r'\w{3}\s+\d{1,2},?\s+\d{4}',      # Jan 1, 2023
            ]
            
            return any(re.match(pattern, str(value).strip()) for pattern in date_patterns)
        
        def check_text_column_issues(escaped_table: str, escaped_column: str, analysis_result: dict):
            """Check if text column contains numeric or date data"""
            try:
                # Check numeric data
                numeric_query = f"""
                EVALUATE
                {{
                    ("TotalRows", COUNTROWS({escaped_table})),
                    ("NumericRows", COUNTROWS(FILTER({escaped_table}, ISNUMBER(VALUE({escaped_table}[{escaped_column}])))))
                }}
                """
                
                try:
                    result = self.sql_endpoint.execute_dax_query(numeric_query)
                    if result and len(result) >= 2:
                        total_rows = result[0][1] if len(result[0]) > 1 else 0
                        numeric_rows = result[1][1] if len(result[1]) > 1 else 0
                        
                        if total_rows > 0 and numeric_rows / total_rows >= 0.8:
                            analysis_result["data_type_issues"].append(f"CRITICAL: {numeric_rows}/{total_rows} rows contain numeric data stored as text")
                            analysis_result["suggested_data_type"] = "Decimal"
                            analysis_result["data_analysis"]["numeric_percentage"] = round((numeric_rows / total_rows) * 100, 1)
                except Exception as e:
                    logger.debug(f"Numeric check failed: {e}")
                
                # Check date patterns
                date_query = f"""
                EVALUATE
                {{
                    ("TotalRows", COUNTROWS({escaped_table})),
                    ("DateLikeRows", COUNTROWS(FILTER({escaped_table}, 
                        LEN({escaped_table}[{escaped_column}]) >= 8 
                        && LEN({escaped_table}[{escaped_column}]) <= 25
                        && (FIND("/", {escaped_table}[{escaped_column}], 1, 0) > 0 
                            || FIND("-", {escaped_table}[{escaped_column}], 1, 0) > 0)
                    )))
                }}
                """
                
                try:
                    result = self.sql_endpoint.execute_dax_query(date_query)
                    if result and len(result) >= 2:
                        total_rows = result[0][1] if len(result[0]) > 1 else 0
                        date_rows = result[1][1] if len(result[1]) > 1 else 0
                        
                        if total_rows > 0 and date_rows / total_rows >= 0.7:
                            analysis_result["data_type_issues"].append(f"Potential date data: {date_rows}/{total_rows} rows have date-like patterns")
                            if "Decimal" not in analysis_result["suggested_data_type"]:
                                analysis_result["suggested_data_type"] = "DateTime"
                            analysis_result["data_analysis"]["date_like_percentage"] = round((date_rows / total_rows) * 100, 1)
                except Exception as e:
                    logger.debug(f"Date check failed: {e}")
                    
            except Exception as e:
                logger.warning(f"Error checking text column: {e}")
        
        def check_numeric_column_issues(escaped_table: str, escaped_column: str, analysis_result: dict, data_type: str):
            """Check numeric columns for optimization opportunities"""
            try:
                consistency_query = f"""
                EVALUATE
                {{
                    ("TotalRows", COUNTROWS({escaped_table})),
                    ("NullRows", COUNTROWS(FILTER({escaped_table}, ISBLANK({escaped_table}[{escaped_column}])))),
                    ("MinValue", MIN({escaped_table}[{escaped_column}])),
                    ("MaxValue", MAX({escaped_table}[{escaped_column}]))
                }}
                """
                
                try:
                    result = self.sql_endpoint.execute_dax_query(consistency_query)
                    if result and len(result) >= 4:
                        total_rows = result[0][1] if len(result[0]) > 1 else 0
                        null_rows = result[1][1] if len(result[1]) > 1 else 0
                        min_val = result[2][1] if len(result[2]) > 1 else None
                        max_val = result[3][1] if len(result[3]) > 1 else None
                        
                        analysis_result["data_analysis"]["null_percentage"] = round((null_rows / total_rows) * 100, 1) if total_rows > 0 else 0
                        analysis_result["data_analysis"]["value_range"] = f"{min_val} to {max_val}"
                        
                        # Check if Integer is more appropriate than Decimal
                        if data_type == "Decimal" and min_val is not None and max_val is not None:
                            if min_val == int(min_val) and max_val == int(max_val):
                                whole_number_query = f"""
                                EVALUATE
                                COUNTROWS(FILTER({escaped_table}, 
                                    {escaped_table}[{escaped_column}] <> INT({escaped_table}[{escaped_column}])
                                ))
                                """
                                try:
                                    decimal_result = self.sql_endpoint.execute_dax_query(whole_number_query)
                                    if decimal_result and len(decimal_result) > 0 and decimal_result[0][0] == 0:
                                        analysis_result["data_type_issues"].append("All values are whole numbers - Integer type more appropriate")
                                        analysis_result["suggested_data_type"] = "Integer"
                                except:
                                    pass
                                    
                except Exception as e:
                    logger.debug(f"Consistency check failed: {e}")
                    
            except Exception as e:
                logger.warning(f"Error checking numeric column: {e}")
        
        # Main analysis function
        try:
            if not self.tabular_editor.connected:
                return {"error": "Not connected to any dataset. Please connect first."}

            # Get workspace and dataset information
            workspace_info = {
                "WorkspaceID": getattr(self.tabular_editor, 'current_workspace_id', 'Unknown'),
                "WorkspaceName": getattr(self.tabular_editor, 'current_workspace', 'Unknown'),
                "DatasetID": getattr(self.tabular_editor, 'current_database_id', 'Unknown'),
                "DatasetName": getattr(self.tabular_editor, 'current_database', 'Unknown')
            }

            analysis_results = []
            tables = self.tabular_editor.model.Tables
            
            # Process each table and column
            for table in tables:
                table_name = table.Name
                
                for column in table.Columns:
                    column_name = column.Name
                    data_type = str(column.DataType) if hasattr(column, 'DataType') else "Unknown"
                    
                    # Skip columns that start with RowNumber- as they are not required in the analysis
                    if column_name.startswith("RowNumber-"):
                        continue
                    
                    # Analyze column name with NLTK
                    nltk_analysis = analyze_column_name_with_nltk(column_name)
                    
                    # Analyze data type with DAX
                    dax_analysis = analyze_column_data_with_dax(table_name, column_name, data_type)
                    
                    # Extract results
                    name_issues = nltk_analysis["name_issues"]
                    type_issues = dax_analysis["data_type_issues"]
                    suggested_name = format_column_name(column_name)
                    suggested_datatype = dax_analysis["suggested_data_type"]
                    
                    # Determine clarity flags
                    confidence = nltk_analysis["confidence_score"]
                    if confidence >= 80:
                        is_unambiguous = 'YES - Clear and self-explanatory'
                        is_self_explanatory = 'YES'
                    elif confidence >= 60:
                        is_unambiguous = 'PARTIAL - Some clarity issues'
                        is_self_explanatory = 'PARTIAL - Needs some clarification'
                    else:
                        primary_issue = nltk_analysis.get("primary_issue", "Multiple issues")
                        is_unambiguous = f'NO - {primary_issue}'
                        is_self_explanatory = 'NO - Requires additional context'
                    
                    # Handle specific cases
                    name_lower = column_name.lower()
                    if len(column_name) < 3:
                        is_unambiguous = 'NO - Too brief to be descriptive'
                        is_self_explanatory = 'NO - Too short to be descriptive'
                    elif name_lower in ['column1', 'field1', 'data1', 'value1', 'field', 'column']:
                        is_unambiguous = 'NO - Generic name provides no business meaning'
                        is_self_explanatory = 'NO - Generic/default name'
                    
                    # Calculate quality scores
                    overall_quality_score = 100
                    if type_issues:
                        overall_quality_score -= 35
                    overall_quality_score -= (100 - confidence) * 0.25
                    if len(column_name) < 3:
                        overall_quality_score -= 15
                    if name_lower in ['column1', 'field1', 'data1', 'value1', 'field', 'column']:
                        overall_quality_score -= 20
                    if column_name.islower() and len(column_name) > 2:
                        overall_quality_score -= 10  # Penalty for lowercase formatting
                    overall_quality_score = max(0, int(overall_quality_score))
                    
                    # Determine priority
                    all_issues = name_issues + type_issues
                    if not all_issues:
                        priority = "NONE"
                    elif any("CRITICAL" in issue for issue in type_issues) or any("default name" in issue.lower() for issue in all_issues):
                        priority = "HIGH"
                    elif confidence < 40 or any("abbreviation" in issue.lower() for issue in name_issues) or any("lowercase column name" in issue.lower() for issue in name_issues):
                        priority = "MEDIUM"
                    else:
                        priority = "LOW"
                    
                    # Generate recommendations
                    if not all_issues:
                        recommendation = "No issues found - follows good practices"
                    else:
                        recommendations = []
                        for issue in all_issues:
                            if "lowercase column name" in issue.lower():
                                recommendations.append("FORMATTING: Convert to proper case (e.g., employeeid → Employee ID)")
                            elif "abbreviation" in issue.lower():
                                recommendations.append("NAMING: Expand abbreviations to full words")
                            elif "CRITICAL" in issue and "numeric data" in issue.lower():
                                percentage = dax_analysis.get("data_analysis", {}).get("numeric_percentage", "")
                                percentage_str = f" ({percentage}% numeric)" if percentage else ""
                                recommendations.append(f"CRITICAL: Convert numeric data from Text to {suggested_datatype}{percentage_str}")
                            elif "date" in issue.lower() and "text" in issue.lower():
                                recommendations.append("IMPROVEMENT: Convert date data from Text to DateTime")
                            elif "whole numbers" in issue.lower():
                                recommendations.append("OPTIMIZATION: Convert from Decimal to Integer for better performance")
                            elif "default name" in issue.lower():
                                recommendations.append("CRITICAL: Replace generic name with meaningful business term")
                            elif "too short" in issue.lower():
                                recommendations.append("IMPROVEMENT: Use more descriptive name")
                        recommendation = " | ".join(recommendations)
                    
                    # Add to results
                    analysis_results.append({
                        "WorkspaceID": workspace_info["WorkspaceID"],
                        "WorkspaceName": workspace_info["WorkspaceName"],
                        "DatasetID": workspace_info["DatasetID"],
                        "DatasetName": workspace_info["DatasetName"],
                        "Table_Name": table_name,
                        "Column_Name": column_name,
                        "Current_Data_Type": data_type,
                        "Is_Unambiguous_Label": is_unambiguous,
                        "Is_Self_Explanatory": is_self_explanatory,
                        "Suggested_DataType": suggested_datatype,
                        "Suggested_Name": suggested_name,
                        "Priority": priority,
                        "Overall_Quality_Score": overall_quality_score,
                        "Detailed_Recommendation": recommendation
                    })
            
            # Generate CSV file
            if not analysis_results:
                return {"error": "No analysis results to export"}
            
            df = pd.DataFrame(analysis_results)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            workspace_name = analysis_results[0]["WorkspaceName"].replace(" ", "")
            dataset_name = analysis_results[0]["DatasetName"].replace(" ", "")
            
            filename = f"Dataset_Columns_Analysis_{workspace_name}_{dataset_name}_{timestamp}.csv"
            file_path = os.path.join("c:\\MCP-Ai", filename)
            
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            df.to_csv(file_path, index=False, encoding='utf-8')
            
            # Generate summary
            total_columns = len(analysis_results)
            priority_counts = {
                "HIGH": sum(1 for r in analysis_results if r["Priority"] == "HIGH"),
                "MEDIUM": sum(1 for r in analysis_results if r["Priority"] == "MEDIUM"),
                "LOW": sum(1 for r in analysis_results if r["Priority"] == "LOW"),
                "NONE": sum(1 for r in analysis_results if r["Priority"] == "NONE")
            }
            
            avg_quality = sum(r["Overall_Quality_Score"] for r in analysis_results) / total_columns
            clear_names = sum(1 for r in analysis_results if r["Is_Unambiguous_Label"].startswith("YES"))
            self_explanatory = sum(1 for r in analysis_results if r["Is_Self_Explanatory"].startswith("YES"))
            
            summary = {
                "total_columns": total_columns,
                "priority_distribution": priority_counts,
                "quality_scores": {
                    "average_overall_quality": round(avg_quality, 2),
                    "average_name_quality": round((clear_names / total_columns) * 100, 2)
                },
                "clarity_assessment": {
                    "clear_and_self_explanatory": self_explanatory,
                    "percentage_unambiguous": round((self_explanatory / total_columns) * 100, 2)
                }
            }
            
            logger.info(f"✅ Dataset Columns Analysis completed: {file_path}")
            
            return {
                "status": "success",
                "total_columns_analyzed": total_columns,
                "csv_file_path": file_path,
                "summary": summary
            }
            
        except Exception as e:
            logger.error(f"Error in dataset_columns_analysis: {str(e)}")
            return {"error": f"Analysis failed: {str(e)}"}

    def evaluate_security_roles(self, semantic_model_name: str = None) -> Dict[str, Any]:
        """
        Comprehensive table-level security role evaluation.
        Checks each table in the model, evaluates roles, sensitivity, and record counts,
        then generates recommendations and CSV output.
        """
        
        if not self.tabular_editor.connected:
            raise Exception("Tabular server is not connected. Please connect to a dataset first.")
        
        try:
            logger.info("Starting enhanced security role evaluation...")
            
            # Resolve model/workspace
            dataset_name = semantic_model_name or getattr(self.tabular_editor, 'current_database', 'Unknown')
            workspace_name = getattr(self.tabular_editor, 'current_workspace', 'Unknown')
            
            # Get tables from the model
            tables = self.tabular_editor.list_tables()
            
            # Get roles from the model
            security_roles = []
            if hasattr(self.tabular_editor.model, 'Roles') and self.tabular_editor.model.Roles:
                for role in self.tabular_editor.model.Roles:
                    role_info = {
                        "role_name": role.Name,
                        "description": getattr(role, 'Description', ''),
                        "filters": []
                    }
                    
                    # Get table permissions for this role
                    if hasattr(role, 'TablePermissions'):
                        for table_perm in role.TablePermissions:
                            filter_info = {
                                "table": table_perm.Table.Name if hasattr(table_perm, 'Table') else 'Unknown',
                                "expression": getattr(table_perm, 'FilterExpression', 'None'),
                                "metadata_permission": str(getattr(table_perm, 'MetadataPermission', 'Default'))
                            }
                            role_info["filters"].append(filter_info)
                    
                    security_roles.append(role_info)
            
            # Build role-to-table map
            table_role_map = {t: [] for t in tables}
            for role in security_roles:
                for f in role.get("filters", []):
                    table = f.get("table")
                    if table in table_role_map:
                        table_role_map[table].append({
                            "role_name": role["role_name"],
                            "expression": f.get("expression", "None"),
                            "metadata_permission": f.get("metadata_permission", "Default")
                        })
            
            # Sensitivity rules (basic heuristic)
            sensitive_keywords = ["ssn", "salary", "email", "dob", "phone", "address", "personal", "confidential", "private"]
            
            # Prepare output
            analysis_results = []
            recommendations = []
            
            for table_name in tables:
                # Count records using DAX query
                try:
                    escaped_table = f"'{table_name}'" if ' ' in table_name else table_name
                    row_count_query = f'EVALUATE ROW("Count", COUNTROWS({escaped_table}))'
                    result = self.tabular_editor.execute_dax_query(row_count_query)
                    if result and len(result) > 0:
                        # DAX query returns list of dicts, get first value from first row
                        first_row = result[0]
                        count_value = list(first_row.values())[0] if first_row else None
                        row_count = count_value if count_value is not None else 0
                    else:
                        row_count = 0
                except Exception as e:
                    logger.warning(f"Could not get row count for table {table_name}: {e}")
                    row_count = "Unknown"
                
                # Check roles
                roles_applied = table_role_map.get(table_name, [])
                has_roles = bool(roles_applied)
                
                # Sensitivity detection
                sensitivity = "Normal"
                try:
                    columns = self.tabular_editor.list_table_columns(table_name)
                    for col in columns:
                        col_name = col if isinstance(col, str) else col.get('column_name', '')
                        if any(keyword in col_name.lower() for keyword in sensitive_keywords):
                            sensitivity = "Sensitive"
                            break
                except Exception as e:
                    logger.warning(f"Could not check columns for table {table_name}: {e}")
                
                # Generate table-specific recommendations and priority
                table_recommendations = []
                recommendation_priority = "NONE"
                
                if not has_roles and sensitivity == "Sensitive":
                    table_recommendations.append("CRITICAL: Implement RLS to protect sensitive data")
                    recommendation_priority = "HIGH"
                    recommendations.append({
                        "priority": "HIGH",
                        "category": "Missing RLS",
                        "table": table_name,
                        "description": f"Table '{table_name}' contains sensitive data but has no RLS.",
                        "action": "Define role-based access to protect sensitive information."
                    })
                elif not has_roles and row_count != "Unknown" and isinstance(row_count, (int, float)) and row_count > 1000:
                    table_recommendations.append(f"MEDIUM: Review RLS requirements for large table ({row_count:,} records)")
                    recommendation_priority = "MEDIUM"
                    recommendations.append({
                        "priority": "MEDIUM",
                        "category": "Missing RLS",
                        "table": table_name,
                        "description": f"Table '{table_name}' has {row_count:,} records but no RLS applied.",
                        "action": "Review if RLS is required for this large table."
                    })
                elif not has_roles and sensitivity == "Normal":
                    table_recommendations.append("LOW: Consider if RLS is needed for this table")
                    recommendation_priority = "LOW"
                    recommendations.append({
                        "priority": "LOW",
                        "category": "No RLS",
                        "table": table_name,
                        "description": f"Table '{table_name}' has no RLS applied.",
                        "action": "Review if RLS is required for this table."
                    })
                else:
                    # Table has RLS applied
                    if sensitivity == "Sensitive":
                        table_recommendations.append("GOOD: Sensitive data is protected with RLS")
                    else:
                        table_recommendations.append("OK: RLS implemented")
                    recommendation_priority = "NONE"
                
                # Add data quality recommendations
                if row_count == "Unknown":
                    table_recommendations.append("WARNING: Unable to verify record count")
                elif isinstance(row_count, (int, float)) and row_count == 0:
                    table_recommendations.append("INFO: Empty table - no data to protect")
                
                # Add role-specific recommendations
                if has_roles:
                    role_count = len(roles_applied)
                    if role_count == 1:
                        table_recommendations.append(f"INFO: Protected by {role_count} security role")
                    else:
                        table_recommendations.append(f"INFO: Protected by {role_count} security roles")
                
                # Format roles applied
                roles_list = [r["role_name"] for r in roles_applied] if roles_applied else ["None"]
                role_details = []
                role_explanations = []
                
                for role in roles_applied:
                    detail = f"{role['role_name']}: {role['expression']}"
                    if role['metadata_permission'] != "Default":
                        detail += f" (Metadata: {role['metadata_permission']})"
                    role_details.append(detail)
                    
                    # Generate natural language explanation for the role (integrated logic)
                    dax_expression = role['expression']
                    role_name = role['role_name']
                    
                    if not dax_expression or dax_expression.strip() in ["None", ""]:
                        explanation = "No filtering applied - all users can access all data"
                    else:
                        try:
                            # Clean up the expression for analysis
                            expression = dax_expression.strip()
                            expr_lower = expression.lower()
                            
                            # Check for USERPRINCIPALNAME (with or without space)
                            has_userprincipalname = "userprincipalname()" in expr_lower or "userprincipalname ()" in expr_lower
                            
                            # Pattern 1: VAR + SELECTCOLUMNS + FILTER + USERPRINCIPALNAME (most sophisticated)
                            if all(keyword in expr_lower for keyword in ["var ", "selectcolumns", "filter"]) and has_userprincipalname:
                                if "subsidiary" in expr_lower:
                                    explanation = "Dynamic subsidiary filtering: The system looks up each user's login credentials, finds which subsidiaries they are authorized to access from the security table, creates a personalized list, and shows only data for those specific subsidiaries"
                                elif "business" in expr_lower:
                                    explanation = "Dynamic business unit filtering: The system uses each user's login credentials to look up their authorized business units from the security table, creates a personalized access list, and displays only data for business units they can access"
                                elif "customer" in expr_lower:
                                    explanation = "Dynamic customer filtering: The system creates a personalized list of customers each user can access based on their login credentials and security permissions"
                                elif "geography" in expr_lower or "territory" in expr_lower:
                                    explanation = "Dynamic geographic filtering: The system determines which geographic regions or territories each user can access based on their credentials"
                                else:
                                    explanation = "Dynamic personalized filtering: The system creates a custom list of authorized data for each user based on their login credentials and security permissions"
                                
                                # Add ISBLANK explanation if present
                                if "isblank" in expr_lower:
                                    explanation += ". Records with missing values in the security column are also included to ensure complete data visibility"
                            
                            # Pattern 2: Simple User-based filtering with USERPRINCIPALNAME() (no VAR)
                            elif has_userprincipalname and "var " not in expr_lower:
                                if "subsidiary" in expr_lower:
                                    explanation = "User-based subsidiary filtering: Users can only see data for subsidiaries they are authorized to access based on their login credentials"
                                elif "business" in expr_lower:
                                    explanation = "User-based business filtering: Users can only see data for business units they are authorized to access based on their login credentials"
                                else:
                                    explanation = "User-based filtering: Data access is controlled based on each user's login credentials and their specific permissions"
                            
                            # Pattern 3: Role-based filtering with fixed values (no USERPRINCIPALNAME)
                            elif "=" in expression and " in " in expr_lower and not has_userprincipalname:
                                if "subsidiary" in expr_lower:
                                    explanation = f"Fixed subsidiary filtering: Users in the '{role_name}' role can only see data for predetermined subsidiaries assigned to this role"
                                elif "business" in expr_lower:
                                    explanation = f"Fixed business unit filtering: Users in the '{role_name}' role can only see data for predetermined business units assigned to this role"
                                elif "region" in expr_lower or "geography" in expr_lower:
                                    explanation = f"Fixed geographic filtering: Users in the '{role_name}' role can only see data for predetermined geographic regions assigned to this role"
                                else:
                                    explanation = f"Fixed data filtering: Users in the '{role_name}' role can only see data for predetermined values assigned to this role"
                            
                            # Pattern 4: Simple equality filters
                            elif "=" in expression and "var " not in expr_lower and not has_userprincipalname:
                                explanation = f"Simple filter: Users in the '{role_name}' role can only see records that match specific predefined criteria (fixed values)"
                            
                            # Pattern 5: Complex expressions without clear patterns
                            elif "filter" in expr_lower or "selectcolumns" in expr_lower:
                                explanation = f"Custom advanced filtering: Complex security logic controls what data users in the '{role_name}' role can access"
                            
                            # Default fallback
                            else:
                                explanation = f"Custom security rule: Data access is controlled by specific business logic defined for the '{role_name}' role"
                                
                        except Exception as e:
                            # Fallback explanation if parsing fails
                            explanation = f"Custom security filtering is applied for the '{role_name}' role (expression parsing failed)"
                    
                    role_explanations.append(f"{role['role_name']}: {explanation}")
                
                if not role_details:
                    role_details = ["No filters applied"]
                    role_explanations = ["No security filtering - all users can see all data in this table"]
                
                # Calculate risk level
                if sensitivity == "Sensitive" and not has_roles:
                    risk_level = "High"
                elif not has_roles and row_count != "Unknown" and isinstance(row_count, (int, float)) and row_count > 1000:
                    risk_level = "Medium"
                elif not has_roles and row_count != "Unknown" and isinstance(row_count, (int, float)) and row_count > 0:
                    risk_level = "Low"
                elif not has_roles:
                    risk_level = "Low"  # Empty table or unknown count
                else:
                    risk_level = "Low"  # Has RLS
                
                # Format row count for display
                display_row_count = f"{row_count:,}" if isinstance(row_count, (int, float)) else str(row_count)
                
                # Format recommendations for CSV
                recommendation_text = " | ".join(table_recommendations) if table_recommendations else "No specific recommendations"
                
                analysis_results.append({
                    "Table": table_name,
                    "Record_Count": display_row_count,
                    "Sensitivity": sensitivity,
                    "Roles_Applied": "; ".join(roles_list),
                    "Role_Details": "; ".join(role_details),
                    "Role_Logic_Explained": "; ".join(role_explanations),
                    "Has_RLS": "Yes" if has_roles else "No",
                    "Risk_Level": risk_level,
                    "Priority": recommendation_priority,
                    "Recommendations": recommendation_text
                })
            
            # Generate CSV
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                workspace_clean = workspace_name.replace(" ", "")
                dataset_clean = dataset_name.replace(" ", "")
                filename = f"Security_Role_Analysis_{workspace_clean}_{dataset_clean}_{timestamp}.csv"
                file_path = os.path.join("c:\\MCP-Ai", filename)
                
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                df = pd.DataFrame(analysis_results)
                df.to_csv(file_path, index=False, encoding="utf-8")
                
                csv_file_path = file_path
                logger.info(f"Security role analysis CSV saved: {csv_file_path}")
            except Exception as e:
                logger.warning(f"Failed to generate CSV file: {e}")
                csv_file_path = None
            
            # Generate summary statistics
            total_tables = len(tables)
            tables_with_rls = sum(1 for r in analysis_results if r["Has_RLS"] == "Yes")
            sensitive_tables = sum(1 for r in analysis_results if r["Sensitivity"] == "Sensitive")
            high_risk_tables = sum(1 for r in analysis_results if r["Risk_Level"] == "High")
            
            # Final Result
            result = {
                "analysis_summary": {
                    "dataset": dataset_name,
                    "workspace": workspace_name,
                    "total_tables": total_tables,
                    "total_roles": len(security_roles),
                    "tables_with_rls": tables_with_rls,
                    "sensitive_tables": sensitive_tables,
                    "high_risk_tables": high_risk_tables,
                    "rls_coverage_percentage": round((tables_with_rls / total_tables) * 100, 2) if total_tables > 0 else 0,
                    "analysis_timestamp": datetime.now().isoformat()
                },
                "table_analysis": analysis_results,
                "recommendations": recommendations,
                "security_roles": security_roles
            }
            
            if csv_file_path:
                result["csv_file_path"] = csv_file_path
            
            logger.info(f"Enhanced security role evaluation completed for dataset: {dataset_name}")
            return result
            
        except Exception as e:
            logger.error(f"Failed to evaluate security roles: {e}")
            raise Exception(f"Security role evaluation failed: {str(e)}")

    def evaluate_fact_and_dimension_analysis(self, tables_to_analyze: List[str] = None) -> Dict[str, Any]:
        """
        Comprehensive analysis to classify tables as fact or dimension tables.
        Combines the logic from both fact table and dimension table analysis for unified results.
        """
        try:
            if not self.tabular_editor.connected:
                raise Exception("Not connected to any Power BI dataset")
           
            # Get available tables using the same pattern as other working methods
            if tables_to_analyze:
                available_tables = self.tabular_editor.list_tables()
                tables = [table for table in available_tables if table in tables_to_analyze]
            else:
                tables = self.tabular_editor.list_tables()
           
            if not tables:
                raise Exception("No tables found to analyze")
           
            logger.info(f"Starting fact and dimension analysis for {len(tables)} tables")
           
            table_analysis = []
            fact_tables = []
            dimension_tables = []
            hybrid_tables = []
            unclear_tables = []
           
            quality_distribution = {"excellent_tables": 0, "good_tables": 0, "fair_tables": 0, "poor_tables": 0}
           
            for table_name in tables:
                logger.info(f"Analyzing table: {table_name}")
               
                # Initialize scores
                fact_score = 0.0
                dimension_score = 0.0
               
                # Get table information using tabular_editor methods
                try:
                    table_columns = self.tabular_editor.list_table_columns(table_name)
                except:
                    table_columns = []
               
                # Get table object directly for measures (following working pattern)
                table_obj = next((t for t in self.tabular_editor.model.Tables
                                if t.Name.lower() == table_name.lower()), None)
                if table_obj:
                    table_measures = [measure.Name for measure in table_obj.Measures] if hasattr(table_obj, 'Measures') else []
                else:
                    table_columns = []
                    table_measures = []
               
                column_count = len(table_columns)
                measure_count = len(table_measures)
               
                # 1. Column Analysis (40% weight)
                numeric_columns = 0
                date_columns = 0
                text_columns = 0
                foreign_key_patterns = 0
                primary_key_patterns = 0
               
                for column_name in table_columns:
                    column_name_lower = column_name.lower()
                   
                    # Basic type inference from column name patterns
                    if any(pattern in column_name_lower for pattern in ['amount', 'value', 'count', 'sum', 'total', 'quantity', 'number', 'rate', 'percentage']):
                        numeric_columns += 1
                    elif any(pattern in column_name_lower for pattern in ['date', 'time', 'created', 'modified', 'start', 'end']):
                        date_columns += 1
                    else:
                        text_columns += 1
                   
                    # Check for key patterns
                    if any(pattern in column_name_lower for pattern in ['id', 'key', '_id', '_key']):
                        if column_name_lower.endswith('id') or column_name_lower.endswith('key'):
                            primary_key_patterns += 1
                        else:
                            foreign_key_patterns += 1
               
                # 2. Measure Analysis (30% weight)
                aggregation_measures = 0
                calculated_measures = 0
               
                for measure_name in table_measures:
                    # Since we only have measure names, we'll use naming patterns for classification
                    measure_lower = measure_name.lower()
                   
                    # Check for aggregation patterns in name
                    if any(func in measure_lower for func in ['sum', 'count', 'average', 'avg', 'min', 'max', 'total']):
                        aggregation_measures += 1
                   
                    # Check for calculation patterns in name
                    if any(calc in measure_lower for calc in ['calculate', 'ratio', 'percentage', 'rate', 'growth', 'variance']):
                        calculated_measures += 1
               
                # 3. Naming Pattern Analysis (20% weight)
                table_name_lower = table_name.lower()
                fact_indicators = ['fact', 'sales', 'order', 'transaction', 'event', 'log', 'activity']
                dim_indicators = ['dim', 'dimension', 'customer', 'product', 'employee', 'location', 'category', 'type']
               
                fact_name_score = sum(1 for indicator in fact_indicators if indicator in table_name_lower)
                dim_name_score = sum(1 for indicator in dim_indicators if indicator in table_name_lower)
               
                # 4. Relationship Analysis (10% weight)
                relationships_out = 0  # This table references others (fact pattern)
                relationships_in = 0   # Others reference this table (dimension pattern)
               
                # Note: Simplified relationship analysis as full relationship inspection requires more complex logic
               
                # Calculate Fact Score
                # Higher numeric columns, more measures, fewer text columns = higher fact score
                if column_count > 0:
                    fact_score += (numeric_columns / column_count) * 0.3  # 30% from numeric ratio
                    fact_score += (measure_count / max(column_count, 1)) * 0.25  # 25% from measure density
                    fact_score += min(foreign_key_patterns / max(column_count, 1), 0.5) * 0.15  # 15% from FK presence
                    fact_score += min(fact_name_score / len(fact_indicators), 1.0) * 0.2  # 20% from naming
                    fact_score += min(date_columns / max(column_count, 1), 0.3) * 0.1  # 10% from date columns
               
                # Calculate Dimension Score
                # Higher text columns, fewer measures, more descriptive attributes = higher dimension score
                if column_count > 0:
                    dimension_score += (text_columns / column_count) * 0.3  # 30% from text ratio
                    dimension_score += max(0, (1 - measure_count / max(column_count, 1))) * 0.25  # 25% from low measure density
                    dimension_score += min(primary_key_patterns / max(column_count, 1), 0.5) * 0.15  # 15% from PK presence
                    dimension_score += min(dim_name_score / len(dim_indicators), 1.0) * 0.2  # 20% from naming
                    dimension_score += min((column_count - numeric_columns) / max(column_count, 1), 0.8) * 0.1  # 10% from descriptive columns
               
                # Normalize scores
                fact_score = min(fact_score, 1.0)
                dimension_score = min(dimension_score, 1.0)
               
                # Determine classification
                score_difference = abs(fact_score - dimension_score)
                confidence = max(fact_score, dimension_score)
               
                if fact_score > dimension_score and fact_score > 0.6:
                    primary_type = "Fact Table"
                    classification = "Fact" if score_difference > 0.2 else "Hybrid (Fact-leaning)"
                elif dimension_score > fact_score and dimension_score > 0.6:
                    primary_type = "Dimension Table"
                    classification = "Dimension" if score_difference > 0.2 else "Hybrid (Dimension-leaning)"
                elif score_difference < 0.1:
                    primary_type = "Hybrid Table"
                    classification = "Hybrid"
                else:
                    primary_type = "Unclear"
                    classification = "Unclear"
               
                # Quality assessment
                quality_score = confidence
                if quality_score >= 0.8:
                    quality_level = "Excellent"
                    quality_icon = "✅"
                    quality_distribution["excellent_tables"] += 1
                elif quality_score >= 0.6:
                    quality_level = "Good"
                    quality_icon = "⚠️"
                    quality_distribution["good_tables"] += 1
                elif quality_score >= 0.4:
                    quality_level = "Fair"
                    quality_icon = "🔶"
                    quality_distribution["fair_tables"] += 1
                else:
                    quality_level = "Poor"
                    quality_icon = "🔴"
                    quality_distribution["poor_tables"] += 1
               
                # Categorize tables
                if "Fact" in classification:
                    fact_tables.append(table_name)
                elif "Dimension" in classification:
                    dimension_tables.append(table_name)
                elif "Hybrid" in classification:
                    hybrid_tables.append(table_name)
                else:
                    unclear_tables.append(table_name)
               
                # Generate recommendations
                recommendations = []
                if fact_score > 0.5 and measure_count == 0:
                    recommendations.append("Consider adding measures for aggregations")
                if dimension_score > 0.5 and primary_key_patterns == 0:
                    recommendations.append("Consider adding a primary key column")
                if quality_score < 0.6:
                    recommendations.append("Table structure could be optimized for its intended purpose")
               
                table_analysis.append({
                    "table_name": table_name,
                    "classification": classification,
                    "primary_type": primary_type,
                    "fact_score": round(fact_score, 3),
                    "dimension_score": round(dimension_score, 3),
                    "confidence": round(confidence, 3),
                    "quality_level": quality_level,
                    "quality_icon": quality_icon,
                    "column_count": column_count,
                    "measure_count": measure_count,
                    "numeric_columns": numeric_columns,
                    "text_columns": text_columns,
                    "date_columns": date_columns,
                    "foreign_key_patterns": foreign_key_patterns,
                    "primary_key_patterns": primary_key_patterns,
                    "recommendations": recommendations
                })
           
            # Generate CSV file
            csv_file_path = None
            try:
                # Get workspace and dataset names for CSV filename
                workspace_name = getattr(self.tabular_editor, 'workspace_name', 'Unknown')
                dataset_name = getattr(self.tabular_editor, 'dataset_name', 'Unknown')
                
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_filename = f"Fact_Dimension_Analysis_{workspace_name}_{dataset_name}_{timestamp}.csv"
                csv_file_path = os.path.join(os.getcwd(), csv_filename)
               
                with open(csv_file_path, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                   
                    # Write header
                    writer.writerow([
                        'Table_Name', 'Classification', 'Primary_Type', 'Fact_Score', 'Dimension_Score',
                        'Confidence', 'Quality_Level', 'Column_Count', 'Measure_Count', 'Numeric_Columns',
                        'Text_Columns', 'Date_Columns', 'FK_Patterns', 'PK_Patterns', 'Recommendations'
                    ])
                   
                    # Write data
                    for table in table_analysis:
                        writer.writerow([
                            table['table_name'], table['classification'], table['primary_type'],
                            table['fact_score'], table['dimension_score'], table['confidence'],
                            table['quality_level'], table['column_count'], table['measure_count'],
                            table['numeric_columns'], table['text_columns'], table['date_columns'],
                            table['foreign_key_patterns'], table['primary_key_patterns'],
                            '; '.join(table['recommendations'])
                        ])
               
                logger.info(f"CSV file generated: {csv_file_path}")
               
            except Exception as csv_error:
                logger.error(f"Failed to generate CSV file: {csv_error}")
                csv_file_path = None
           
            # Prepare final result
            result = {
                "analysis_summary": {
                    "total_tables_analyzed": len(tables),
                    "table_classification_distribution": {
                        "fact_tables": len(fact_tables),
                        "dimension_tables": len(dimension_tables),
                        "hybrid_tables": len(hybrid_tables),
                        "unclear_tables": len(unclear_tables)
                    },
                    "quality_distribution": quality_distribution,
                    "analysis_timestamp": datetime.now().isoformat()
                },
                "table_analysis": table_analysis,
                "categorized_tables": {
                    "fact_tables": fact_tables,
                    "dimension_tables": dimension_tables,
                    "hybrid_tables": hybrid_tables,
                    "unclear_tables": unclear_tables
                }
            }
           
            # Add CSV file path
            if csv_file_path:
                result["csv_file_path"] = csv_file_path
            else:
                result["csv_generation_error"] = "Failed to generate CSV file - check logs for details"
           
            logger.info(f"Fact and dimension analysis completed. Analyzed {len(tables)} tables")
            return result
           
        except Exception as e:
            logger.error(f"Failed to evaluate fact and dimension analysis: {e}")
            raise Exception(f"Failed to evaluate fact and dimension analysis: {e}")

    def evaluate_table_linking(self, tables_to_analyze: List[str] = None) -> Dict[str, Any]:
        """
        Comprehensive table linking evaluation with enhanced cardinality detection and SMART business-focused relationship suggestions.
        """
        if not self.tabular_editor.connected:
            raise Exception("Tabular server is not connected. Please connect to a dataset first.")
       
        def _detect_actual_cardinality(from_table: str, from_column: str, to_table: str, to_column: str) -> str:
            """Detect actual cardinality of relationship using DAX queries"""
            try:
                # Query to check cardinality patterns
                dax_query = f"""
                EVALUATE
                SUMMARIZECOLUMNS(
                    "Analysis", "Cardinality_Check",
                    "From_Distinct", DISTINCTCOUNT({from_table}[{from_column}]),
                    "To_Distinct", DISTINCTCOUNT({to_table}[{to_column}]),
                    "From_Count", COUNTROWS({from_table}),
                    "To_Count", COUNTROWS({to_table})
                )
                """
                result = self.tabular_editor.execute_dax_query(dax_query)
                
                if result and len(result) > 0:
                    data = result[0]
                    from_distinct = data.get('[From_Distinct]', 0)
                    to_distinct = data.get('[To_Distinct]', 0)
                    from_count = data.get('[From_Count]', 0)
                    to_count = data.get('[To_Count]', 0)
                    
                    # Determine cardinality based on patterns
                    if from_count == from_distinct and to_count == to_distinct:
                        return "one_to_one"
                    elif from_count > from_distinct and to_count == to_distinct:
                        return "many_to_one"
                    elif from_count == from_distinct and to_count > to_distinct:
                        return "one_to_many"
                    else:
                        return "many_to_many"
                
                return "unknown"
                
            except Exception as e:
                logger.warning(f"Could not detect cardinality for {from_table}.{from_column} → {to_table}.{to_column}: {e}")
                return "unknown"

        def _get_workspace_dataset_info() -> Dict[str, str]:
            """Get workspace and dataset information"""
            return {
                "WorkspaceID": getattr(self.tabular_editor, 'current_workspace_id', 'Unknown'),
                "WorkspaceName": getattr(self.tabular_editor, 'current_workspace', 'Unknown'),
                "DatasetID": getattr(self.tabular_editor, 'current_database_id', 'Unknown'),
                "DatasetName": getattr(self.tabular_editor, 'current_database', 'Unknown')
            }

        try:
            logger.info("Starting SMART business-focused table linking evaluation...")
           
            # Get tables to analyze
            if tables_to_analyze:
                available_tables = self.tabular_editor.list_tables()
                invalid_tables = [t for t in tables_to_analyze if t not in available_tables]
                if invalid_tables:
                    raise Exception(f"Tables not found: {invalid_tables}")
                tables = tables_to_analyze
            else:
                tables = self.tabular_editor.list_tables()
           
            # Get current relationships
            current_relationships = self.tabular_editor.list_all_relationships()
            existing_rels = current_relationships.get("relationships", [])
           
            # RELATIONSHIP CLASSIFICATION WITH CARDINALITY DETECTION
            relationship_classification = {
                "total_relationships": len(existing_rels),
                "active_relationships": 0,
                "inactive_relationships": 0,
                "by_cardinality": {"one_to_many": 0, "many_to_one": 0, "one_to_one": 0, "many_to_many": 0, "unknown": 0},
                "by_cross_filter": {"single": 0, "both": 0},
                "detailed_relationships": []
            }
           
            # ANALYZE EXISTING RELATIONSHIPS WITH CARDINALITY DETECTION
            for i, rel in enumerate(existing_rels):
                try:
                    detected_cardinality = _detect_actual_cardinality(
                        rel["from_table"], rel["from_column"],
                        rel["to_table"], rel["to_column"]
                    )
                   
                    relationship_classification["by_cardinality"][detected_cardinality] += 1
                   
                    if rel.get("is_active", True):
                        relationship_classification["active_relationships"] += 1
                    else:
                        relationship_classification["inactive_relationships"] += 1
                   
                    cross_filter = rel.get("cross_filter_direction", "single").lower()
                    if cross_filter in relationship_classification["by_cross_filter"]:
                        relationship_classification["by_cross_filter"][cross_filter] += 1
                   
                    relationship_classification["detailed_relationships"].append({
                        "from_table": rel["from_table"],
                        "from_column": rel["from_column"],
                        "to_table": rel["to_table"],
                        "to_column": rel["to_column"],
                        "cardinality": detected_cardinality,
                        "is_active": rel.get("is_active", True),
                        "cross_filter_direction": rel.get("cross_filter_direction", "single")
                    })
                   
                except Exception as e:
                    logger.warning(f"Could not analyze relationship {i+1}: {e}")
                    relationship_classification["by_cardinality"]["unknown"] += 1
           
            # SMART BUSINESS-FOCUSED RELATIONSHIP ANALYSIS
            existing_pairs = set()
            for rel in existing_rels:
                existing_pairs.add((rel["from_table"], rel["to_table"]))
                existing_pairs.add((rel["to_table"], rel["from_table"]))
           
            # Categorize tables
            fact_tables = [t for t in tables if t.lower().startswith('fact')]
            dim_tables = [t for t in tables if t.lower().startswith('dim')]
            date_tables = [t for t in dim_tables if 'date' in t.lower() or 'time' in t.lower()]
            security_tables = [t for t in tables if 'sec_' in t.lower() or 'security' in t.lower()]
           
            logger.info(f"Table categorization: {len(fact_tables)} fact, {len(dim_tables)} dimension, {len(date_tables)} date, {len(security_tables)} security")
           
            # HELPER FUNCTION TO GET ACTUAL COLUMN NAMES
            def get_actual_columns(table_name):
                """Get actual column names from table using DAX query"""
                try:
                    dax_query = f"EVALUATE TOPN(1, {table_name})"
                    result = self.tabular_editor.execute_dax_query(dax_query)
                    if result and len(result) > 0:
                        return list(result[0].keys())
                    return []
                except:
                    return []
           
            # SMART POTENTIAL RELATIONSHIPS ANALYSIS
            potential_relationships = []
           
            # 1. CRITICAL DATE RELATIONSHIPS - Only if actual matching columns exist
            logger.info("Analyzing date relationships with actual column verification...")
            for date_table in date_tables:
                date_columns = get_actual_columns(date_table)
                date_join_columns = [col for col in date_columns if 'fiscalmonthid' in col.lower() or 'dateid' in col.lower()]
               
                for fact_table in fact_tables:
                    if (fact_table, date_table) not in existing_pairs:
                        fact_columns = get_actual_columns(fact_table)
                       
                        # Look for matching fiscal month ID columns
                        fact_fiscal_columns = [col for col in fact_columns if 'fiscalmonthid' in col.lower()]
                        date_fiscal_columns = [col for col in date_columns if 'fiscalmonthid' in col.lower()]
                       
                        if fact_fiscal_columns and date_fiscal_columns:
                            potential_relationships.append({
                                "from_table": fact_table,
                                "from_column": fact_fiscal_columns[0],  # Actual column name
                                "to_table": date_table,
                                "to_column": date_fiscal_columns[0],    # Actual column name
                                "suggested_cardinality": "many_to_one",
                                "confidence_score": 0.95,
                                "reason": f"Critical date relationship: {fact_table}.{fact_fiscal_columns[0]} → {date_table}.{date_fiscal_columns[0]}",
                                "business_justification": "Essential for time intelligence calculations and fiscal period analysis"
                            })
           
            # 2. CRITICAL SECURITY RELATIONSHIPS - Only if actual matching columns exist
            logger.info("Analyzing security relationships with actual column verification...")
            for sec_table in security_tables:
                sec_columns = get_actual_columns(sec_table)
               
                # Look for business hierarchy key columns in security table
                sec_business_keys = [col for col in sec_columns if 'dimbusinesshierarchykey' in col.lower()]
               
                if sec_business_keys:
                    # Find business hierarchy dimension
                    business_dims = [t for t in dim_tables if 'business' in t.lower() and 'hierarchy' in t.lower()]
                   
                    for business_dim in business_dims:
                        if (sec_table, business_dim) not in existing_pairs:
                            business_columns = get_actual_columns(business_dim)
                            business_keys = [col for col in business_columns if 'dimbusinesshierarchykey' in col.lower()]
                           
                            if business_keys:
                                potential_relationships.append({
                                    "from_table": sec_table,
                                    "from_column": sec_business_keys[0],  # Actual column name
                                    "to_table": business_dim,
                                    "to_column": business_keys[0],        # Actual column name
                                    "suggested_cardinality": "many_to_one",
                                    "confidence_score": 0.90,
                                    "reason": f"Critical security relationship: {sec_table}.{sec_business_keys[0]} → {business_dim}.{business_keys[0]}",
                                    "business_justification": "Essential for row-level security implementation and user-based data access controls"
                                })
           
            # 3. MISSING FACT-TO-DIMENSION RELATIONSHIPS - Only verify actual columns
            logger.info("Analyzing missing fact-to-dimension relationships...")
            for fact_table in fact_tables:
                fact_columns = get_actual_columns(fact_table)
                fact_connections = set()
               
                for rel in existing_rels:
                    if rel["from_table"] == fact_table:
                        fact_connections.add(rel["to_table"])
               
                # Check for missing product hierarchy connections
                product_dims = [t for t in dim_tables if 'product' in t.lower() and 'hierarchy' in t.lower()]
                for product_dim in product_dims:
                    if product_dim not in fact_connections:
                        product_columns = get_actual_columns(product_dim)
                       
                        fact_product_keys = [col for col in fact_columns if 'dimproducthierarchykey' in col.lower()]
                        dim_product_keys = [col for col in product_columns if 'dimproducthierarchykey' in col.lower()]
                       
                        if fact_product_keys and dim_product_keys:
                            potential_relationships.append({
                                "from_table": fact_table,
                                "from_column": fact_product_keys[0],
                                "to_table": product_dim,
                                "to_column": dim_product_keys[0],
                                "suggested_cardinality": "many_to_one",
                                "confidence_score": 0.85,
                                "reason": f"Missing product dimension: {fact_table}.{fact_product_keys[0]} → {product_dim}.{dim_product_keys[0]}",
                                "business_justification": "Essential for product-based analysis and reporting"
                            })
           
            # GENERATE SMART RECOMMENDATIONS
            recommendations = []
           
            # Find isolated tables
            connected_tables = set()
            for rel in existing_rels:
                connected_tables.add(rel["from_table"])
                connected_tables.add(rel["to_table"])
           
            isolated_tables = [table for table in tables if table not in connected_tables]
           
            if isolated_tables:
                isolated_facts = [t for t in isolated_tables if t.lower().startswith('fact')]
                isolated_dims = [t for t in isolated_tables if t.lower().startswith('dim')]
                isolated_security = [t for t in isolated_tables if 'sec_' in t.lower()]
               
                if isolated_facts:
                    recommendations.append({
                        "priority": "CRITICAL",
                        "category": "Isolated Fact Tables",
                        "description": f"Found {len(isolated_facts)} fact tables without relationships - this prevents all analysis",
                        "action": "URGENT: Connect fact tables to dimension tables immediately",
                        "affected_tables": isolated_facts,
                        "impact": "No data analysis possible until fact tables are connected"
                    })
               
                if isolated_security:
                    recommendations.append({
                        "priority": "HIGH",
                        "category": "Isolated Security Tables",
                        "description": f"Found {len(isolated_security)} security tables without relationships",
                        "action": "Connect security tables to business dimensions for RLS implementation",
                        "affected_tables": isolated_security,
                        "impact": "Row-level security cannot function without these connections"
                    })
               
                if isolated_dims:
                    recommendations.append({
                        "priority": "MEDIUM",
                        "category": "Isolated Dimension Tables",
                        "description": f"Found {len(isolated_dims)} dimension tables without relationships",
                        "action": "Review if these dimensions should be connected to fact tables",
                        "affected_tables": isolated_dims,
                        "impact": "These dimensions cannot be used for filtering or analysis"
                    })
           
            # Critical missing relationships
            critical_missing = [rel for rel in potential_relationships if rel["confidence_score"] >= 0.90]
            if critical_missing:
                recommendations.append({
                    "priority": "CRITICAL",
                    "category": "Missing Critical Relationships",
                    "description": f"Found {len(critical_missing)} critical missing relationships with verified column matches",
                    "action": "Implement these relationships immediately - columns verified and ready",
                    "affected_tables": [f"{rel['from_table']}.{rel['from_column']} → {rel['to_table']}.{rel['to_column']}" for rel in critical_missing],
                    "impact": "Essential business functionality missing without these relationships"
                })
           
            # GENERATE CSV FILE WITH SMART ANALYSIS
            logger.info("Generating CSV file with SMART relationship analysis...")
            csv_file_path = None
            try:
                import pandas as pd
                import os
                from datetime import datetime
               
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                workspace_name = getattr(self.tabular_editor, 'current_workspace', 'Unknown')
                dataset_name = getattr(self.tabular_editor, 'current_database', 'Unknown')
               
                filename = f"SMART_Relationship_Analysis_{workspace_name}_{dataset_name}_{timestamp}.csv"
                file_path = os.path.join("c:\\MCP-Ai", filename)
               
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                workspace_dataset_info = _get_workspace_dataset_info()
               
                relationship_data = []
               
                # Add existing relationships
                for rel in relationship_classification["detailed_relationships"]:
                    relationship_data.append({
                        "WorkspaceID": workspace_dataset_info["WorkspaceID"],
                        "WorkspaceName": workspace_dataset_info["WorkspaceName"],
                        "DatasetID": workspace_dataset_info["DatasetID"],
                        "DatasetName": workspace_dataset_info["DatasetName"],
                        "Relationship_Type": "Existing",
                        "From_Table": rel["from_table"],
                        "From_Column": rel["from_column"],
                        "To_Table": rel["to_table"],
                        "To_Column": rel["to_column"],
                        "Cardinality": rel["cardinality"],
                        "Is_Active": "Yes" if rel["is_active"] else "No",
                        "Cross_Filter_Direction": rel["cross_filter_direction"],
                        "Confidence_Score": "1.00",
                        "Reason": "Existing relationship in model",
                        "Business_Justification": "Already implemented",
                        "Priority": "Existing",
                        "Status": "✅ Active" if rel["is_active"] else "⚠️ Inactive",
                        "Column_Verified": "Yes",
                        "Analysis_Timestamp": datetime.now().isoformat()
                    })
               
                # Add SMART potential relationships with verified columns
                for rel in potential_relationships:
                    priority = "Critical" if rel["confidence_score"] >= 0.90 else "High" if rel["confidence_score"] >= 0.85 else "Medium"
                    status_icon = "🔴" if rel["confidence_score"] >= 0.90 else "⚠️"
                   
                    relationship_data.append({
                        "WorkspaceID": workspace_dataset_info["WorkspaceID"],
                        "WorkspaceName": workspace_dataset_info["WorkspaceName"],
                        "DatasetID": workspace_dataset_info["DatasetID"],
                        "DatasetName": workspace_dataset_info["DatasetName"],
                        "Relationship_Type": "SMART_Suggested",
                        "From_Table": rel["from_table"],
                        "From_Column": rel["from_column"],
                        "To_Table": rel["to_table"],
                        "To_Column": rel["to_column"],
                        "Cardinality": rel["suggested_cardinality"],
                        "Is_Active": "N/A",
                        "Cross_Filter_Direction": "Single",
                        "Confidence_Score": f"{rel['confidence_score']:.2f}",
                        "Reason": rel["reason"],
                        "Business_Justification": rel["business_justification"],
                        "Priority": priority,
                        "Status": f"{status_icon} Missing - {priority} Priority",
                        "Column_Verified": "Yes - Columns Exist",
                        "Analysis_Timestamp": datetime.now().isoformat()
                    })
               
                if relationship_data:
                    df = pd.DataFrame(relationship_data)
                    df.to_csv(file_path, index=False, encoding='utf-8-sig')
                    csv_file_path = file_path
                    logger.info(f"SMART CSV file successfully generated: {csv_file_path}")
               
            except Exception as e:
                logger.warning(f"Failed to generate CSV file: {e}")
           
            # COMPILE FINAL RESULT
            result = {
                "analysis_summary": {
                    "analysis_type": "SMART Business-Focused",
                    "tables_analyzed": len(tables),
                    "existing_relationships": len(existing_rels),
                    "smart_potential_relationships": len(potential_relationships),
                    "isolated_tables": len(isolated_tables),
                    "table_categorization": {
                        "fact_tables": len(fact_tables),
                        "dimension_tables": len(dim_tables),
                        "date_tables": len(date_tables),
                        "security_tables": len(security_tables)
                    },
                    "analysis_timestamp": datetime.now().isoformat()
                },
                "existing_relationships": relationship_classification,
                "smart_potential_relationships": potential_relationships,
                "recommendations": recommendations,
                "smart_features": [
                    "✅ Verified actual column names exist",
                    "✅ Only business-critical relationships suggested",
                    "✅ No generic pattern matching",
                    "✅ Confidence scores based on column verification",
                    "❌ Eliminated speculative relationships"
                ]
            }
           
            if csv_file_path:
                result["csv_file_path"] = csv_file_path
                result["csv_generation_status"] = "Success"
            else:
                result["csv_generation_error"] = "Failed to generate CSV file"
                result["csv_generation_status"] = "Failed"
           
            logger.info(f"SMART table linking evaluation completed. Found {len(existing_rels)} existing relationships and {len(potential_relationships)} verified business relationships")
            return result
           
        except Exception as e:
            logger.error(f"Failed to evaluate SMART table linking: {e}")
            raise Exception(f"Failed to evaluate SMART table linking: {e}")

    def evaluate_table_hierarchies(self, tables_to_analyze: List[str] = None) -> Dict[str, Any]:
        """
        Comprehensive hierarchy evaluation combining existing and suggested hierarchies in one analysis.
        Integrates both list_existing_hierarchies and hierarchy detection functionality.
        
        Features:
        - Detects existing Power BI hierarchies with detailed level information
        - Analyzes schema for potential new hierarchies based on column patterns
        - Identifies logical groupings and parent-child relationships
        - Evaluates common dimension patterns (Time, Geography, Product, Organization)
        - Generates unified CSV differentiating existing vs suggested hierarchies
        - Provides detailed level structure for existing hierarchies

        Args:
            tables_to_analyze: Optional list of specific tables to analyze. If None, analyzes all tables.

        Returns:
            Dictionary containing comprehensive hierarchy analysis, recommendations, and CSV file path
        """
        if not self.tabular_editor.connected:
            raise Exception("Tabular server is not connected. Please connect to a dataset first.")

        try:
            logger.info("Starting comprehensive hierarchy evaluation (existing + suggested)...")
            
            def _assess_hierarchy_quality(table: str, matching_columns: Dict, sample_data: Dict) -> float:
                """
                Assess the quality of a potential hierarchy based on data patterns.
                Returns a quality score between 0.0 and 1.0.
                """
                try:
                    quality_score = 0.5  # Base score
                    
                    # Check for data consistency
                    if matching_columns and sample_data:
                        # Add quality based on number of levels
                        level_count = len(matching_columns)
                        if level_count >= 3:
                            quality_score += 0.2
                        elif level_count >= 2:
                            quality_score += 0.1
                        
                        # Check for data completeness (non-null values)
                        non_null_ratio = 0.8  # Assume good data quality for now
                        quality_score += (non_null_ratio - 0.5) * 0.3
                    
                    return min(1.0, quality_score)
                except Exception as e:
                    logger.warning(f"Error assessing hierarchy quality: {e}")
                    return 0.5
            
            # Initialize debug tracking
            debug_info = {"function_started": True}

            # Get all tables to analyze
            available_tables = self.tabular_editor.list_tables()
            if tables_to_analyze:
                invalid_tables = [t for t in tables_to_analyze if t not in available_tables]
                if invalid_tables:
                    raise Exception(f"Tables not found: {invalid_tables}")
                tables = tables_to_analyze
            else:
                tables = available_tables

            logger.info(f"Analyzing {len(tables)} tables for existing and potential hierarchies...")

            # Enhanced hierarchy patterns with extensive pattern matching
            predefined_hierarchies = {
                "time": {
                    "levels": ["Fiscal Year", "Fiscal Semester", "Fiscal Quarter", "Fiscal Month", "Date"],
                    "patterns": ["year", "quarter", "month", "day", "date", "fiscal", "calendar", "semester", "period"],
                    "priority": "CRITICAL",
                    "description": "Time intelligence hierarchy for temporal analysis"
                },
                "geography": {
                    "levels": ["Big Area", "Area", "Region", "Sub Region", "Subsidiary", "Country", "State", "City"],
                    "patterns": ["country", "region", "state", "province", "city", "location", "territory", "area", "subsidiary", "big area", "field", "geography"],
                    "priority": "HIGH",
                    "description": "Geographic hierarchy for location-based analysis"
                },
                "product": {
                    "levels": ["Super Rev Sum Division", "Rev Sum Division", "Solution Area", "Product Family", "Product Unit"],
                    "patterns": ["category", "subcategory", "product", "item", "brand", "line", "division", "family", "solution", "rev sum", "super rev", "commercial workload", "pipeline grouping"],
                    "priority": "HIGH",
                    "description": "Product classification hierarchy for revenue analysis"
                },
                "sales_organization": {
                    "levels": ["Sales Group", "Sales Team", "Sales Territory", "ATU Group", "ATU"],
                    "patterns": ["sales", "group", "team", "territory", "atu", "org", "organization"],
                    "priority": "HIGH",
                    "description": "Sales organization hierarchy for performance tracking"
                },
                "seller_organization": {
                    "levels": ["Org", "Role Summary", "Role", "Standard Title"],
                    "patterns": ["org", "organization", "role", "title", "seller", "employee", "manager"],
                    "priority": "MEDIUM",
                    "description": "Seller organizational hierarchy for team analysis"
                },
                "industry": {
                    "levels": ["Industry", "Vertical Category", "Vertical", "Sub Vertical"],
                    "patterns": ["industry", "vertical", "category", "sector", "market"],
                    "priority": "MEDIUM",
                    "description": "Industry classification hierarchy for market analysis"
                },
                "customer": {
                    "levels": ["Segment", "SubSegment", "Customer", "Account"],
                    "patterns": ["segment", "subsegment", "customer", "account", "client", "contact"],
                    "priority": "MEDIUM",
                    "description": "Customer segmentation hierarchy"
                },
                "support": {
                    "levels": ["Support Model", "Current Support Type", "Active Support Flag"],
                    "patterns": ["support", "service", "model", "type", "flag"],
                    "priority": "LOW",
                    "description": "Support service hierarchy"
                }
            }

            # Initialize analysis results
            hierarchy_analysis = []
            recommendations = []
            existing_hierarchies_detailed = []
            
            # Step 1: Get detailed existing hierarchies using direct DAX query (simplified)
            logger.info("Step 1: Fetching detailed existing Power BI hierarchies...")
            try:
                debug_info["step1_started"] = True
                
                # Use the same simple DAX query that works in list_existing_hierarchies
                hierarchies_query = "EVALUATE INFO.HIERARCHIES()"
                hierarchies_result = self.tabular_editor.execute_dax_query(hierarchies_query)
                if hierarchies_result and len(hierarchies_result) > 0:
                    logger.info(f"Found {len(hierarchies_result)} existing hierarchies via DAX query")
                    debug_info["dax_query_success"] = True
                    debug_info["hierarchy_result_count"] = len(hierarchies_result)
                    debug_info["method_call_success"] = True
                    
                    # First get table mapping
                    tables_query = "EVALUATE INFO.TABLES()"
                    tables_result = self.tabular_editor.execute_dax_query(tables_query)
                    table_id_to_name = {}
                    if tables_result:
                        for table_row in tables_result:
                            table_id = table_row.get('[ID]')
                            table_name = table_row.get('[Name]')
                            if table_id and table_name:
                                table_id_to_name[table_id] = table_name
                    
                    # Get hierarchy level counts
                    levels_query = "EVALUATE INFO.LEVELS()"
                    levels_result = self.tabular_editor.execute_dax_query(levels_query)
                    hierarchy_level_counts = {}
                    if levels_result:
                        for level_row in levels_result:
                            hierarchy_id = level_row.get('[HierarchyID]')
                            if hierarchy_id not in hierarchy_level_counts:
                                hierarchy_level_counts[hierarchy_id] = 0
                            hierarchy_level_counts[hierarchy_id] += 1
                    
                    for hierarchy_row in hierarchies_result:
                        # Extract basic hierarchy information using actual DAX column names
                        hierarchy_id = hierarchy_row.get('[ID]')
                        table_id = hierarchy_row.get('[TableID]')
                        table_name = table_id_to_name.get(table_id, f'TableID_{table_id}')
                        hierarchy_name = str(hierarchy_row.get('[Name]', 'Unknown'))
                        level_count = hierarchy_level_counts.get(hierarchy_id, 0)
                        description = hierarchy_row.get('[Description]', '')
                        
                        # Build basic hierarchy info
                        existing_hierarchies_detailed.append({
                            "table": table_name,
                            "hierarchy_name": hierarchy_name,
                            "level_count": level_count,
                            "levels": [],  # Could be populated with detailed level info if needed
                            "level_path": f"{hierarchy_name} ({level_count} levels)",
                            "column_path": f"{hierarchy_name} with {level_count} levels",
                            "status": "EXISTING_IN_MODEL", 
                            "hierarchy_type": "Power BI Hierarchy",
                            "description": description or f"Existing hierarchy in {table_name}"
                        })
                    
                    debug_info["processed_count"] = len(existing_hierarchies_detailed)
                    logger.info(f"Successfully processed {len(existing_hierarchies_detailed)} existing hierarchies")
                else:
                    debug_info["no_hierarchies_reason"] = "No hierarchies returned from DAX query"
                    debug_info["method_call_success"] = False
                    logger.info("No existing hierarchies found via DAX query")
                    
            except Exception as e:
                debug_info["method_call_success"] = False
                debug_info["error"] = str(e)
                logger.warning(f"Could not retrieve existing hierarchies via DAX query: {e}")
                existing_hierarchies_detailed = []

            # Step 2: Analyze each table for potential hierarchies and combine with existing
            logger.info("Step 2: Analyzing table schemas for hierarchy opportunities...")
            
            for table in tables:
                try:
                    # Get columns and sample data for analysis
                    dax_query = f"EVALUATE TOPN(5, '{table}')"
                    result = self.tabular_editor.execute_dax_query(dax_query)
                    
                    if result and len(result) > 0:
                        # Extract column information
                        columns = list(result[0].keys())
                        clean_columns = [col.replace(f"{table}[", "").replace("]", "") for col in columns]
                        col_names_lower = [c.lower() for c in clean_columns]
                        
                        # Get sample values for better analysis
                        sample_data = {col: [row.get(col) for row in result[:3]] for col in columns}
                        
                        logger.info(f"Analyzing table '{table}' with {len(clean_columns)} columns")
                    else:
                        logger.warning(f"No data returned for table {table}")
                        hierarchy_analysis.append({
                            "table": table,
                            "error": "No data returned from table",
                            "detected_hierarchies": [],
                            "potential_hierarchies": [],
                            "missing_levels": [],
                            "existing_hierarchies": []
                        })
                        continue
                    
                    # Check if this table has existing hierarchies
                    table_existing_hierarchies = [h for h in existing_hierarchies_detailed if h["table"] == table]
                    
                    detected_hierarchies = []
                    potential_hierarchies = []
                    missing_levels = []

                    # Enhanced hierarchy detection with pattern matching and data analysis
                    for hierarchy_type, hierarchy_info in predefined_hierarchies.items():
                        levels = hierarchy_info["levels"]
                        patterns = hierarchy_info["patterns"]
                        priority = hierarchy_info["priority"]
                        description = hierarchy_info["description"]
                        
                        # Skip if this type already exists in the table
                        existing_types = [eh["hierarchy_type"] for eh in table_existing_hierarchies]
                        if any(hierarchy_type.lower() in et.lower() for et in existing_types):
                            continue
                        
                        # Advanced pattern matching
                        matching_columns = {}
                        pattern_score = 0
                        
                        for i, col_name in enumerate(col_names_lower):
                            for pattern in patterns:
                                if pattern in col_name:
                                    # Find the best matching level for this column
                                    best_level = None
                                    best_score = 0
                                    
                                    for level in levels:
                                        level_words = level.lower().split()
                                        col_words = col_name.split()
                                        
                                        # Calculate similarity score
                                        score = 0
                                        for level_word in level_words:
                                            if level_word in col_name:
                                                score += 2
                                            for col_word in col_words:
                                                if level_word in col_word or col_word in level_word:
                                                    score += 1
                                        
                                        if score > best_score:
                                            best_score = score
                                            best_level = level
                                    
                                    if best_level and best_score > 0:
                                        if best_level not in matching_columns:
                                            matching_columns[best_level] = []
                                        matching_columns[best_level].append({
                                            "column": clean_columns[i],
                                            "score": best_score,
                                            "pattern": pattern
                                        })
                                        pattern_score += best_score
                        
                        # Determine if this is a detected or potential hierarchy
                        if matching_columns and len(matching_columns) >= 2:
                            # Sort levels by their original order
                            ordered_levels = [level for level in levels if level in matching_columns]
                            completeness = f"{len(ordered_levels)}/{len(levels)}"
                            
                            # Check data quality and hierarchy validity
                            hierarchy_quality = _assess_hierarchy_quality(table, matching_columns, sample_data)
                            
                            if len(ordered_levels) >= 3 or pattern_score >= 6:
                                detected_hierarchies.append({
                                    "type": hierarchy_type.replace("_", " ").title(),
                                    "levels": ordered_levels,
                                    "all_levels": levels,
                                    "completeness": completeness,
                                    "matching_columns": matching_columns,
                                    "priority": priority,
                                    "description": description,
                                    "quality_score": hierarchy_quality,
                                    "pattern_score": pattern_score,
                                    "status": "SUGGESTED_NEW"
                                })
                            else:
                                potential_hierarchies.append({
                                    "type": hierarchy_type.replace("_", " ").title(),
                                    "present_levels": ordered_levels,
                                    "missing_levels": [level for level in levels if level not in matching_columns],
                                    "matching_columns": matching_columns,
                                    "priority": priority,
                                    "description": description,
                                    "pattern_score": pattern_score,
                                    "status": "POTENTIAL_OPPORTUNITY"
                                })

                    # Store analysis results
                    hierarchy_analysis.append({
                        "table": table,
                        "columns": clean_columns,
                        "column_count": len(clean_columns),
                        "detected_hierarchies": detected_hierarchies,
                        "potential_hierarchies": potential_hierarchies,
                        "existing_hierarchies": table_existing_hierarchies,
                        "missing_levels": list(set(missing_levels)),
                        "row_count_sample": len(result)
                    })

                    # Generate enhanced recommendations based on analysis
                    if detected_hierarchies:
                        for hierarchy in detected_hierarchies:
                            if len(hierarchy["levels"]) < len(hierarchy["all_levels"]):
                                missing_for_complete = [lvl for lvl in hierarchy["all_levels"] if lvl not in hierarchy["levels"]]
                                recommendations.append({
                                    "priority": hierarchy["priority"],
                                    "category": "Enhance Suggested Hierarchy",
                                    "table": table,
                                    "hierarchy_type": hierarchy["type"],
                                    "description": f"Table {table} has incomplete {hierarchy['type']} hierarchy ({hierarchy['completeness']})",
                                    "action": f"Consider adding columns for: {', '.join(missing_for_complete)}",
                                    "quality_score": hierarchy.get("quality_score", 0.5)
                                })
                            else:
                                recommendations.append({
                                    "priority": "HIGH",
                                    "category": "Create New Power BI Hierarchy",
                                    "table": table,
                                    "hierarchy_type": hierarchy["type"],
                                    "description": f"Table {table} has complete {hierarchy['type']} hierarchy structure",
                                    "action": f"CREATE Power BI hierarchy with levels: {' > '.join(hierarchy['levels'])}",
                                    "quality_score": hierarchy.get("quality_score", 0.8)
                                })
                    
                    if potential_hierarchies:
                        for potential in potential_hierarchies:
                            recommendations.append({
                                "priority": potential["priority"],
                                "category": "Potential New Hierarchy",
                                "table": table,
                                "hierarchy_type": potential["type"],
                                "description": f"Table {table} shows potential for {potential['type']} hierarchy",
                                "action": f"Consider adding columns for: {', '.join(potential['missing_levels'])}",
                                "quality_score": potential.get("pattern_score", 0) / 10.0
                            })
                    
                    if table_existing_hierarchies:
                        for existing in table_existing_hierarchies:
                            recommendations.append({
                                "priority": "VERIFY",
                                "category": "Existing Hierarchy - Document Usage",
                                "table": table,
                                "hierarchy_type": existing["hierarchy_type"],
                                "description": f"Existing hierarchy '{existing['hierarchy_name']}' in table {table}",
                                "action": f"Verify usage and document: {existing['level_path']}",
                                "quality_score": 1.0
                            })
                    
                    if not detected_hierarchies and not potential_hierarchies and not table_existing_hierarchies:
                        recommendations.append({
                            "priority": "LOW",
                            "category": "No Hierarchy Detected",
                            "table": table,
                            "hierarchy_type": "None",
                            "description": f"Table {table} does not show clear hierarchy patterns",
                            "action": "Evaluate if this table could benefit from hierarchical organization",
                            "quality_score": 0.0
                        })
                        
                except Exception as e:
                    logger.warning(f"Error analyzing table {table}: {e}")
                    hierarchy_analysis.append({
                        "table": table,
                        "error": str(e),
                        "detected_hierarchies": [],
                        "potential_hierarchies": [],
                        "existing_hierarchies": [],
                        "missing_levels": []
                    })

            # Step 3: Generate unified CSV with existing and suggested hierarchies
            logger.info("Step 3: Generating unified CSV file with existing and suggested hierarchies...")
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                workspace_name = getattr(self.tabular_editor, 'current_workspace', 'Unknown')
                dataset_name = getattr(self.tabular_editor, 'current_database', 'Unknown')
                
                # Clean workspace and dataset names for filename
                workspace_name = re.sub(r'[^\w\-_]', '_', str(workspace_name))
                dataset_name = re.sub(r'[^\w\-_]', '_', str(dataset_name))
                
                filename = f"Existing_vs_Suggested_Hierarchies_{workspace_name}_{dataset_name}_{timestamp}.csv"
                
                # Ensure output directory exists
                output_dir = "c:\\MCP-Ai"
                os.makedirs(output_dir, exist_ok=True)
                file_path = os.path.join(output_dir, filename)

                csv_rows = []
                
                # First, add all existing hierarchies from the detailed analysis
                logger.info(f"Adding {len(existing_hierarchies_detailed)} existing hierarchies to CSV...")
                for existing in existing_hierarchies_detailed:
                    csv_rows.append({
                        "Workspace": workspace_name,
                        "Dataset": dataset_name,
                        "Table": str(existing["table"]),
                        "Analysis_Type": "EXISTING HIERARCHY",
                        "Hierarchy_Status": "EXISTS_IN_MODEL",
                        "Hierarchy_Type": existing.get("hierarchy_type", "Power BI Hierarchy"),
                        "Hierarchy_Name": str(existing["hierarchy_name"]),
                        "Level_Path": existing.get("level_path", "Level details unavailable"),
                        "Column_Path": existing.get("column_path", "Column details unavailable"),
                        "Level_Count": int(existing["level_count"]) if existing["level_count"] else 0,
                        "Missing_Levels": "N/A",
                        "Suggested_Enhancements": "Review for optimization opportunities",
                        "Implementation_Status": "IMPLEMENTED",
                        "Analysis_Notes": f"Found in Power BI model with {existing['level_count']} levels"
                    })
                
                logger.info(f"Added {len(existing_hierarchies_detailed)} existing hierarchy rows to CSV")
                
                # Then, process each table analysis for suggested hierarchies
                logger.info("Adding suggested hierarchies to CSV...")
                for entry in hierarchy_analysis:
                    table_name = entry["table"]
                    
                    # Handle errors
                    if "error" in entry:
                        csv_rows.append({
                            "Workspace": workspace_name,
                            "Dataset": dataset_name,
                            "Table": table_name,
                            "Analysis_Type": "ERROR",
                            "Hierarchy_Status": "ANALYSIS_ERROR",
                            "Hierarchy_Type": "N/A",
                            "Hierarchy_Name": "N/A",
                            "Level_Path": "N/A",
                            "Column_Path": "N/A",
                            "Level_Count": 0,
                            "Missing_Levels": "N/A",
                            "Suggested_Enhancements": "N/A",
                            "Implementation_Status": "ERROR",
                            "Analysis_Notes": f"Analysis failed: {entry['error']}"
                        })
                        continue
                    
                    # Handle detected hierarchies (strong suggestions)
                    if entry.get("detected_hierarchies"):
                        for h in entry["detected_hierarchies"]:
                            missing_for_complete = [lvl for lvl in h.get("all_levels", []) if lvl not in h["levels"]]
                            status = "READY_TO_CREATE" if len(h["levels"]) >= 3 else "NEEDS_ENHANCEMENT"
                            
                            # Create column path from matching columns
                            column_path_parts = []
                            for level in h["levels"]:
                                if level in h.get("matching_columns", {}):
                                    col_info = h["matching_columns"][level][0]  # Get first matching column
                                    column_path_parts.append(f"[{col_info['column']}]")
                                else:
                                    column_path_parts.append(f"[{level}]")
                            
                            csv_rows.append({
                                "Workspace": workspace_name,
                                "Dataset": dataset_name,
                                "Table": table_name,
                                "Analysis_Type": "SUGGESTED HIERARCHY",
                                "Hierarchy_Status": status,
                                "Hierarchy_Type": h["type"],
                                "Hierarchy_Name": f"{table_name} {h['type']} Hierarchy (Suggested)",
                                "Level_Path": " > ".join(h["levels"]),
                                "Column_Path": " > ".join(column_path_parts),
                                "Level_Count": len(h["levels"]),
                                "Missing_Levels": ", ".join(missing_for_complete) if missing_for_complete else "None",
                                "Suggested_Enhancements": f"Add missing levels: {', '.join(missing_for_complete)}" if missing_for_complete else "Ready to implement",
                                "Implementation_Status": "PENDING_CREATION",
                                "Analysis_Notes": f"Pattern score: {h.get('pattern_score', 0)}, Completeness: {h.get('completeness', 'Unknown')}"
                            })
                    
                    # Handle potential hierarchies (opportunities)
                    if entry.get("potential_hierarchies"):
                        for p in entry["potential_hierarchies"]:
                            missing_count = len(p.get("missing_levels", []))
                            present_count = len(p.get("present_levels", []))
                            
                            csv_rows.append({
                                "Workspace": workspace_name,
                                "Dataset": dataset_name,
                                "Table": table_name,
                                "Analysis_Type": "POTENTIAL HIERARCHY",
                                "Hierarchy_Status": "OPPORTUNITY",
                                "Hierarchy_Type": f"{p['type']} (Opportunity)",
                                "Hierarchy_Name": f"{table_name} {p['type']} (Potential)",
                                "Level_Path": " > ".join(p.get("present_levels", [])),
                                "Column_Path": " > ".join([f"[{level}]" for level in p.get("present_levels", [])]),
                                "Level_Count": present_count,
                                "Missing_Levels": ", ".join(p.get("missing_levels", [])),
                                "Suggested_Enhancements": f"Add columns for: {', '.join(p.get('missing_levels', []))}",
                                "Implementation_Status": "REQUIRES_DATA_ENHANCEMENT",
                                "Analysis_Notes": f"Pattern score: {p.get('pattern_score', 0)}, Missing {missing_count} levels"
                            })
                    
                    # Handle tables with no hierarchy opportunities
                    if (not entry.get("detected_hierarchies") and 
                        not entry.get("potential_hierarchies") and 
                        not entry.get("existing_hierarchies")):
                        csv_rows.append({
                            "Workspace": workspace_name,
                            "Dataset": dataset_name,
                            "Table": table_name,
                            "Analysis_Type": "NO HIERARCHY",
                            "Hierarchy_Status": "NO_PATTERN_DETECTED",
                            "Hierarchy_Type": "None Detected",
                            "Hierarchy_Name": "N/A",
                            "Level_Path": "N/A",
                            "Column_Path": "N/A",
                            "Level_Count": 0,
                            "Missing_Levels": "N/A",
                            "Suggested_Enhancements": "N/A",
                            "Implementation_Status": "NO_ACTION_NEEDED",
                            "Analysis_Notes": f"No clear hierarchy patterns found in {entry.get('column_count', 0)} columns"
                        })

                # Write unified CSV file
                if csv_rows:
                    try:
                        df = pd.DataFrame(csv_rows)
                        df.to_csv(file_path, index=False, encoding="utf-8-sig")
                        csv_file_path = file_path
                        logger.info(f"Unified hierarchy analysis CSV file successfully generated: {csv_file_path}")
                        logger.info(f"Total hierarchy analysis rows: {len(csv_rows)}")
                        
                        # Print detailed summary statistics
                        analysis_types = df['Analysis_Type'].value_counts()
                        hierarchy_statuses = df['Hierarchy_Status'].value_counts()
                        logger.info(f"Analysis breakdown: {dict(analysis_types)}")
                        logger.info(f"Status breakdown: {dict(hierarchy_statuses)}")
                        
                        # Log specific counts for existing vs suggested
                        existing_count = len(df[df['Analysis_Type'] == 'EXISTING HIERARCHY'])
                        suggested_count = len(df[df['Analysis_Type'] == 'SUGGESTED HIERARCHY'])
                        potential_count = len(df[df['Analysis_Type'] == 'POTENTIAL HIERARCHY'])
                        logger.info(f"Summary: {existing_count} existing, {suggested_count} suggested, {potential_count} potential hierarchies")
                        
                    except Exception as csv_error:
                        logger.error(f"Error writing CSV file: {csv_error}")
                        csv_file_path = None
                else:
                    logger.warning("No data to write to CSV file")
                    csv_file_path = None
                    
            except Exception as e:
                logger.warning(f"Failed to generate CSV file: {e}")
                csv_file_path = None

            # Step 4: Compile final comprehensive results
            logger.info("Step 4: Compiling final comprehensive analysis results...")
            
            # Calculate summary statistics
            total_tables = len(tables)
            total_detected = sum(len(entry.get("detected_hierarchies", [])) for entry in hierarchy_analysis)
            total_potential = sum(len(entry.get("potential_hierarchies", [])) for entry in hierarchy_analysis)
            total_existing = len(existing_hierarchies_detailed)
            
            # Get hierarchy types found across existing and suggested
            hierarchy_types_found = set()
            # Add existing hierarchy types
            for existing in existing_hierarchies_detailed:
                hierarchy_types_found.add(existing.get("hierarchy_type", "Unknown"))
            # Add suggested hierarchy types
            for entry in hierarchy_analysis:
                for h in entry.get("detected_hierarchies", []):
                    hierarchy_types_found.add(h.get("type", "Unknown"))
                for p in entry.get("potential_hierarchies", []):
                    hierarchy_types_found.add(p.get("type", "Unknown"))
            
            # Enhanced final comprehensive result
            result = {
                "analysis_summary": {
                    "workspace": workspace_name,
                    "dataset": dataset_name,
                    "tables_analyzed": total_tables,
                    "total_existing_hierarchies": total_existing,
                    "total_suggested_hierarchies": total_detected,
                    "total_potential_hierarchies": total_potential,
                    "total_all_hierarchies": total_existing + total_detected + total_potential,
                    "analysis_timestamp": datetime.now().isoformat(),
                    "csv_rows_generated": len(csv_rows) if 'csv_rows' in locals() else 0
                },
                "debug_info": debug_info,  # Include debug information in result
                "existing_hierarchies_detailed": existing_hierarchies_detailed,
                "suggested_hierarchies_analysis": hierarchy_analysis,
                "recommendations": recommendations,
                "hierarchy_types_found": list(hierarchy_types_found),
                "csv_file_path": csv_file_path,
                "enhancement_summary": {
                    "existing_model_hierarchies": total_existing,
                    "ready_to_create_suggested": len([r for r in recommendations if r.get("category") == "Create New Power BI Hierarchy"]),
                    "enhancement_opportunities": len([r for r in recommendations if r.get("category") == "Enhance Suggested Hierarchy"]),
                    "potential_new_hierarchies": len([r for r in recommendations if r.get("category") == "Potential New Hierarchy"]),
                    "existing_to_document": len([r for r in recommendations if r.get("category") == "Existing Hierarchy - Document Usage"])
                },
                "detailed_breakdown": {
                    "tables_with_existing_hierarchies": len(set(h["table"] for h in existing_hierarchies_detailed)),
                    "tables_with_suggested_hierarchies": len([entry for entry in hierarchy_analysis if entry.get("detected_hierarchies")]),
                    "tables_with_potential_hierarchies": len([entry for entry in hierarchy_analysis if entry.get("potential_hierarchies")]),
                    "tables_with_no_hierarchies": len([entry for entry in hierarchy_analysis 
                                                     if not entry.get("detected_hierarchies") and 
                                                        not entry.get("potential_hierarchies") and 
                                                        not entry.get("existing_hierarchies")])
                }
            }
            
            # Add file generation status
            if csv_file_path:
                result["csv_generation_status"] = "SUCCESS"
                result["csv_file_location"] = csv_file_path
            else:
                result["csv_generation_status"] = "FAILED"
                result["csv_generation_error"] = "Failed to generate CSV file - check logs for details"
            
            logger.info(f"Comprehensive hierarchy evaluation completed!")
            logger.info(f"Found: {total_existing} existing, {total_detected} suggested, {total_potential} potential hierarchies")
            logger.info(f"CSV file: {csv_file_path or 'Not generated'}")
            return result
        
        except Exception as e:
            logger.error(f"Failed to evaluate table hierarchies: {e}")
            return {
                "analysis_summary": {
                    "workspace": "Unknown",
                    "dataset": "Unknown", 
                    "tables_analyzed": 0,
                    "total_existing_hierarchies": 0,
                    "total_suggested_hierarchies": 0,
                    "total_potential_hierarchies": 0,
                    "total_all_hierarchies": 0,
                    "analysis_timestamp": datetime.now().isoformat(),
                    "error": str(e)
                },
                "debug_info": {"error": str(e), "function_failed": True},  # Include debug info in error case too
                "existing_hierarchies_detailed": [],
                "suggested_hierarchies_analysis": [],
                "recommendations": [],
                "hierarchy_types_found": [],
                "csv_file_path": None,
                "csv_generation_status": "FAILED",
                "error_details": str(e)
            }

    def evaluate_measures_analysis(self, tables_to_analyze: List[str] = None) -> Dict[str, Any]:
        """
        Comprehensive measures analysis with integrated CSV generation.
        Analyzes measure calculation logic, standardization, complexity, and provides recommendations
        for improving measure design and maintainability.
        
        Args:
            tables_to_analyze: Optional list of specific tables to analyze. If None, analyzes all tables.
            
        Returns:
            Dictionary containing complete measures analysis, recommendations, and CSV file path
        """
        if not self.tabular_editor.connected:
            raise Exception("Tabular server is not connected. Please connect to a dataset first.")
        
        try:
            logger.info("Starting comprehensive measures analysis with integrated CSV generation...")
            
            # Get tables to analyze
            if tables_to_analyze:
                available_tables = self.tabular_editor.list_tables()
                invalid_tables = [t for t in tables_to_analyze if t not in available_tables]
                if invalid_tables:
                    raise Exception(f"Tables not found: {invalid_tables}")
                tables = tables_to_analyze
            else:
                tables = self.tabular_editor.list_tables()
            
            logger.info(f"Analyzing measures across {len(tables)} tables...")
            
            # INLINE HELPER FUNCTIONS
            def classify_measure_calculation(expression: str) -> str:
                """Classify the type of calculation used in a measure."""
                expression_lower = expression.lower()
                
                # Time intelligence functions
                time_intelligence_funcs = ['totalytd', 'totalmtd', 'totalqtd', 'sameperiodlastyear', 
                                          'previousmonth', 'previousyear', 'dateadd', 'datesytd',
                                          'datesmtd', 'datesqtd', 'parallelperiod']
                
                if any(func in expression_lower for func in time_intelligence_funcs):
                    return "time_intelligence"
                
                # Simple aggregations
                simple_agg_funcs = ['sum(', 'count(', 'average(', 'max(', 'min(', 'counta(', 'countrows(']
                if any(func in expression_lower for func in simple_agg_funcs) and len(expression) < 100:
                    return "simple_aggregation"
                
                # Ratio and percentage calculations
                ratio_funcs = ['divide(', 'percentage', '%', 'ratio']
                if any(func in expression_lower for func in ratio_funcs):
                    return "ratio_percentage"
                
                # Complex calculations (multiple functions, long expressions)
                if len(expression) > 200 or expression.count('(') > 5:
                    return "complex_calculation"
                
                # Calculated measures (has some business logic)
                if any(func in expression_lower for func in ['if(', 'switch(', 'calculate(', 'filter(']):
                    return "calculated_measure"
                
                # Undocumented or unclear
                return "undocumented"
            
            def analyze_measure_standardization(measure: Dict[str, Any]) -> float:
                """Analyze how well a measure follows standardization practices."""
                score = 0.0
                
                # Name standardization (20%)
                name = measure["measure_name"]
                if " " in name and name.replace(" ", "").isalnum():  # Proper spacing
                    score += 0.2
                elif name.replace("_", "").replace(" ", "").isalnum():  # Acceptable naming
                    score += 0.1
                
                # Description presence and quality (30%)
                description = measure["description"]
                if description:
                    if len(description) > 20:  # Meaningful description
                        score += 0.3
                    else:
                        score += 0.1
                
                # Format string presence (20%)
                if measure["format_string"]:
                    score += 0.2
                
                # Expression clarity (30%)
                expression = measure["expression"]
                if len(expression) > 0:
                    # Check for common good practices
                    if 'SUM(' in expression.upper() or 'COUNT(' in expression.upper():
                        score += 0.1
                    if not expression.lower().startswith('='):  # Modern syntax
                        score += 0.1
                    if len(expression) < 150:  # Not overly complex
                        score += 0.1
                
                return min(score, 1.0)
            
            def analyze_measure_best_practices(measure: Dict[str, Any]) -> float:
                """Analyze how well a measure follows best practices."""
                score = 0.0
                
                # Has description (25%)
                if measure["description"]:
                    score += 0.25
                
                # Has format string (25%)
                if measure["format_string"]:
                    score += 0.25
                
                # Proper data type (15%)
                data_type = measure["data_type"].lower()
                if data_type in ['decimal', 'currency', 'percentage', 'integer']:
                    score += 0.15
                
                # Not hidden unnecessarily (10%)
                if not measure["is_hidden"]:
                    score += 0.1
                
                # Expression quality (25%)
                expression = measure["expression"]
                if expression:
                    # Check for error handling
                    if 'DIVIDE(' in expression.upper() or 'IFERROR(' in expression.upper():
                        score += 0.1
                    # Check for reasonable complexity
                    if len(expression) < 300:
                        score += 0.1
                    # Check for proper function usage
                    if any(func in expression.upper() for func in ['SUM(', 'AVERAGE(', 'COUNT(']):
                        score += 0.05
                
                return min(score, 1.0)
            
            def generate_measure_recommendations(measure: Dict[str, Any], calc_type: str, 
                                               standardization_score: float, best_practices_score: float) -> List[str]:
                """Generate specific recommendations for improving a measure."""
                recommendations = []
                
                # Description recommendations
                if not measure["description"]:
                    recommendations.append("Add a clear description explaining what this measure calculates")
                elif len(measure["description"]) < 20:
                    recommendations.append("Expand description to be more detailed and informative")
                
                # Format string recommendations
                if not measure["format_string"]:
                    if calc_type == "ratio_percentage":
                        recommendations.append("Add percentage format string (e.g., '0.00%')")
                    elif "currency" in measure["measure_name"].lower() or "sales" in measure["measure_name"].lower():
                        recommendations.append("Add currency format string (e.g., '$#,##0')")
                    else:
                        recommendations.append("Add appropriate format string for better display")
                
                # Calculation improvements
                if calc_type == "undocumented":
                    recommendations.append("Review and improve calculation logic for clarity")
                elif calc_type == "complex_calculation":
                    recommendations.append("Consider breaking down into simpler measures or add detailed comments")
                
                # Error handling
                expression = measure["expression"].upper()
                if "DIVIDE(" not in expression and "/" in expression:
                    recommendations.append("Use DIVIDE() function instead of / operator to handle division by zero")
                
                # Naming improvements
                name = measure["measure_name"]
                if "_" in name and " " not in name:
                    recommendations.append("Consider using spaces instead of underscores in measure name")
                elif name.isupper() or name.islower():
                    recommendations.append("Use proper case (Title Case) for measure name")
                
                # Overall score recommendations
                if standardization_score < 0.6:
                    recommendations.append("Improve overall standardization (naming, description, formatting)")
                if best_practices_score < 0.6:
                    recommendations.append("Follow Power BI best practices for measure development")
                
                return recommendations if recommendations else ["Measure follows good practices"]
            
            def get_measure_complexity(expression: str) -> str:
                """Determine the complexity level of a measure expression."""
                if len(expression) < 50:
                    return "Low"
                elif len(expression) < 150:
                    return "Medium"
                else:
                    return "High"
            
            # Collect all measures from specified tables
            all_measures = []
            measure_count_by_table = {}
            
            for table_name in tables:
                try:
                    table_obj = next((t for t in self.tabular_editor.model.Tables 
                                    if t.Name.lower() == table_name.lower()), None)
                    if table_obj and hasattr(table_obj, 'Measures'):
                        table_measures = []
                        for measure in table_obj.Measures:
                            measure_info = {
                                "table_name": table_name,
                                "measure_name": measure.Name,
                                "expression": getattr(measure, 'Expression', ''),
                                "description": getattr(measure, 'Description', ''),
                                "format_string": getattr(measure, 'FormatString', ''),
                                "is_hidden": getattr(measure, 'IsHidden', False),
                                "data_type": str(getattr(measure, 'DataType', 'Unknown'))
                            }
                            table_measures.append(measure_info)
                            all_measures.append(measure_info)
                        
                        measure_count_by_table[table_name] = len(table_measures)
                        logger.info(f"Found {len(table_measures)} measures in table '{table_name}'")
                    else:
                        measure_count_by_table[table_name] = 0
                        
                except Exception as e:
                    logger.warning(f"Could not retrieve measures for table '{table_name}': {e}")
                    measure_count_by_table[table_name] = 0
            
            logger.info(f"Total measures collected: {len(all_measures)}")
            
            # ANALYZE MEASURE CALCULATION LOGIC
            measure_analysis = []
            calculation_patterns = {
                "simple_aggregation": 0,
                "calculated_measure": 0,
                "time_intelligence": 0,
                "ratio_percentage": 0,
                "complex_calculation": 0,
                "undocumented": 0
            }
            
            standardization_issues = []
            best_practices_violations = []
            
            for measure in all_measures:
                expression = measure["expression"].strip()
                description = measure["description"].strip()
                measure_name = measure["measure_name"]
                table_name = measure["table_name"]
                
                # Analyze calculation logic type
                calc_type = classify_measure_calculation(expression)
                calculation_patterns[calc_type] += 1
                
                # Analyze standardization
                standardization_score = analyze_measure_standardization(measure)
                
                # Check best practices
                best_practices_score = analyze_measure_best_practices(measure)
                
                # Generate recommendations
                recommendations = generate_measure_recommendations(measure, calc_type, standardization_score, best_practices_score)
                
                measure_analysis.append({
                    "table_name": table_name,
                    "measure_name": measure_name,
                    "calculation_type": calc_type,
                    "complexity_level": get_measure_complexity(expression),
                    "has_description": bool(description),
                    "standardization_score": standardization_score,
                    "best_practices_score": best_practices_score,
                    "recommendations": recommendations,
                    "full_expression": expression
                })
                
                # Collect issues for summary
                if standardization_score < 0.7:
                    standardization_issues.append({
                        "table": table_name,
                        "measure": measure_name,
                        "issue": "Low standardization score",
                        "score": standardization_score
                    })
                
                if best_practices_score < 0.7:
                    best_practices_violations.append({
                        "table": table_name,
                        "measure": measure_name,
                        "issue": "Best practices violations",
                        "score": best_practices_score
                    })
            
            # GENERATE RECOMMENDATIONS
            recommendations = []
            
            # Standardization recommendations
            if standardization_issues:
                recommendations.append({
                    "priority": "HIGH",
                    "category": "Measure Standardization",
                    "description": f"Found {len(standardization_issues)} measures with standardization issues",
                    "action": "Improve measure naming, descriptions, and formatting consistency",
                    "affected_measures": standardization_issues[:5]  # Top 5
                })
            
            # Best practices recommendations
            if best_practices_violations:
                recommendations.append({
                    "priority": "MEDIUM", 
                    "category": "Best Practices Compliance",
                    "description": f"Found {len(best_practices_violations)} measures violating best practices",
                    "action": "Add descriptions, format strings, and improve calculation clarity",
                    "affected_measures": best_practices_violations[:5]  # Top 5
                })
            
            # Complex measures recommendation
            complex_measures = [m for m in measure_analysis if m["complexity_level"] == "High"]
            if complex_measures:
                recommendations.append({
                    "priority": "MEDIUM",
                    "category": "Complex Calculations",
                    "description": f"Found {len(complex_measures)} complex measures that may need documentation",
                    "action": "Consider breaking down complex measures or adding detailed descriptions",
                    "affected_measures": [{"table": m["table_name"], "measure": m["measure_name"], 
                                         "complexity": m["complexity_level"]} for m in complex_measures[:5]]
                })
            
            # GENERATE CSV FILE
            logger.info("Generating CSV file with measures analysis...")
            try:
                # Generate timestamp and file info
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                workspace_name = getattr(self.tabular_editor, 'current_workspace', 'Unknown')
                dataset_name = getattr(self.tabular_editor, 'current_database', 'Unknown')
                
                filename = f"Measures_Analysis_{workspace_name}_{dataset_name}_{timestamp}.csv"
                file_path = os.path.join("c:\\MCP-Ai", filename)
                
                # Get workspace and dataset information for CSV
                workspace_dataset_info = {
                    "WorkspaceID": getattr(self.tabular_editor, 'current_workspace_id', 'Unknown'),
                    "WorkspaceName": getattr(self.tabular_editor, 'current_workspace', 'Unknown'),
                    "DatasetID": getattr(self.tabular_editor, 'current_database_id', 'Unknown'),
                    "DatasetName": getattr(self.tabular_editor, 'current_database', 'Unknown')
                }
                
                # Prepare measures data for CSV
                measures_data = []
                
                for measure in measure_analysis:
                    # Determine priority based on scores
                    overall_score = (measure["standardization_score"] + measure["best_practices_score"]) / 2
                    
                    if overall_score >= 0.8:
                        priority = "Good"
                    elif overall_score >= 0.6:
                        priority = "Needs Improvement"
                    else:
                        priority = "Critical"
                    
                    # Format recommendations as a single string
                    rec_text = "; ".join(measure["recommendations"]) if measure["recommendations"] else "No issues found"
                    
                    # Determine calculation type description
                    calc_descriptions = {
                        "simple_aggregation": "Simple aggregation (SUM, COUNT, etc.)",
                        "calculated_measure": "Calculated measure with business logic",
                        "time_intelligence": "Time intelligence calculation",
                        "ratio_percentage": "Ratio or percentage calculation",
                        "complex_calculation": "Complex multi-step calculation",
                        "undocumented": "Undocumented or unclear calculation"
                    }
                    
                    measures_data.append({
                        "WorkspaceID": workspace_dataset_info["WorkspaceID"],
                        "WorkspaceName": workspace_dataset_info["WorkspaceName"],
                        "DatasetID": workspace_dataset_info["DatasetID"],
                        "DatasetName": workspace_dataset_info["DatasetName"],
                        "Table Name": measure["table_name"],
                        "Measure Name": measure["measure_name"],
                        "Calculation Type": calc_descriptions.get(measure["calculation_type"], measure["calculation_type"]),
                        "Complexity Level": measure["complexity_level"],
                        "Has Description": "Yes" if measure["has_description"] else "No",
                        "Overall Score": f"{overall_score:.2f}",
                        "Priority": priority,
                        "Recommendations": rec_text,
                        "Full Expression": measure["full_expression"]
                    })
                
                # Create DataFrame and save as CSV file
                df = pd.DataFrame(measures_data)
                
                # Save to CSV with UTF-8 encoding
                df.to_csv(file_path, index=False, encoding='utf-8-sig')
                
                csv_file_path = file_path
                logger.info(f"CSV file successfully generated: {csv_file_path}")
                
            except Exception as e:
                logger.warning(f"Failed to generate CSV file: {e}")
                csv_file_path = None
            
            # ANALYSIS SUMMARY
            analysis_info = {
                "total_measures": len(all_measures),
                "tables_with_measures": sum(1 for count in measure_count_by_table.values() if count > 0),
                "average_measures_per_table": round(sum(measure_count_by_table.values()) / len(measure_count_by_table), 2),
                "standardization_issues_count": len(standardization_issues),
                "best_practices_violations_count": len(best_practices_violations)
            }
            
            # FINAL RESULT WITH INTEGRATED CSV PATH
            result = {
                "analysis_summary": {
                    "tables_analyzed": len(tables),
                    "total_measures": len(all_measures),
                    "measures_by_table": measure_count_by_table,
                    "calculation_patterns": calculation_patterns,
                    "analysis_info": analysis_info,
                    "analysis_timestamp": datetime.now().isoformat()
                },
                "measure_analysis": measure_analysis,
                "standardization_issues": standardization_issues,
                "best_practices_violations": best_practices_violations,
                "recommendations": recommendations,
                "calculation_examples": [
                    "Total Sales = SUM(Sales[SaleAmount]) - Simple aggregation",
                    "Revenue Growth % = DIVIDE([Current Revenue] - [Previous Revenue], [Previous Revenue], 0) - Ratio calculation",
                    "YTD Sales = TOTALYTD(SUM(Sales[SaleAmount]), Calendar[Date]) - Time intelligence"
                ]
            }
            
            # Add CSV file path or error to result
            if csv_file_path:
                result["csv_file_path"] = csv_file_path
            else:
                result["csv_generation_error"] = "Failed to generate CSV file - check logs for details"
            
            logger.info(f"Measures analysis with CSV generation completed. Analyzed {len(all_measures)} measures")
            return result
            
        except Exception as e:
            logger.error(f"Failed to evaluate measures analysis: {e}")
            raise Exception(f"Failed to evaluate measures analysis: {e}")
        
class PowerBIMCPServer:
    def __init__(self):
        self.server = Server("MCP")
        self.sql_endpoint = SQLEndpoint()
        self.fabric = Fabric()
        self.tabular_editor = TabularEditor()
        self.copilot_evaluator = CopilotDataEvaluator(self.tabular_editor, self.sql_endpoint)
        self.is_connected = False
        self.connection_lock = threading.Lock()
        
        # Setup server handlers
        self._setup_handlers()
        
    def _setup_handlers(self):
        @self.server.list_tools()
        async def handle_list_tools() -> List[Tool]:
            return [
            Tool(
                name="initialize_sql_connection",
                description="Initialize SQL connection with server and database details.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sql_endpoint": {"type": "string"},
                        "sql_database": {"type": "string"}
                    },
                    "required": ["sql_endpoint", "sql_database"]
                }
            ),
            Tool(
                name="get_sql_tables",
                description="Retrieve a list of tables from the SQL database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                    },
                    "required": []
                }
            ),
            Tool(
                name="get_sql_table_schema",
                description="Retrieve the schema of a specific table from the SQL database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {"type": "string"}
                    },
                    "required": ["table_name"]
                }
            ),
            # REMOVED: update_column_names tool - use safe_rename_with_dependencies instead
            # REMOVED: update_table_name tool - use safe_rename_with_dependencies instead
            Tool(
                name="execute_sql_query",
                description="Execute a SQL query against the database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"}
                    },
                    "required": ["query"]
                }
            ),
            Tool(
                name="execute_dax_query",
                description="Execute a DAX query against the database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dax_query": {"type": "string"}
                    },
                    "required": ["dax_query"]
                }
            ),
            Tool(
                name="get_workspace_info",
                description="Retrieve information about the workspace.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {"type": "string"}
                    },
                    "required": ["workspace_identifier"]
                }
            ),
            Tool(
                name="get_lakehouse_info",
                description="Retrieve information about the lakehouse and sql endpoint.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {"type": "string"},
                        "lakehouse_identifier": {"type": "string"}
                    },
                    "required": ["workspace_identifier", "lakehouse_identifier"]
                }
            ),
            Tool(
                name="refresh_sql_endpoint",
                description="Refresh the metadata of a SQL endpoint.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {"type": "string"},
                        "lakehouse_identifier": {"type": "string"}
                    },
                    "required": ["workspace_identifier", "lakehouse_identifier"]
                }
            ),
            Tool(
                name="connect_dataset",
                description="Connect to a Power BI dataset using workspace_identifier (workspace name or ID) and database_name (dataset name)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {
                            "type": "string",
                            "description": "The workspace name or workspace ID (not server_name)"
                        },
                        "database_name": {
                            "type": "string",
                            "description": "The Power BI dataset/database name"
                        }
                    },
                    "required": ["workspace_identifier","database_name"]
                }
            ),
            Tool(
                name="disconnect_dataset",
                description="Disconnecting power bi dataset after use",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="list_tables",
                description="List all tables in the connected semantic model.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="list_all_relationships",
                description="List all relationships in the connected semantic model.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="refresh_semantic_model",
                description="Refresh a Power BI dataset using workspace_identifier (workspace name or ID) and database_name (dataset name)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {
                            "type": "string",
                            "description": "The workspace name or workspace ID (not server_name)"
                        },
                        "semantic_model_name": {
                            "type": "string",
                            "description": "The name of the semantic model to refresh"
                        },
                        "refresh_type": {
                            "type": "string",
                            "description": "The type of refresh to perform"
                        }
                    },
                    "required": ["workspace_identifier","semantic_model_name"]
                }
            ),
            Tool(
                name="check_date_table_exists",
                description="Check if a date table exists in the model and return its details. Can check a specific table or all tables.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Optional: specific table name to check. If not provided, checks all tables for date table candidates."
                        }
                    },
                    "required": []
                }
            ),
            Tool(
                name="mark_as_date_table",
                description="Mark a table as a date table in the model with specified date column as key.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table to mark as date table"
                        },
                        "date_column": {
                            "type": "string",
                            "description": "Optional: specific date column to use as key. If not provided, uses the first date column found."
                        }
                    },
                    "required": ["table_name"]
                }
            ),
            Tool(
                name="unmark_date_table",
                description="Remove date table marking from a table.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table to remove date table marking from"
                        }
                    },
                    "required": ["table_name"]
                }
            ),
            Tool(
                name="get_table_properties",
                description="Get all available properties for a specific table with their current values and metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table to inspect"
                        }
                    },
                    "required": ["table_name"]
                }
            ),
            Tool(
                name="get_column_properties",
                description="Get all available properties for a specific column with their current values and metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table containing the column"
                        },
                        "column_name": {
                            "type": "string",
                            "description": "Name of the column to inspect"
                        }
                    },
                    "required": ["table_name", "column_name"]
                }
            ),
            Tool(
                name="get_measure_properties",
                description="Get all available properties for a specific measure with their current values and metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table containing the measure"
                        },
                        "measure_name": {
                            "type": "string",
                            "description": "Name of the measure to inspect"
                        }
                    },
                    "required": ["table_name", "measure_name"]
                }
            ),
            Tool(
                name="classify_all_measures_in_model",
                description="Automatically classify all measures in the entire model with intelligent annotation assignment based on DAX expression analysis.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="analyze_dependencies",
                description="Analyze dependencies for a given object (table, column, or measure) before renaming to understand impact.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "object_type": {
                            "type": "string",
                            "description": "Type of object to analyze ('table', 'column', 'measure')"
                        },
                        "object_name": {
                            "type": "string", 
                            "description": "Name of the object to analyze"
                        },
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table (required for column and measure analysis)"
                        }
                    },
                    "required": ["object_type", "object_name"]
                }
            ),
            Tool(
                name="dataset_columns_analysis",
                description="Perform comprehensive column analysis for all tables in the dataset. Evaluates column names and data types based on best practices. Generates CSV output with standardized columns.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="evaluate_security_roles",
                description="Comprehensive table-level security role evaluation. Analyzes RLS implementation, identifies sensitive data, evaluates role coverage, and generates security recommendations with CSV output.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "semantic_model_name": {
                            "type": "string",
                            "description": "Optional name of the semantic model to analyze. If not provided, uses the currently connected dataset."
                        }
                    },
                    "required": []
                }
            ),
            Tool(
                name="evaluate_fact_dimension_analysis",
                description="Comprehensive fact and dimension table classification analysis. Analyzes table structure, column patterns, measures, and naming conventions to classify tables as fact, dimension, hybrid, or unclear types. Generates detailed CSV output with scoring and recommendations.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "tables_to_analyze": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                            "description": "Optional list of specific table names to analyze. If not provided, analyzes all tables in the dataset."
                        }
                    },
                    "required": []
                }
            ),
            Tool(
                name="evaluate_table_linking",
                description="Comprehensive relationship and table linking analysis. Evaluates existing relationships, analyzes cardinality patterns, identifies potential missing relationships, and provides recommendations for data model optimization. Generates detailed CSV output with relationship quality scoring and improvement suggestions.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "tables_to_analyze": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                            "description": "Optional list of specific table names to analyze relationships for. If not provided, analyzes all tables in the dataset."
                        },
                        "include_potential_relationships": {
                            "type": "boolean",
                            "description": "Whether to analyze and include potential missing relationships. Default is true."
                        }
                    },
                    "required": []
                }
            ),
            Tool(
                name="evaluate_table_hierarchies",
                description="Comprehensive hierarchy evaluation combining existing and suggested hierarchies in one analysis. Detects existing Power BI hierarchies, analyzes schema for potential new hierarchies based on column patterns, identifies logical groupings and parent-child relationships, evaluates common dimension patterns (Time, Geography, Product, Organization), and generates unified CSV differentiating existing vs suggested hierarchies.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "tables_to_analyze": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                            "description": "Optional list of specific table names to analyze hierarchies for. If not provided, analyzes all tables in the dataset."
                        }
                    },
                    "required": []
                }
            ),
            Tool(
                name="generate_model_diagram",
                description="Generate an ERD (Entity Relationship Diagram) in PNG format documenting the structure of the data model. Shows entities (tables), attributes (columns), primary/foreign keys, relationships with cardinality, and measures. Supports conceptual, logical, and physical ERD levels with detailed entity representation.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "output_format": {
                            "type": "string",
                            "description": "Output format - always generates PNG visual diagram regardless of this parameter",
                            "default": "visual"
                        },
                        "include_measures": {
                            "type": "boolean",
                            "description": "Include measures in the diagram documentation",
                            "default": True
                        },
                        "include_relationships": {
                            "type": "boolean",
                            "description": "Include relationship details in the diagram",
                            "default": True
                        }
                    },
                    "required": []
                }
            ),
            Tool(
                name="evaluate_measures_analysis",
                description="Comprehensive measures analysis with integrated CSV generation. Analyzes measure calculation logic, standardization, complexity, and provides recommendations for improving measure design and maintainability. Evaluates calculation types (simple aggregation, time intelligence, complex calculations), standardization practices (naming, descriptions, formatting), best practices compliance, and generates detailed CSV with scores and recommendations.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "tables_to_analyze": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                            "description": "Optional list of specific table names to analyze measures for. If not provided, analyzes measures across all tables in the dataset."
                        }
                    },
                    "required": []
                }
            )
        ]
        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: Optional[Dict[str, Any]]) -> List[TextContent]:
            """Handle tool calls and return results as TextContent"""
            try:
                logger.info(f"Handling tool call: {name}")
                
                if name == "initialize_sql_connection":
                    result = await self._handle_initialize_sql_connection(arguments)
                elif name == "get_sql_tables":
                    result = await self._handle_get_sql_tables(arguments)
                elif name == "get_sql_table_schema":
                    result = await self._handle_get_sql_table_schema(arguments)
                elif name == "execute_sql_query":
                    result = await self._handle_execute_sql_query(arguments)
                elif name == "get_workspace_info":
                    result = await self._handle_get_workspace_info(arguments)
                elif name == "get_lakehouse_info":
                    result = await self._handle_get_lakehouse_info(arguments)
                elif name == "connect_dataset":
                    result = await self._handle_connect_dataset(arguments)
                elif name == "list_tables":
                    result = await self._handle_list_tables(arguments)
                elif name == "disconnect_dataset":
                    result = await self._handle_disconnect_dataset(arguments)
                elif name == "refresh_semantic_model":
                    result = await self._handle_refresh_semantic_model(arguments)
                elif name == "execute_dax_query":
                    result = await self._handle_execute_dax_query(arguments)
                elif name == "list_all_relationships":
                    result = await self._handle_list_all_relationships(arguments)  
                # REMOVED: update_column_names and update_table_name handlers - use safe_rename_with_dependencies instead
                elif name == "check_date_table_exists":
                    result = await self._handle_check_date_table_exists(arguments)
                elif name == "mark_as_date_table":
                    result = await self._handle_mark_as_date_table(arguments)
                elif name == "unmark_date_table":
                    result = await self._handle_unmark_date_table(arguments)
                elif name == "get_column_properties":
                    result = await self._handle_get_column_properties(arguments)
                elif name == "get_measure_properties":
                    result = await self._handle_get_measure_properties(arguments)
                elif name == "get_table_properties":
                    result = await self._handle_get_table_properties(arguments)
                elif name == "classify_all_measures_in_model":
                    result = await self._handle_classify_all_measures_in_model(arguments)
                elif name == "analyze_dependencies":
                    result = await self._handle_analyze_dependencies(arguments)
                elif name == "refresh_sql_endpoint":
                    result = await self._handle_refresh_sql_endpoint(arguments)
                elif name == "dataset_columns_analysis":
                    result = await self._handle_dataset_columns_analysis(arguments)
                elif name == "evaluate_security_roles":
                    result = await self._handle_evaluate_security_roles(arguments)
                elif name == "evaluate_fact_dimension_analysis":
                    result = await self._handle_evaluate_fact_dimension_analysis(arguments)
                elif name == "evaluate_table_linking":
                    result = await self._handle_evaluate_table_linking(arguments)
                elif name == "evaluate_table_hierarchies":
                    result = await self._handle_evaluate_table_hierarchies(arguments)
                elif name == "evaluate_measures_analysis":
                    result = await self._handle_evaluate_measures_analysis(arguments)
                elif name == "generate_model_diagram":
                    result = await self._handle_generate_model_diagram(arguments)    
                else:
                    logger.warning(f"Unknown tool: {name}")
                    return [TextContent(type="text", text=f"Unknown tool: {name}")]
                
                # Convert string result to TextContent
                return [TextContent(type="text", text=result)]
                
            except Exception as e:
                logger.error(f"Error executing {name}: {str(e)}", exc_info=True)
                return [TextContent(type="text", text=f"Error executing {name}: {str(e)}")]
    
    async def _handle_initialize_sql_connection(self, arguments: Dict[str, Any]) -> str:
        """Handle initialization of SQL connection"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.sql_endpoint.initialize_sql_connection,
                    arguments["sql_endpoint"],
                    arguments["sql_database"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
        
    async def _handle_get_sql_tables(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of SQL tables"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.sql_endpoint.get_sql_tables
                )
                return str(result)

        except Exception as e:
            return f"Connection failed: {str(e)}"

    async def _handle_get_sql_table_schema(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of SQL table schema"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.sql_endpoint.get_sql_table_schema,
                    arguments["table_name"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_execute_sql_query(self, arguments: Dict[str, Any]) -> str:
        """Handle execution of SQL query"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.sql_endpoint.execute_sql_query,
                    arguments["query"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
    
    async def _handle_execute_dax_query(self, arguments: Dict[str, Any]) -> str:
        """Handle execution of DAX query"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.execute_dax_query,
                    arguments["dax_query"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_refresh_sql_endpoint(self, arguments: Dict[str, Any]) -> str:
        """Handle refresh of SQL endpoint"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.fabric.refresh_sql_endpoint,
                    arguments["workspace_identifier"],
                    arguments["lakehouse_identifier"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_get_workspace_info(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of workspace information"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.fabric.get_workspace_info,
                    arguments["workspace_identifier"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
    
    async def _handle_get_lakehouse_info(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of lakehouse information"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.fabric.get_lakehouse_info,
                    arguments["workspace_identifier"],
                    arguments["lakehouse_identifier"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_connect_dataset(self, arguments: Dict[str, Any]) -> str:
        """Handle connection to Power BI"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.connect_dataset,
                    arguments["workspace_identifier"],
                    arguments["database_name"]
                )
                return str(result)
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_get_column_properties(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of column properties"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.get_column_properties,
                    arguments["table_name"],
                    arguments["column_name"]
                )
                return str(result)
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_refresh_semantic_model(self, arguments: Dict[str, Any]) -> str:
        """Handle refresh of Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.refresh_semantic_model,
                    arguments["workspace_identifier"],
                    arguments["semantic_model_name"],
                    arguments["refresh_type"]
                )
                return str(result)
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    # REMOVED: update functions and add functions (update_table_name, update_column_names, 
    # update_table_security_role, update_table_properties, update_measure_properties, 
    # update_column_properties, add_role_member_rest_api, add_directlake_table, 
    # add_measure_annotations, safe_rename_with_dependencies, analyze_dependencies, 
    # classify_all_measures_in_model) - focusing on pure analysis and inspection only
    # These functions were redundant as safe_rename_with_dependencies provides
    # comprehensive dependency checking and safe renaming

    async def _handle_disconnect_dataset(self, arguments: Dict[str, Any]) -> str:
        """Handle disconnection from Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.disconnect_dataset
                )
                return str(msg)

        except Exception as e:
            logger.error(f"error in disconnecting the model from mcp server: {str(e)}")
            return f"error in disconnecting the model from mcp server: {str(e)}"

    async def _handle_list_tables(self, arguments: Dict[str, Any]) -> str:
        """Handle listing tables in the Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.list_tables
                )
                return str(msg)

        except Exception as e:
            logger.error(f"Error listing tables in the model from mcp server: {str(e)}")
            return f"Error listing tables in the model from mcp server: {str(e)}"

    async def _handle_list_all_relationships(self, arguments: Dict[str, Any]) -> str:
        """Handle listing all relationships in the Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.list_all_relationships
                )
                return str(msg)

        except Exception as e:
            logger.error(f"Error listing all relationships in the model from mcp server: {str(e)}")
            return f"Error listing all relationships in the model from mcp server: {str(e)}"
    
    async def _handle_check_date_table_exists(self, arguments: Dict[str, Any]) -> str:
        """Handle checking if date table exists in the model"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.check_date_table_exists,
                    arguments.get("table_name")
                )
                return json.dumps(result)

        except Exception as e:
            logger.error(f"Error checking date table: {str(e)}")
            return f"Error checking date table: {str(e)}"

    async def _handle_mark_as_date_table(self, arguments: Dict[str, Any]) -> str:
        """Handle marking a table as date table"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.mark_as_date_table,
                    arguments["table_name"],
                    arguments.get("date_column")
                )
                return str(result)

        except Exception as e:
            logger.error(f"Error marking table as date table: {str(e)}")
            return f"Error marking table as date table: {str(e)}"

    async def _handle_unmark_date_table(self, arguments: Dict[str, Any]) -> str:
        """Handle removing date table marking from a table"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.unmark_date_table,
                    arguments["table_name"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Error unmarking date table: {str(e)}")
            return f"Error unmarking date table: {str(e)}"
    
    async def _handle_get_table_properties(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of table properties"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.get_table_properties,
                    arguments["table_name"]
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error getting table properties: {str(e)}")
            return f"Error getting table properties: {str(e)}"
    
    async def _handle_get_measure_properties(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of measure properties"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.get_measure_properties,
                    arguments["table_name"],
                    arguments["measure_name"]
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error getting measure properties: {str(e)}")
            return f"Error getting measure properties: {str(e)}"
    
    async def _handle_classify_all_measures_in_model(self, arguments: Dict[str, Any]) -> str:
        """Handle automatic classification of all measures in the model"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.classify_all_measures_in_model
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error classifying all measures: {str(e)}")
            return f"Error classifying all measures: {str(e)}"
    
    async def _handle_analyze_dependencies(self, arguments: Dict[str, Any]) -> str:
        """Handle dependency analysis for an object before renaming"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.analyze_dependencies,
                    arguments["object_type"],
                    arguments["object_name"],
                    arguments.get("table_name")
                )
                return json.dumps(result, indent=2)
                
        except Exception as e:
            logger.error(f"Error analyzing dependencies: {str(e)}")
            return f"Error analyzing dependencies: {str(e)}"
    
    async def _handle_dataset_columns_analysis(self, arguments: Dict[str, Any]) -> str:
        """Handle dataset columns analysis with CSV generation"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.copilot_evaluator.dataset_columns_analysis
                )
                
                if "error" in result:
                    return f"❌ Analysis failed: {result['error']}"
                
                # Format success response with summary
                response_lines = [
                    "✅ Dataset Columns Analysis completed successfully!",
                    "",
                    f"📊 Total columns analyzed: {result['total_columns_analyzed']}",
                    ""
                ]
                
                if "summary" in result and result["summary"]:
                    summary = result["summary"]
                    response_lines.extend([
                        "📈 Analysis Summary:",
                        f"   • High priority issues: {summary.get('priority_distribution', {}).get('high_priority_issues', 0)} columns",
                        f"   • Medium priority issues: {summary.get('priority_distribution', {}).get('medium_priority_issues', 0)} columns", 
                        f"   • Low priority issues: {summary.get('priority_distribution', {}).get('low_priority_issues', 0)} columns",
                        f"   • No issues found: {summary.get('priority_distribution', {}).get('no_issues', 0)} columns",
                        "",
                        "🎯 Quality Scores:",
                        f"   • Overall average: {summary.get('quality_scores', {}).get('average_overall_quality', 0)}%",
                        f"   • Name quality: {summary.get('quality_scores', {}).get('average_name_quality', 0)}%",
                        "",
                        "🏷️ Unambiguous Labeling:",
                        f"   • Clear & self-explanatory: {summary.get('unambiguous_labeling', {}).get('clear_and_self_explanatory', 0)} columns ({summary.get('unambiguous_labeling', {}).get('percentage_unambiguous', 0)}%)",
                        f"   • Requires context: {summary.get('unambiguous_labeling', {}).get('requires_context', 0)} columns",
                        "",
                        "📝 Self-Explanatory Assessment:",
                        f"   • Fully self-explanatory: {summary.get('self_explanatory', {}).get('fully_self_explanatory', 0)} columns ({summary.get('self_explanatory', {}).get('percentage_self_explanatory', 0)}%)",
                        f"   • Not self-explanatory: {summary.get('self_explanatory', {}).get('not_self_explanatory', 0)} columns",
                        f"   • Partially self-explanatory: {summary.get('self_explanatory', {}).get('partially_self_explanatory', 0)} columns",
                        ""
                    ])
                
                if "csv_file_path" in result:
                    response_lines.append(f"💾 CSV report saved: {result['csv_file_path']}")
                else:
                    response_lines.append("⚠️ CSV generation completed but file path not available")
                
                return "\n".join(response_lines)
                
        except Exception as e:
            logger.error(f"Error in dataset columns analysis: {str(e)}")
            return f"❌ Error in dataset columns analysis: {str(e)}"
    
    async def _handle_evaluate_security_roles(self, arguments: Dict[str, Any]) -> str:
        """Handle security roles evaluation with CSV generation"""
        try:
            with self.connection_lock:
                semantic_model_name = arguments.get("semantic_model_name")
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.copilot_evaluator.evaluate_security_roles,
                    semantic_model_name
                )
                
                if "error" in result:
                    return f"❌ Security roles evaluation failed: {result['error']}"
                
                # Format success response with summary
                response_lines = [
                    "🔒 Security Roles Evaluation completed successfully!",
                    "",
                    f"📊 Analysis Summary:",
                    f"   • Dataset: {result['analysis_summary']['dataset']}",
                    f"   • Workspace: {result['analysis_summary']['workspace']}",
                    f"   • Total tables: {result['analysis_summary']['total_tables']}",
                    f"   • Total security roles: {result['analysis_summary']['total_roles']}",
                    f"   • Tables with RLS: {result['analysis_summary']['tables_with_rls']}",
                    f"   • Sensitive tables: {result['analysis_summary']['sensitive_tables']}",
                    f"   • High risk tables: {result['analysis_summary']['high_risk_tables']}",
                    f"   • RLS coverage: {result['analysis_summary']['rls_coverage_percentage']}%",
                    ""
                ]
                
                # Add recommendations summary
                recommendations = result.get('recommendations', [])
                if recommendations:
                    high_priority = sum(1 for r in recommendations if r['priority'] == 'HIGH')
                    medium_priority = sum(1 for r in recommendations if r['priority'] == 'MEDIUM')
                    low_priority = sum(1 for r in recommendations if r['priority'] == 'LOW')
                    
                    response_lines.extend([
                        "⚠️ Recommendations Summary:",
                        f"   • High priority: {high_priority} issues",
                        f"   • Medium priority: {medium_priority} issues",
                        f"   • Low priority: {low_priority} issues",
                        ""
                    ])
                    
                    # Show top 3 high priority recommendations
                    high_priority_recs = [r for r in recommendations if r['priority'] == 'HIGH'][:3]
                    if high_priority_recs:
                        response_lines.append("🚨 Top High Priority Issues:")
                        for i, rec in enumerate(high_priority_recs, 1):
                            response_lines.append(f"   {i}. {rec['table']}: {rec['description']}")
                        response_lines.append("")
                else:
                    response_lines.extend([
                        "✅ No security issues found - All tables have appropriate RLS coverage!",
                        ""
                    ])
                
                # Add CSV file path
                if "csv_file_path" in result:
                    response_lines.append(f"💾 Security analysis CSV saved: {result['csv_file_path']}")
                else:
                    response_lines.append("⚠️ CSV generation completed but file path not available")
                
                return "\n".join(response_lines)
                
        except Exception as e:
            logger.error(f"Error in security roles evaluation: {str(e)}")
            return f"❌ Error in security roles evaluation: {str(e)}"
    
    async def _handle_evaluate_fact_dimension_analysis(self, arguments: Dict[str, Any]) -> str:
        """Handle fact and dimension analysis with CSV generation"""
        try:
            with self.connection_lock:
                tables_to_analyze = arguments.get("tables_to_analyze")
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.copilot_evaluator.evaluate_fact_and_dimension_analysis,
                    tables_to_analyze
                )
                
                if "error" in result:
                    return f"❌ Fact and dimension analysis failed: {result['error']}"
                
                # Format success response with summary
                analysis_summary = result['analysis_summary']
                table_distribution = analysis_summary['table_classification_distribution']
                quality_distribution = analysis_summary['quality_distribution']
                
                response_lines = [
                    "📊 Fact & Dimension Analysis completed successfully!",
                    "",
                    f"🏗️ Analysis Summary:",
                    f"   • Total tables analyzed: {analysis_summary['total_tables_analyzed']}",
                    f"   • Fact tables: {table_distribution['fact_tables']}",
                    f"   • Dimension tables: {table_distribution['dimension_tables']}",
                    f"   • Hybrid tables: {table_distribution['hybrid_tables']}",
                    f"   • Unclear tables: {table_distribution['unclear_tables']}",
                    "",
                    f"⭐ Quality Distribution:",
                    f"   • Excellent: {quality_distribution['excellent_tables']} tables ✅",
                    f"   • Good: {quality_distribution['good_tables']} tables ⚠️",
                    f"   • Fair: {quality_distribution['fair_tables']} tables 🔶",
                    f"   • Poor: {quality_distribution['poor_tables']} tables 🔴",
                    ""
                ]
                
                # Show categorized tables summary
                categorized = result['categorized_tables']
                if categorized['fact_tables']:
                    response_lines.append(f"📈 Fact Tables ({len(categorized['fact_tables'])}):")
                    for table in categorized['fact_tables'][:5]:  # Show first 5
                        response_lines.append(f"   • {table}")
                    if len(categorized['fact_tables']) > 5:
                        response_lines.append(f"   ... and {len(categorized['fact_tables']) - 5} more")
                    response_lines.append("")
                
                if categorized['dimension_tables']:
                    response_lines.append(f"📝 Dimension Tables ({len(categorized['dimension_tables'])}):")
                    for table in categorized['dimension_tables'][:5]:  # Show first 5
                        response_lines.append(f"   • {table}")
                    if len(categorized['dimension_tables']) > 5:
                        response_lines.append(f"   ... and {len(categorized['dimension_tables']) - 5} more")
                    response_lines.append("")
                
                if categorized['hybrid_tables']:
                    response_lines.append(f"🔀 Hybrid Tables ({len(categorized['hybrid_tables'])}):")
                    for table in categorized['hybrid_tables'][:3]:  # Show first 3
                        response_lines.append(f"   • {table}")
                    if len(categorized['hybrid_tables']) > 3:
                        response_lines.append(f"   ... and {len(categorized['hybrid_tables']) - 3} more")
                    response_lines.append("")
                
                if categorized['unclear_tables']:
                    response_lines.append(f"❓ Unclear Tables ({len(categorized['unclear_tables'])}):")
                    for table in categorized['unclear_tables'][:3]:  # Show first 3
                        response_lines.append(f"   • {table}")
                    if len(categorized['unclear_tables']) > 3:
                        response_lines.append(f"   ... and {len(categorized['unclear_tables']) - 3} more")
                    response_lines.append("")
                
                # Show tables needing attention (poor quality)
                poor_quality_tables = [table for table in result['table_analysis'] 
                                     if table['quality_level'] == 'Poor']
                if poor_quality_tables:
                    response_lines.extend([
                        "🚨 Tables Needing Attention (Poor Quality):",
                    ])
                    for table in poor_quality_tables[:3]:  # Show first 3
                        recommendations = table['recommendations'][:2]  # Show first 2 recommendations
                        rec_text = '; '.join(recommendations) if recommendations else "Review table structure"
                        response_lines.append(f"   • {table['table_name']}: {rec_text}")
                    if len(poor_quality_tables) > 3:
                        response_lines.append(f"   ... and {len(poor_quality_tables) - 3} more")
                    response_lines.append("")
                
                # Add CSV file path
                if "csv_file_path" in result:
                    response_lines.append(f"💾 Analysis CSV saved: {result['csv_file_path']}")
                else:
                    response_lines.append("⚠️ CSV generation completed but file path not available")
                
                return "\n".join(response_lines)
                
        except Exception as e:
            logger.error(f"Error in fact and dimension analysis: {str(e)}")
            return f"❌ Error in fact and dimension analysis: {str(e)}"
    
    async def _handle_evaluate_table_linking(self, arguments: Dict[str, Any]) -> str:
        """Handle table linking evaluation request"""
        try:
            if not self.copilot_evaluator:
                return "❌ Error: No dataset connected. Please connect to a dataset first using connect_dataset."
            
            logger.info("Starting table linking evaluation...")
            
            # Get optional parameters
            tables_to_analyze = arguments.get('tables_to_analyze', None)
            include_potential = arguments.get('include_potential_relationships', True)
            
            # Run the evaluation
            result = self.copilot_evaluator.evaluate_table_linking(
                tables_to_analyze=tables_to_analyze
            )
            
            if result:
                response_lines = []
                response_lines.append("🔗 Table Linking Analysis completed successfully!")
                response_lines.append("")
                
                # Parse the result for summary information
                summary_info = result.get('summary', {})
                if summary_info:
                    response_lines.append("📊 Analysis Summary:")
                    if 'total_tables' in summary_info:
                        response_lines.append(f"   • Total tables analyzed: {summary_info['total_tables']}")
                    if 'existing_relationships' in summary_info:
                        response_lines.append(f"   • Existing relationships: {summary_info['existing_relationships']}")
                    if 'potential_relationships' in summary_info:
                        response_lines.append(f"   • Potential relationships identified: {summary_info['potential_relationships']}")
                    if 'average_quality_score' in summary_info:
                        response_lines.append(f"   • Average relationship quality: {summary_info['average_quality_score']:.1f}%")
                    response_lines.append("")
                
                # Show cardinality distribution if available
                cardinality_info = result.get('cardinality_distribution', {})
                if cardinality_info:
                    response_lines.append("🔢 Cardinality Distribution:")
                    for cardinality, count in cardinality_info.items():
                        response_lines.append(f"   • {cardinality}: {count} relationships")
                    response_lines.append("")
                
                # Show quality distribution if available
                quality_info = result.get('quality_distribution', {})
                if quality_info:
                    response_lines.append("⭐ Quality Distribution:")
                    for quality, count in quality_info.items():
                        icon = "✅" if "Excellent" in quality else "⚠️" if "Good" in quality else "🔶" if "Fair" in quality else "🔴"
                        response_lines.append(f"   • {quality}: {count} relationships {icon}")
                    response_lines.append("")
                
                # Show high priority recommendations if available
                recommendations = result.get('top_recommendations', [])
                if recommendations:
                    response_lines.append("🚨 Top Priority Recommendations:")
                    for i, rec in enumerate(recommendations[:3], 1):  # Show first 3
                        response_lines.append(f"   {i}. {rec}")
                    response_lines.append("")
                
                # CSV file information
                csv_path = result.get('csv_file_path')
                if csv_path:
                    response_lines.append(f"💾 Relationship analysis CSV saved: {csv_path}")
                
                return "\n".join(response_lines)
            else:
                return "❌ Table linking evaluation failed to generate results."
                
        except Exception as e:
            logger.error(f"Error in table linking evaluation: {str(e)}")
            return f"❌ Error in table linking evaluation: {str(e)}"
    
    async def _handle_evaluate_table_hierarchies(self, arguments: Dict[str, Any]) -> str:
        """Handle table hierarchies evaluation request"""
        try:
            if not self.copilot_evaluator:
                return "❌ Error: No dataset connected. Please connect to a dataset first using connect_dataset."
            
            logger.info("Starting table hierarchies evaluation...")
            
            # Get optional parameters
            tables_to_analyze = arguments.get('tables_to_analyze', None)
            
            # Run the evaluation
            result = self.copilot_evaluator.evaluate_table_hierarchies(
                tables_to_analyze=tables_to_analyze
            )
            
            if result:
                response_lines = []
                response_lines.append("🏗️ Table Hierarchies Analysis completed successfully!")
                response_lines.append("")
                
                # Parse the result for summary information
                summary_info = result.get('analysis_summary', {})
                if summary_info:
                    response_lines.append("📊 Analysis Summary:")
                    if 'workspace' in summary_info:
                        response_lines.append(f"   • Workspace: {summary_info['workspace']}")
                    if 'dataset' in summary_info:
                        response_lines.append(f"   • Dataset: {summary_info['dataset']}")
                    if 'tables_analyzed' in summary_info:
                        response_lines.append(f"   • Tables analyzed: {summary_info['tables_analyzed']}")
                    if 'total_existing_hierarchies' in summary_info:
                        response_lines.append(f"   • Existing hierarchies: {summary_info['total_existing_hierarchies']}")
                    if 'total_suggested_hierarchies' in summary_info:
                        response_lines.append(f"   • Suggested hierarchies: {summary_info['total_suggested_hierarchies']}")
                    if 'total_potential_hierarchies' in summary_info:
                        response_lines.append(f"   • Potential hierarchies: {summary_info['total_potential_hierarchies']}")
                    response_lines.append("")
                
                # Show enhancement summary if available
                enhancement_info = result.get('enhancement_summary', {})
                if enhancement_info:
                    response_lines.append("🚀 Implementation Opportunities:")
                    if 'ready_to_create_suggested' in enhancement_info:
                        response_lines.append(f"   • Ready to create: {enhancement_info['ready_to_create_suggested']} hierarchies ✅")
                    if 'enhancement_opportunities' in enhancement_info:
                        response_lines.append(f"   • Enhancement opportunities: {enhancement_info['enhancement_opportunities']} hierarchies 🔧")
                    if 'potential_new_hierarchies' in enhancement_info:
                        response_lines.append(f"   • Potential new hierarchies: {enhancement_info['potential_new_hierarchies']} opportunities 💡")
                    if 'existing_to_document' in enhancement_info:
                        response_lines.append(f"   • Existing to document: {enhancement_info['existing_to_document']} hierarchies 📋")
                    response_lines.append("")
                
                # Show detailed breakdown if available
                breakdown_info = result.get('detailed_breakdown', {})
                if breakdown_info:
                    response_lines.append("🔍 Detailed Breakdown:")
                    for key, value in breakdown_info.items():
                        formatted_key = key.replace('_', ' ').title()
                        response_lines.append(f"   • {formatted_key}: {value}")
                    response_lines.append("")
                
                # Show hierarchy types found if available
                hierarchy_types = result.get('hierarchy_types_found', [])
                if hierarchy_types:
                    response_lines.append("📋 Hierarchy Types Identified:")
                    for hierarchy_type in hierarchy_types[:5]:  # Show first 5
                        response_lines.append(f"   • {hierarchy_type}")
                    if len(hierarchy_types) > 5:
                        response_lines.append(f"   ... and {len(hierarchy_types) - 5} more types")
                    response_lines.append("")
                
                # Show top recommendations if available
                recommendations = result.get('recommendations', [])
                if recommendations:
                    high_priority = [r for r in recommendations if r.get('priority') in ['HIGH', 'CRITICAL']]
                    if high_priority:
                        response_lines.append("🚨 High Priority Recommendations:")
                        for i, rec in enumerate(high_priority[:3], 1):  # Show first 3
                            category = rec.get('category', 'General')
                            table = rec.get('table', 'Unknown')
                            action = rec.get('action', 'No action specified')
                            response_lines.append(f"   {i}. [{category}] {table}: {action}")
                        response_lines.append("")
                
                # CSV file information
                csv_path = result.get('csv_file_path')
                if csv_path:
                    response_lines.append(f"💾 Hierarchy analysis CSV saved: {csv_path}")
                
                # Debug information if available
                debug_info = result.get('debug_info', {})
                if debug_info and debug_info.get('error'):
                    response_lines.append("")
                    response_lines.append(f"⚠️ Debug info: {debug_info.get('error', 'Unknown debug issue')}")
                
                return "\n".join(response_lines)
            else:
                return "❌ Table hierarchies evaluation failed to generate results."
                
        except Exception as e:
            logger.error(f"Error in table hierarchies evaluation: {str(e)}")
            return f"❌ Error in table hierarchies evaluation: {str(e)}"

    async def _handle_evaluate_measures_analysis(self, arguments: Dict[str, Any]) -> str:
        """Handle measures analysis with integrated CSV generation."""
        try:
            logger.info("Processing measures analysis request...")
            
            # Extract tables_to_analyze parameter
            tables_to_analyze = arguments.get("tables_to_analyze")
            if tables_to_analyze and not isinstance(tables_to_analyze, list):
                return "❌ Error: tables_to_analyze must be a list of table names."
            
            # Check if we have an active connection
            if not self.tabular_editor.connected:
                return "❌ Error: Not connected to any Power BI dataset. Please connect first using connect_dataset."
            
            # Validate the CopilotDataEvaluator
            if not self.copilot_evaluator:
                return "❌ Error: CopilotDataEvaluator is not initialized."
            
            # Execute the measures analysis
            logger.info(f"Executing measures analysis for tables: {tables_to_analyze or 'all tables'}")
            result = self.copilot_evaluator.evaluate_measures_analysis(
                tables_to_analyze=tables_to_analyze
            )
            
            if result:
                response_lines = []
                response_lines.append("📊 Measures Analysis completed successfully!")
                response_lines.append("")
                
                # Parse the result for summary information
                summary_info = result.get('analysis_summary', {})
                if summary_info:
                    response_lines.append("📈 Analysis Summary:")
                    if 'tables_analyzed' in summary_info:
                        response_lines.append(f"   • Tables analyzed: {summary_info['tables_analyzed']}")
                    if 'total_measures' in summary_info:
                        response_lines.append(f"   • Total measures: {summary_info['total_measures']}")
                    
                    analysis_info = summary_info.get('analysis_info', {})
                    if analysis_info:
                        if 'tables_with_measures' in analysis_info:
                            response_lines.append(f"   • Tables with measures: {analysis_info['tables_with_measures']}")
                        if 'average_measures_per_table' in analysis_info:
                            response_lines.append(f"   • Average measures per table: {analysis_info['average_measures_per_table']}")
                    
                    calculation_patterns = summary_info.get('calculation_patterns', {})
                    if calculation_patterns:
                        response_lines.append("")
                        response_lines.append("📋 Calculation Patterns:")
                        for pattern, count in calculation_patterns.items():
                            if count > 0:
                                pattern_name = pattern.replace('_', ' ').title()
                                response_lines.append(f"   • {pattern_name}: {count}")
                    response_lines.append("")
                
                # Show recommendations summary
                recommendations = result.get('recommendations', [])
                if recommendations:
                    response_lines.append("🚨 Top Recommendations:")
                    for rec in recommendations[:3]:  # Show top 3
                        priority = rec.get('priority', 'UNKNOWN')
                        category = rec.get('category', 'General')
                        description = rec.get('description', 'No description')
                        response_lines.append(f"   {priority}: {category} - {description}")
                    response_lines.append("")
                
                # Show standardization and best practices issues
                analysis_info = summary_info.get('analysis_info', {})
                if analysis_info:
                    std_issues = analysis_info.get('standardization_issues_count', 0)
                    bp_violations = analysis_info.get('best_practices_violations_count', 0)
                    
                    if std_issues > 0 or bp_violations > 0:
                        response_lines.append("⚠️ Issues Summary:")
                        if std_issues > 0:
                            response_lines.append(f"   • Standardization issues: {std_issues} measures")
                        if bp_violations > 0:
                            response_lines.append(f"   • Best practices violations: {bp_violations} measures")
                        response_lines.append("")
                
                # Add CSV file path if available
                csv_file_path = result.get('csv_file_path')
                if csv_file_path:
                    response_lines.append(f"💾 Measures analysis CSV saved: {csv_file_path}")
                else:
                    csv_error = result.get('csv_generation_error')
                    if csv_error:
                        response_lines.append(f"⚠️ CSV generation issue: {csv_error}")
                
                return "\n".join(response_lines)
            else:
                return "❌ Measures analysis failed to generate results."
                
        except Exception as e:
            logger.error(f"Error in measures analysis: {str(e)}")
            return f"❌ Error in measures analysis: {str(e)}"
        
    async def _handle_generate_model_diagram(self, arguments: Dict[str, Any]) -> str:
        """Handle model diagram generation"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.generate_model_diagram,
                    arguments.get("output_format", "visual"),
                    arguments.get("include_measures", True),
                    arguments.get("include_relationships", True)
                )
                return json.dumps(result, indent=2)
                
        except Exception as e:
            logger.error(f"Error generating model diagram: {str(e)}")
            return f"Error generating model diagram: {str(e)}"    
    
    async def run(self):
        """Run the MCP server"""
        try:
            logger.info("Starting Power BI MCP Server...")
            async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                await self.server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name="MCP",
                        server_version="1.0.1",
                        capabilities=self.server.get_capabilities(
                            notification_options=NotificationOptions(),
                            experimental_capabilities={},
                        ),
                    ),
                )
        except anyio.BrokenResourceError:
            logger.info("Client disconnected")
        except Exception as e:
            logger.error(f"Server error: {e}", exc_info=True)
        finally:
            logger.info("Server shutting down")

# Main entry point
async def main():
    server = PowerBIMCPServer()
    await server.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)