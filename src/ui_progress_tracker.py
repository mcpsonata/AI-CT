"""
UI Progress Tracker Module

Centralized progress tracking system for real-time UI updates.
Provides reusable progress tracking mechanisms for all analysis functions.

Features:
- Real-time progress updates via callbacks
- Batch completion tracking
- Table and column processing statistics
- Automatic progress percentage calculation
- Comprehensive logging for UI streaming
- Error handling and fallback mechanisms

Usage:
    tracker = UIProgressTracker(progress_callback, session_id="analysis_001")
    tracker.start_analysis("Dataset Column Analysis", total_items=100)
    tracker.update_progress("Processing table Customer", current=25, total=100)
    tracker.complete_batch(1, 5, columns_processed=15, tables_processed=3)
    tracker.complete_analysis(success=True, summary="Analysis completed successfully")
"""

import logging
import time
from typing import Optional, Callable, Dict, Any, List, Set
from datetime import datetime
import threading

# Thread-local storage for session context
_session_context = threading.local()

def get_current_session_id() -> Optional[str]:
    """Get the current session ID from thread-local storage."""
    return getattr(_session_context, 'session_id', None)

def set_session_context(session_id: str):
    """Set the current session ID in thread-local storage."""
    _session_context.session_id = session_id

# Set up logging
logger = logging.getLogger(__name__)

class SessionAwareLogHandler(logging.Handler):
    """
    Session-aware logging handler that only streams logs for the current session.
    Uses thread-local storage to determine which session is active.
    """
    
    def __init__(self, updates_queue_dict: Dict[str, Any]):
        super().__init__()
        self.updates_queue_dict = updates_queue_dict  # session_id -> queue mapping
        self.formatter = logging.Formatter('%(message)s')
        
    def emit(self, record):
        """Emit a log record only to the appropriate session's queue."""
        try:
            # Get the current session ID from thread context
            current_session_id = get_current_session_id()
            
            if not current_session_id:
                return  # No session context, don't stream
                
            # Only stream if we have a queue for this session
            if current_session_id not in self.updates_queue_dict:
                return
                
            session_queue = self.updates_queue_dict[current_session_id]
            log_message = self.format(record)
            
            # Filter messages similar to original logic
            if self._should_stream_message(log_message, record):
                stream_type = self._determine_stream_type(record, log_message)
                
                # Import here to avoid circular imports
                try:
                    from streaming import StreamingManager
                    
                    StreamingManager.send_update(
                        session_queue,
                        {
                            "message": log_message,
                            "stream_type": stream_type,
                            "session_id": current_session_id,
                            "level": record.levelname,
                            "logger_name": record.name
                        },
                        "process_output"
                    )
                except ImportError:
                    # StreamingManager not available, skip streaming
                    pass
                
        except Exception:
            # Avoid infinite recursion
            pass
    
    def _should_stream_message(self, message: str, record) -> bool:
        """Determine if a log message should be streamed to the UI."""
        if record.levelno < logging.INFO:
            return False
            
        skip_patterns = [
            "HTTP Request:", "Request URL:", "Response status:",
            "127.0.0.1 - -", "WARNING: This is a development server",
            "Heartbeat received", "Added update to queue:",
            "Delivering immediate result", "streaming"
        ]
        
        for pattern in skip_patterns:
            if pattern in message:
                return False
        
        interesting_patterns = [
            "✅", "❌", "🔍", "📊", "🚀", "Connected to model",
            "Workspace:", "Dataset:", "Executing DAX query:",
            "Starting", "Successfully", "Error executing", "Failed"
        ]
        
        for pattern in interesting_patterns:
            if pattern in message:
                return True
                
        if record.levelno >= logging.WARNING:
            return True
        
        return False
    
    def _determine_stream_type(self, record, message: str) -> str:
        """Determine the appropriate stream type for a log message."""
        if record.levelno >= logging.ERROR:
            return "process_stderr"
        elif record.levelno == logging.WARNING:
            return "process_stderr" 
        elif "✅" in message or "Successfully" in message:
            return "command_success"
        elif "❌" in message or "Error" in message or "Failed" in message:
            return "command_error"
        elif "🚀" in message or "Starting" in message:
            return "command_start"
        else:
            return "process_stdout"


