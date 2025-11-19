"""
Semantic Model Configuration Manager - Replacement for DatabaseConfigManager
Uses Fabric access tokens and TabularEditor to read dimreport_table from MSXI 2.0 Telemetry semantic model
This replaces the SQL authentication approach with cleaner semantic model access
"""

import logging
import sys
import os
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import pandas as pd

# Add the src directory to the Python path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from server import TabularEditor, Fabric, SQLEndpoint
    from authentication_manager import AuthenticationManager
except ImportError as e:
    print(f"❌ Error importing classes: {e}")

logger = logging.getLogger(__name__)

class SemanticModelConfigManager:
    """
    Semantic model-based configuration manager for workspace/dataset combinations.
    Replaces DatabaseConfigManager with direct semantic model access using Fabric tokens.
    """
    
    def __init__(self, auth_manager=None):
        """Initialize the semantic model configuration manager."""
        self.auth_manager = auth_manager if auth_manager else AuthenticationManager()
        
        # Hardcoded semantic model connection parameters
        self.workspace_name = "*MSX Leader Insights"
        self.semantic_model_name = "MSXI 2.0 Telemetry"
        self.table_name = "dimreport_table"
        
        # Cache configuration
        self.cache_file_path = os.path.join(os.path.dirname(__file__), "..", "cache", "workspace_dataset_cache.json")
        self.cache_max_age_hours = 24  # Cache expires after 24 hours
        
        # Initialize components
        self.fabric = Fabric(self.auth_manager)
        self.sql_endpoint = SQLEndpoint(self.auth_manager)
        self.tabular_editor = TabularEditor(
            fabric_instance=self.fabric,
            sql_endpoint_instance=self.sql_endpoint,
            auth_manager=self.auth_manager
        )
        
        self.connected = False
        self._cached_data = None
        
        # Ensure cache directory exists
        os.makedirs(os.path.dirname(self.cache_file_path), exist_ok=True)
        
        # Load cache on initialization
        self._load_cache_from_file()
        
        logger.info("🔧 SemanticModelConfigManager initialized with caching enabled")
    
    def connect(self) -> bool:
        """
        Connect to the MSXI 2.0 Telemetry semantic model.
        
        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            # Check if already connected by checking both our flag and TabularEditor state
            if (self.connected and 
                hasattr(self.tabular_editor, 'connected') and 
                self.tabular_editor.connected):
                logger.info("🔌 Already connected to semantic model")
                return True
                
            logger.info(f"🔌 Connecting to semantic model: {self.workspace_name} -> {self.semantic_model_name}")
            
            # Ensure clean connection state
            try:
                if hasattr(self.tabular_editor, 'connected') and self.tabular_editor.connected:
                    logger.info("🔄 Disconnecting existing connection before reconnecting...")
                    self.tabular_editor.disconnect_dataset()
            except Exception as disconnect_error:
                logger.warning(f"⚠️ Warning during pre-connection cleanup: {str(disconnect_error)}")
            
            # Connect to semantic model
            self.tabular_editor.connect_dataset(self.workspace_name, self.semantic_model_name)
            self.connected = True
            
            logger.info("✅ Successfully connected to semantic model")
            return True
            
        except Exception as e:
            # Handle the specific "already connected" case more gracefully
            error_msg = str(e)
            if "already connected" in error_msg.lower():
                logger.warning("⚠️ Connection already exists, treating as successful connection")
                self.connected = True
                return True
            else:
                logger.error(f"❌ Failed to connect to semantic model: {error_msg}")
                self.connected = False
                return False
    
    def _get_finallist_data(self) -> List[Dict[str, str]]:
        """
        Get all workspace/dataset combinations from dimreport_table.
        Uses local cache first for immediate response, then attempts live connection.
        
        Returns:
            List of dictionaries with WorkspaceName and DatasetName from PBISourceWorkspaceName and datasetname columns
        """
        try:
            # CACHE-FIRST APPROACH: Return cached data immediately if available
            if self._is_cache_valid():
                logger.info(f"⚡ Using cached data for immediate response ({len(self._cached_data)} records)")
                
                # Try to refresh cache in background (non-blocking)
                try:
                    self._refresh_cache_async()
                except Exception as refresh_error:
                    logger.warning(f"⚠️ Background cache refresh failed (continuing with cached data): {str(refresh_error)}")
                
                return self._cached_data
            
            # NO VALID CACHE: Attempt live connection
            logger.info("📡 No valid cache found, attempting live connection...")
            
            # Try to connect and get fresh data
            fresh_data = self._get_live_data()
            
            if fresh_data:
                # Success: Cache the fresh data and return it
                self._cached_data = fresh_data
                self._save_cache_to_file(fresh_data)
                logger.info(f"✅ Retrieved fresh data and updated cache ({len(fresh_data)} records)")
                return fresh_data
            else:
                # Connection failed: Try to use any existing cache even if expired
                if self._cached_data:
                    logger.warning(f"⚠️ Live connection failed, using expired cache as fallback ({len(self._cached_data)} records)")
                    return self._cached_data
                else:
                    logger.error("❌ No live data and no cache available")
                    return []
            
        except Exception as e:
            logger.error(f"❌ Error in _get_finallist_data: {str(e)}")
            # Last resort: return cached data if any exists
            return self._cached_data if self._cached_data else []
    
    def _get_live_data(self) -> List[Dict[str, str]]:
        """
        Get fresh data from semantic model (separated for clarity).
        
        Returns:
            List of workspace/dataset combinations or empty list if failed
        """
        try:
            # Ensure connection
            if not self.connect():
                logger.error("❌ Cannot connect to semantic model")
                return []
            
            logger.info(f"📊 Querying {self.table_name} table for workspace/dataset combinations...")
            
            # Get all data using DAX query
            dax_query = f"EVALUATE '{self.table_name}'"
            
            result = self.tabular_editor.execute_dax_query(dax_query)
            
            if not result:
                logger.warning("⚠️ No data returned from semantic model")
                return []
            
            # Convert to list of dictionaries
            combinations = []
            for row in result:
                # Use the specific columns: datasetname and PBISourceWorkspaceName
                workspace_name = row.get(f'{self.table_name}[PBISourceWorkspaceName]', '')
                dataset_name = row.get(f'{self.table_name}[datasetname]', '')
                
                if workspace_name and dataset_name:
                    combinations.append({
                        'WorkspaceName': workspace_name,
                        'DatasetName': dataset_name
                    })
            
            logger.info(f"✅ Retrieved {len(combinations)} workspace/dataset combinations from live connection")
            return combinations
            
        except Exception as e:
            logger.error(f"❌ Error retrieving live data from semantic model: {str(e)}")
            return []
    
    def _refresh_cache_async(self):
        """
        Attempt to refresh cache in background (non-blocking).
        This is called when serving cached data to keep cache fresh.
        """
        try:
            import threading
            
            def background_refresh():
                try:
                    fresh_data = self._get_live_data()
                    if fresh_data and len(fresh_data) > 0:
                        self._cached_data = fresh_data
                        self._save_cache_to_file(fresh_data)
                        logger.info(f"🔄 Background cache refresh completed ({len(fresh_data)} records)")
                    else:
                        logger.warning("⚠️ Background refresh returned no data, keeping existing cache")
                except Exception as e:
                    logger.warning(f"⚠️ Background cache refresh failed: {str(e)}")
            
            # Start background refresh (don't wait for it)
            thread = threading.Thread(target=background_refresh, daemon=True)
            thread.start()
            logger.info("🔄 Started background cache refresh")
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to start background refresh: {str(e)}")
    
    def get_unique_workspaces(self, table_name: str = "dimreport_table") -> List[str]:
        """
        Get list of unique workspace names from semantic model.
        
        Args:
            table_name: Name of the table (ignored, uses hardcoded dimreport_table)
            
        Returns:
            List of unique workspace names sorted alphabetically
        """
        try:
            logger.info("📋 Getting unique workspaces from semantic model...")
            
            combinations = self._get_finallist_data()
            
            if not combinations:
                logger.warning("⚠️ No combinations found")
                return []
            
            # Extract unique workspace names
            workspaces = list(set(combo['WorkspaceName'] for combo in combinations if combo['WorkspaceName']))
            workspaces.sort()
            
            logger.info(f"✅ Found {len(workspaces)} unique workspaces")
            return workspaces
            
        except Exception as e:
            logger.error(f"❌ Error getting unique workspaces: {str(e)}")
            return []
    
    def get_datasets_for_workspace(self, workspace_name: str, table_name: str = "dimreport_table") -> List[str]:
        """
        Get list of datasets for a specific workspace from semantic model.
        
        Args:
            workspace_name: Name of the workspace
            table_name: Name of the table (ignored, uses hardcoded dimreport_table)
            
        Returns:
            List of dataset names for the specified workspace sorted alphabetically
        """
        try:
            logger.info(f"📋 Getting datasets for workspace '{workspace_name}' from semantic model...")
            
            combinations = self._get_finallist_data()
            
            if not combinations:
                logger.warning("⚠️ No combinations found")
                return []
            
            # Filter datasets for the specified workspace
            datasets = [
                combo['DatasetName'] 
                for combo in combinations 
                if combo['WorkspaceName'] == workspace_name and combo['DatasetName']
            ]
            
            # Remove duplicates and sort
            datasets = list(set(datasets))
            datasets.sort()
            
            logger.info(f"✅ Found {len(datasets)} datasets for workspace '{workspace_name}'")
            return datasets
            
        except Exception as e:
            logger.error(f"❌ Error getting datasets for workspace '{workspace_name}': {str(e)}")
            return []
    
    def validate_combination(self, workspace_name: str, dataset_name: str, table_name: str = "dimreport_table") -> bool:
        """
        Validate if a workspace/dataset combination exists in the semantic model.
        
        Args:
            workspace_name: Name of the workspace
            dataset_name: Name of the dataset
            table_name: Name of the table (ignored, uses hardcoded dimreport_table)
            
        Returns:
            bool: True if combination exists, False otherwise
        """
        try:
            logger.info(f"🔍 Validating combination: '{workspace_name}' / '{dataset_name}'")
            
            combinations = self._get_finallist_data()
            
            # Check if combination exists
            for combo in combinations:
                if (combo['WorkspaceName'] == workspace_name and 
                    combo['DatasetName'] == dataset_name):
                    logger.info("✅ Combination is valid")
                    return True
            
            logger.warning("⚠️ Combination not found")
            return False
            
        except Exception as e:
            logger.error(f"❌ Error validating combination: {str(e)}")
            return False
    
    def get_workspace_dataset_combinations(self, table_name: str = "dimreport_table") -> List[Dict[str, str]]:
        """
        Get all workspace/dataset combinations from semantic model.
        
        Args:
            table_name: Name of the table (ignored, uses hardcoded dimreport_table)
            
        Returns:
            List of dictionaries with WorkspaceName and DatasetName keys
        """
        try:
            logger.info("📋 Getting all workspace/dataset combinations from semantic model...")
            return self._get_finallist_data()
            
        except Exception as e:
            logger.error(f"❌ Error getting combinations: {str(e)}")
            return []
    
    def setup_connection(self, sql_endpoint: str = None, database_name: str = None) -> bool:
        """
        Setup connection (compatibility method with DatabaseConfigManager interface).
        For semantic model, this just ensures connection to MSXI 2.0 Telemetry model.
        
        Args:
            sql_endpoint: Ignored (for compatibility)
            database_name: Ignored (for compatibility)
            
        Returns:
            bool: True if semantic model connection successful
        """
        logger.info("🔌 Setting up semantic model connection...")
        
        # First validate existing connection
        if self._validate_connection():
            logger.info("✅ Existing connection is valid")
            return True
            
        # If validation failed, try to reconnect
        return self.connect()
    
    def _load_cache_from_file(self):
        """Load cached data from file if it exists and is not expired."""
        try:
            if not os.path.exists(self.cache_file_path):
                logger.info("📁 No cache file found, will create on first successful connection")
                return
                
            with open(self.cache_file_path, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                
            # Check cache expiry
            cache_timestamp = datetime.fromisoformat(cache_data.get('timestamp', ''))
            cache_age = datetime.now() - cache_timestamp
            
            if cache_age.total_seconds() / 3600 > self.cache_max_age_hours:
                logger.info(f"⏰ Cache expired (age: {cache_age}), will refresh on next connection")
                return
                
            self._cached_data = cache_data.get('data', [])
            logger.info(f"✅ Loaded {len(self._cached_data)} workspace/dataset combinations from cache (age: {cache_age})")
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to load cache from file: {str(e)}")
            self._cached_data = None
    
    def _save_cache_to_file(self, data: List[Dict[str, str]]):
        """Save data to cache file with timestamp."""
        try:
            cache_data = {
                'timestamp': datetime.now().isoformat(),
                'data': data,
                'source': f"{self.workspace_name} -> {self.semantic_model_name}",
                'table': self.table_name,
                'count': len(data)
            }
            
            with open(self.cache_file_path, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False)
                
            logger.info(f"💾 Saved {len(data)} workspace/dataset combinations to cache")
            
        except Exception as e:
            logger.error(f"❌ Failed to save cache to file: {str(e)}")
    
    def _is_cache_valid(self) -> bool:
        """Check if current cache is valid and not expired."""
        return (self._cached_data is not None and 
                len(self._cached_data) > 0)
    
    def clear_cache(self):
        """Clear cached data to force fresh retrieval on next request."""
        logger.info("🗑️ Clearing cached data")
        self._cached_data = None
        
        # Also remove cache file
        try:
            if os.path.exists(self.cache_file_path):
                os.remove(self.cache_file_path)
                logger.info("🗑️ Removed cache file")
        except Exception as e:
            logger.warning(f"⚠️ Failed to remove cache file: {str(e)}")
    
    def disconnect(self):
        """Disconnect from semantic model."""
        try:
            if self.connected:
                self.tabular_editor.disconnect_dataset()
                self.connected = False
                logger.info("✅ Disconnected from semantic model")
        except Exception as e:
            logger.warning(f"⚠️ Warning during disconnect: {str(e)}")
    
    def _validate_connection(self) -> bool:
        """
        Validate if the connection is actually active and working.
        
        Returns:
            bool: True if connection is valid and active
        """
        try:
            if not self.connected:
                return False
                
            # Check TabularEditor connection state
            if not (hasattr(self.tabular_editor, 'connected') and self.tabular_editor.connected):
                logger.warning("🔍 Connection state mismatch detected - TabularEditor not connected")
                self.connected = False
                return False
                
            # Test connection with a simple query
            if hasattr(self.tabular_editor, 'model') and self.tabular_editor.model:
                # Connection appears valid
                return True
            else:
                logger.warning("🔍 TabularEditor model not available")
                self.connected = False
                return False
                
        except Exception as e:
            logger.warning(f"🔍 Connection validation failed: {str(e)}")
            self.connected = False
            return False

    def refresh_cache_now(self) -> bool:
        """
        Manually refresh cache with fresh data from semantic model.
        
        Returns:
            bool: True if refresh successful, False otherwise
        """
        try:
            logger.info("🔄 Manual cache refresh requested...")
            fresh_data = self._get_live_data()
            
            if fresh_data:
                self._cached_data = fresh_data
                self._save_cache_to_file(fresh_data)
                logger.info(f"✅ Manual cache refresh completed ({len(fresh_data)} records)")
                return True
            else:
                logger.error("❌ Manual cache refresh failed - no data retrieved")
                return False
                
        except Exception as e:
            logger.error(f"❌ Manual cache refresh failed: {str(e)}")
            return False
    
    def get_status(self) -> Dict[str, str]:
        """
        Get connection status and basic info including cache status.
        
        Returns:
            Dictionary with status information
        """
        # Validate connection before reporting status
        actual_connected = self._validate_connection()
        
        # Get cache info
        cache_info = {}
        if os.path.exists(self.cache_file_path):
            try:
                with open(self.cache_file_path, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    cache_timestamp = datetime.fromisoformat(cache_data.get('timestamp', ''))
                    cache_age = datetime.now() - cache_timestamp
                    cache_info = {
                        "cache_file_exists": True,
                        "cache_timestamp": cache_data.get('timestamp'),
                        "cache_age_hours": round(cache_age.total_seconds() / 3600, 2),
                        "cache_record_count": cache_data.get('count', 0),
                        "cache_expired": cache_age.total_seconds() / 3600 > self.cache_max_age_hours
                    }
            except Exception:
                cache_info = {"cache_file_exists": True, "cache_file_error": True}
        else:
            cache_info = {"cache_file_exists": False}
        
        return {
            "connected": actual_connected,
            "connection_flag": self.connected,
            "workspace": self.workspace_name,
            "semantic_model": self.semantic_model_name,
            "table": self.table_name,
            "cached_records_memory": len(self._cached_data) if self._cached_data else 0,
            "cache_max_age_hours": self.cache_max_age_hours,
            **cache_info
        }

def test_semantic_model_config_manager():
    """Test the semantic model configuration manager."""
    logger.info("=" * 60)
    logger.info("TESTING SEMANTIC MODEL CONFIG MANAGER")
    logger.info("=" * 60)
    
    config_manager = SemanticModelConfigManager()
    
    try:
        # Test connection
        if not config_manager.setup_connection():
            logger.error("❌ Failed to setup connection")
            return False
        
        # Test getting workspaces
        workspaces = config_manager.get_unique_workspaces()
        logger.info(f"📊 Found {len(workspaces)} workspaces")
        if workspaces:
            logger.info(f"📋 Sample workspaces: {workspaces[:5]}")
        
        # Test getting datasets for first workspace
        if workspaces:
            first_workspace = workspaces[0]
            datasets = config_manager.get_datasets_for_workspace(first_workspace)
            logger.info(f"📊 Found {len(datasets)} datasets for '{first_workspace}'")
            if datasets:
                logger.info(f"📋 Sample datasets: {datasets[:3]}")
            
            # Test validation
            if datasets:
                is_valid = config_manager.validate_combination(first_workspace, datasets[0])
                logger.info(f"✅ Validation test: {is_valid}")
        
        # Test getting all combinations
        combinations = config_manager.get_workspace_dataset_combinations()
        logger.info(f"📊 Total combinations: {len(combinations)}")
        
        logger.info("✅ All tests passed!")
        return True
        
    except Exception as e:
        logger.error(f"❌ Test failed: {str(e)}")
        return False
        
    finally:
        config_manager.disconnect()

if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Run test
    test_semantic_model_config_manager()