class UIProgressTracker:
    """
    Centralized UI progress tracking system for all analysis functions.
    Provides consistent progress reporting, batch tracking, and UI updates.
    """
    
    def __init__(self, progress_callback: Optional[Callable] = None, session_id: str = None):
        """
        Initialize the UI Progress Tracker with session awareness.
        
        Args:
            progress_callback: Optional callback function for sending progress updates to UI
            session_id: Unique identifier for this analysis session
        """
        self.progress_callback = progress_callback
        self.session_id = session_id or f"session_{int(time.time())}"
        
        # Set session context for this thread
        set_session_context(self.session_id)
        
        # Progress tracking state
        self.analysis_name = ""
        self.start_time = None
        self.total_items = 0
        self.current_item = 0
        self.phase_name = ""
        
        # Batch tracking
        self.current_batch = 0
        self.total_batches = 0
        self.batch_statistics = []
        
        # Item tracking (tables, columns, etc.)
        self.processed_items = set()
        self.item_statistics = {
            "total_processed": 0,
            "tables_processed": set(),
            "columns_processed": 0,
            "batches_completed": 0
        }
        
        # Status tracking
        self.is_active = False
        self.has_errors = False
        self.error_messages = []
        
        # Create session-aware logger
        self.logger = logging.getLogger(f"ui_progress_tracker.{self.session_id}")
        self.logger.info(f"🎯 UIProgressTracker initialized for session: {self.session_id}")
    
    def start_analysis(self, analysis_name: str, total_items: int = 0, total_batches: int = 0) -> None:
        """
        Start a new analysis with progress tracking.
        Ensures session context is set for the current thread.
        
        Args:
            analysis_name: Human-readable name of the analysis
            total_items: Total number of items to process (columns, tables, etc.)
            total_batches: Total number of batches to process
        """
        # Ensure session context is set
        set_session_context(self.session_id)
        
        self.analysis_name = analysis_name
        self.total_items = total_items
        self.total_batches = total_batches
        self.start_time = datetime.now()
        self.is_active = True
        
        # Reset counters
        self.current_item = 0
        self.current_batch = 0
        self.processed_items.clear()
        self.batch_statistics.clear()
        self.item_statistics = {
            "total_processed": 0,
            "tables_processed": set(),
            "columns_processed": 0,
            "batches_completed": 0
        }
        
        start_message = f"🚀 Starting {analysis_name}"
        if total_items > 0:
            start_message += f" - {total_items} items"
        if total_batches > 0:
            start_message += f" in {total_batches} batches"
        
        self.logger.info(start_message)
        self._send_progress_update(start_message, 0, max(total_items, total_batches))
        
        # Send detailed start notification
        self._send_detailed_update("analysis_start", {
            "analysis_name": analysis_name,
            "total_items": total_items,
            "total_batches": total_batches,
            "session_id": self.session_id,
            "start_time": self.start_time.isoformat()
        })
    
    def set_phase(self, phase_name: str, description: str = "") -> None:
        """
        Set the current analysis phase with session context.
        
        Args:
            phase_name: Name of the current phase
            description: Optional detailed description
        """
        set_session_context(self.session_id)
        
        self.phase_name = phase_name
        phase_message = f"📊 PHASE: {phase_name}"
        if description:
            phase_message += f" - {description}"
        
        self.logger.info(phase_message)
        self._send_progress_update(phase_message, self.current_item, self.total_items)
        
        # Send phase change notification
        self._send_detailed_update("phase_change", {
            "phase_name": phase_name,
            "description": description,
            "current_progress": self._calculate_percentage()
        })
    
    def update_progress(self, message: str, current: int = None, total: int = None, 
                       table_name: str = None, extra_data: Dict[str, Any] = None) -> None:
        """
        Update progress with session context maintained.
        
        Args:
            message: Progress message to display
            current: Current progress number (optional)
            total: Total progress number (optional)
            table_name: Name of table being processed (optional)
            extra_data: Additional data to include in detailed update
        """
        set_session_context(self.session_id)
        
        if current is not None:
            self.current_item = current
        if total is not None:
            self.total_items = total
        
        # Track table processing
        if table_name:
            self.item_statistics["tables_processed"].add(table_name)
        
        percentage = self._calculate_percentage()
        progress_message = f"📊 PROGRESS: {message} ({percentage}%)"
        
        self.logger.info(progress_message)
        self._send_progress_update(message, self.current_item, self.total_items)
        
        # Send detailed progress update
        update_data = {
            "message": message,
            "current": self.current_item,
            "total": self.total_items,
            "percentage": percentage,
            "phase": self.phase_name,
            "table_name": table_name,
            "session_id": self.session_id  # Explicitly include session ID
        }
        if extra_data:
            update_data.update(extra_data)
        
        self._send_detailed_update("progress_update", update_data)
    
    def complete_batch(self, batch_number: int, total_batches: int, 
                      columns_processed: int = 0, tables_processed: int = 0,
                      table_names: List[str] = None, batch_data: Dict[str, Any] = None) -> None:
        """
        Complete batch with session context.
        
        Args:
            batch_number: Number of the completed batch
            total_batches: Total number of batches
            columns_processed: Number of columns processed in this batch
            tables_processed: Number of tables processed in this batch
            table_names: List of table names processed in this batch
            batch_data: Additional batch-specific data
        """
        set_session_context(self.session_id)
        
        self.current_batch = batch_number
        self.total_batches = total_batches
        self.item_statistics["batches_completed"] += 1
        self.item_statistics["columns_processed"] += columns_processed
        
        # Track table names
        if table_names:
            for table_name in table_names:
                self.item_statistics["tables_processed"].add(table_name)
        
        # Create batch statistics entry
        batch_stats = {
            "batch_number": batch_number,
            "total_batches": total_batches,
            "columns_processed": columns_processed,
            "tables_processed": tables_processed,
            "table_names": table_names or [],
            "completion_time": datetime.now().isoformat(),
            "batch_percentage": round((batch_number / total_batches) * 100, 1) if total_batches > 0 else 0,
            "session_id": self.session_id  # Include session ID
        }
        if batch_data:
            batch_stats.update(batch_data)
        
        self.batch_statistics.append(batch_stats)
        
        # Calculate batch-level summary statistics (like final analysis results)
        batch_duration = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
        batch_completion_percentage = round((batch_number / total_batches) * 100, 1) if total_batches > 0 else 0
        total_columns_so_far = self.item_statistics["columns_processed"]
        total_tables_so_far = len(self.item_statistics["tables_processed"])
        
        # Create batch summary (similar to final analysis results)
        batch_summary = {
            "batch_number": batch_number,
            "total_batches": total_batches,
            "batch_completion_percentage": batch_completion_percentage,
            "duration_so_far": self._format_duration(batch_duration),
            "duration_seconds": batch_duration,
            "columns_processed_this_batch": columns_processed,
            "tables_processed_this_batch": tables_processed,
            "total_columns_processed": total_columns_so_far,
            "total_tables_processed": total_tables_so_far,
            "batches_completed": self.item_statistics["batches_completed"],
            "table_names_this_batch": table_names or [],
            "all_tables_processed": list(self.item_statistics["tables_processed"]),
            "has_errors": self.has_errors,
            "error_count": len(self.error_messages),
            "analysis_name": self.analysis_name,
            "current_phase": self.phase_name,
            "session_id": self.session_id  # Include session ID
        }
        
        # Log batch completion with rich summary
        batch_message = f"✅ BATCH COMPLETED: {batch_number}/{total_batches}"
        if columns_processed > 0:
            batch_message += f" - {columns_processed} columns"
        if tables_processed > 0:
            batch_message += f" from {tables_processed} tables"
        if table_names:
            batch_message += f": {', '.join(table_names[:3])}"
            if len(table_names) > 3:
                batch_message += f" + {len(table_names) - 3} more"
        
        self.logger.info(batch_message)
        self.logger.info(f"📊 BATCH SUMMARY: {batch_completion_percentage}% complete - {total_columns_so_far} total columns from {total_tables_so_far} tables ({self._format_duration(batch_duration)})")
        
        self._send_progress_update(batch_message, batch_number, total_batches)
        
        # Send detailed batch completion with original data
        self._send_detailed_update("batch_completion", batch_stats)
        
        # Send new batch summary (like final analysis results but for this batch)
        self._send_detailed_update("batch_summary", batch_summary)
    
    def add_error(self, error_message: str, error_type: str = "general") -> None:
        """
        Add error with session context.
        
        Args:
            error_message: Description of the error
            error_type: Type of error (general, ai_error, connection_error, etc.)
        """
        set_session_context(self.session_id)
        
        self.has_errors = True
        error_data = {
            "message": error_message,
            "type": error_type,
            "timestamp": datetime.now().isoformat(),
            "phase": self.phase_name,
            "session_id": self.session_id
        }
        self.error_messages.append(error_data)
        
        self.logger.error(f"❌ ERROR: {error_message}")
        self._send_detailed_update("error", error_data)
    
    def complete_analysis(self, success: bool = True, summary: str = "", 
                         results_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Complete analysis with session context.
        
        Args:
            success: Whether the analysis completed successfully
            summary: Summary message for the completion
            results_data: Additional results data to include
        
        Returns:
            Dictionary containing comprehensive analysis statistics
        """
        set_session_context(self.session_id)
        
        self.is_active = False
        end_time = datetime.now()
        duration = (end_time - self.start_time).total_seconds() if self.start_time else 0
        
        # Calculate final statistics
        final_stats = {
            "analysis_name": self.analysis_name,
            "session_id": self.session_id,
            "success": success,
            "summary": summary,
            "duration_seconds": duration,
            "duration_formatted": self._format_duration(duration),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": end_time.isoformat(),
            "total_items_processed": self.current_item,
            "total_items_planned": self.total_items,
            "completion_percentage": self._calculate_percentage(),
            "batches_completed": len(self.batch_statistics),
            "total_batches_planned": self.total_batches,
            "tables_processed": len(self.item_statistics["tables_processed"]),
            "columns_processed": self.item_statistics["columns_processed"],
            "has_errors": self.has_errors,
            "error_count": len(self.error_messages),
            "batch_statistics": self.batch_statistics,
            "item_statistics": {
                "total_processed": self.item_statistics["total_processed"],
                "tables_processed": list(self.item_statistics["tables_processed"]),
                "columns_processed": self.item_statistics["columns_processed"],
                "batches_completed": self.item_statistics["batches_completed"]
            }
        }
        
        if results_data:
            final_stats["results"] = results_data
        
        # Log completion
        status_emoji = "✅" if success else "❌"
        completion_message = f"{status_emoji} ANALYSIS COMPLETE: {self.analysis_name}"
        if summary:
            completion_message += f" - {summary}"
        completion_message += f" (Duration: {self._format_duration(duration)})"
        
        self.logger.info(completion_message)
        self._send_progress_update(completion_message, self.total_items, self.total_items)
        
        # Send final completion notification
        self.logger.info(f"🎯 CRITICAL: About to send analysis_completion event for: {self.analysis_name}")
        self.logger.info(f"🎯 Completion data summary: success={success}, duration={self._format_duration(duration)}")
        self._send_detailed_update("analysis_completion", final_stats)
        self.logger.info(f"🎯 CRITICAL: analysis_completion event sent successfully")
        
        return final_stats
    
    def _calculate_percentage(self) -> float:
        """Calculate current progress percentage."""
        if self.total_items > 0:
            return round((self.current_item / self.total_items) * 100, 1)
        elif self.total_batches > 0:
            return round((self.current_batch / self.total_batches) * 100, 1)
        return 0.0
    
    def _format_duration(self, seconds: float) -> str:
        """Format duration in human-readable format."""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
    
    def _send_progress_update(self, message: str, current: int, total: int) -> None:
        """Send progress update via callback if available."""
        if self.progress_callback and callable(self.progress_callback):
            try:
                percentage = round((current / total) * 100, 1) if total > 0 else 0
                self.progress_callback({
                    "type": "analysis_progress",
                    "message": message,
                    "current": current,
                    "total": total,
                    "percentage": percentage,
                    "session_id": self.session_id,
                    "analysis_name": self.analysis_name,
                    "phase": self.phase_name
                })
            except Exception as e:
                self.logger.warning(f"Failed to send progress callback: {str(e)}")
    
    def _send_detailed_update(self, update_type: str, data: Dict[str, Any]) -> None:
        """Send detailed update via callback with session verification."""
        # Ensure session ID is in the data
        if "session_id" not in data:
            data["session_id"] = self.session_id
            
        critical_events = ["analysis_start", "analysis_completion", "error"]
        
        if update_type in critical_events:
            self.logger.info(f"🔄 UIProgressTracker: Sending detailed update - Type: {update_type}")
            self.logger.debug(f"🔄 Update data: {data}")
        
        if self.progress_callback and callable(self.progress_callback):
            try:
                update_payload = {
                    "type": update_type,
                    "data": data,
                    "session_id": self.session_id,
                    "timestamp": int(time.time() * 1000)
                }
                if update_type in critical_events:
                    self.logger.info(f"📤 Sending {update_type} event via callback")
                self.progress_callback(update_payload)
                if update_type in critical_events:
                    self.logger.info(f"✅ Successfully sent {update_type} event")
            except Exception as e:
                self.logger.error(f"❌ Failed to send detailed update callback ({update_type}): {str(e)}")
        else:
            # Only warn about missing callbacks for truly critical events
            if update_type in critical_events:
                self.logger.debug(f"⚠️ No progress callback available for {update_type} event")
            else:
                # Optional events (batch_summary, phase_change, etc.) - silent
                self.logger.debug(f"🔇 Optional event {update_type} - no callback (this is normal)")


class BatchProgressTracker:
    """
    Session-aware batch progress tracker.
    """
    
    def __init__(self, ui_tracker: UIProgressTracker, batch_size: int = 15):
        """
        Initialize batch progress tracker with session awareness.
        
        Args:
            ui_tracker: Parent UI progress tracker
            batch_size: Size of each batch
        """
        self.ui_tracker = ui_tracker
        self.batch_size = batch_size
        self.current_batch_items = []
        self.current_batch_tables = set()
        # Inherit session ID from parent tracker
        self.session_id = ui_tracker.session_id
    
    def start_batch(self, batch_number: int, total_batches: int, batch_items: List[Any]) -> None:
        """Start processing a new batch with session context."""
        # Ensure session context is maintained
        set_session_context(self.session_id)
        
        self.current_batch_items = batch_items
        self.current_batch_tables.clear()
        
        # Extract table names from batch items if they have table_name attribute
        for item in batch_items:
            if hasattr(item, 'get') and item.get('table_name'):
                self.current_batch_tables.add(item['table_name'])
            elif hasattr(item, 'table_name'):
                self.current_batch_tables.add(item.table_name)
        
        # Generate consistent batch processing message
        batch_message = f"Processing batch {batch_number} of {total_batches} ({len(batch_items)} items)"
            
        self.ui_tracker.update_progress(
            batch_message,
            current=batch_number,
            total=total_batches,
            extra_data={
                "batch_size": len(batch_items),
                "batch_tables": list(self.current_batch_tables),
                "session_id": self.session_id
            }
        )
    
    def complete_batch(self, batch_number: int, total_batches: int, 
                      processing_results: List[Any] = None) -> None:
        """Complete the current batch with session context."""
        set_session_context(self.session_id)
        
        columns_processed = len(self.current_batch_items)
        tables_processed = len(self.current_batch_tables)
        table_names = list(self.current_batch_tables)
        
        # Extract additional statistics from processing results if available
        batch_data = {"session_id": self.session_id}
        if processing_results:
            batch_data["results_count"] = len(processing_results)
            batch_data["processing_successful"] = True
        
        self.ui_tracker.complete_batch(
            batch_number=batch_number,
            total_batches=total_batches,
            columns_processed=columns_processed,
            tables_processed=tables_processed,
            table_names=table_names,
            batch_data=batch_data
        )


# Session management utilities
def initialize_session_logging(session_id: str, workflow_updates_queue_dict: Dict[str, Any]):
    """
    Initialize session-isolated logging for a specific session.
    
    Args:
        session_id: Unique session identifier
        workflow_updates_queue_dict: Dictionary mapping session IDs to their update queues
    """
    # Set session context for current thread
    set_session_context(session_id)
    
    # Create or get session-aware log handler
    handler_name = f"session_handler_{session_id}"
    
    # Remove any existing handler for this session
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        if hasattr(handler, 'name') and handler.name == handler_name:
            root_logger.removeHandler(handler)
    
    # Create new session-aware handler
    session_handler = SessionAwareLogHandler(workflow_updates_queue_dict)
    session_handler.name = handler_name
    session_handler.setLevel(logging.INFO)
    
    # Add to specific loggers (not root to avoid duplication)
    target_loggers = ['src.server', 'ui_progress_tracker']
    for logger_name in target_loggers:
        logger_instance = logging.getLogger(logger_name)
        logger_instance.addHandler(session_handler)
        logger_instance.setLevel(logging.INFO)
    
    return session_handler


def cleanup_session_logging(session_id: str):
    """
    Clean up session-specific logging handlers.
    
    Args:
        session_id: Session identifier to clean up
    """
    handler_name = f"session_handler_{session_id}"
    
    # Remove from all loggers
    all_loggers = [logging.getLogger()] + [logging.getLogger(name) for name in logging.Logger.manager.loggerDict]
    
    for logger_instance in all_loggers:
        for handler in logger_instance.handlers[:]:
            if hasattr(handler, 'name') and handler.name == handler_name:
                logger_instance.removeHandler(handler)


# Session-aware factory functions
def create_analysis_tracker(progress_callback: Optional[Callable] = None, 
                          session_id: str = None) -> UIProgressTracker:
    """
    Factory function to create a session-aware UI progress tracker.
    
    Args:
        progress_callback: Optional callback function for UI updates
        session_id: Session identifier (required for proper isolation)
    
    Returns:
        Configured UIProgressTracker instance with session awareness
    """
    if not session_id:
        session_id = f"session_{int(time.time())}"
    
    return UIProgressTracker(progress_callback, session_id)


def create_batch_tracker(ui_tracker: UIProgressTracker, 
                        batch_size: int = 15) -> BatchProgressTracker:
    """
    Factory function to create a session-aware batch progress tracker.
    
    Args:
        ui_tracker: Parent UI progress tracker (must have session_id)
        batch_size: Size of each batch
    
    Returns:
        Configured BatchProgressTracker instance with session awareness
    """
    return BatchProgressTracker(ui_tracker, batch_size)