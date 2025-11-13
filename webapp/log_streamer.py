"""
Custom logging handler to stream log messages to the webapp UI in real-time.
"""

import logging
import queue
import time
from streaming import StreamingManager

class StreamingLogHandler(logging.Handler):
    """Custom logging handler that streams log messages to the webapp UI."""
    
    def __init__(self, updates_queue: queue.Queue, session_id: str = None):
        super().__init__()
        self.updates_queue = updates_queue
        self.session_id = session_id
        
        # Set format for the log messages
        formatter = logging.Formatter('%(message)s')
        self.setFormatter(formatter)
    
    def emit(self, record):
        """Emit a log record to the streaming queue."""
        try:
            # Format the log message
            log_message = self.format(record)
            
            # Only stream certain types of messages (filter out noise)
            if self._should_stream_message(log_message, record):
                # Determine the message type based on log level and content
                stream_type = self._determine_stream_type(record, log_message)
                
                # Send to streaming queue
                StreamingManager.send_update(
                    self.updates_queue,
                    {
                        "message": log_message,
                        "stream_type": stream_type,
                        "session_id": self.session_id,
                        "level": record.levelname,
                        "logger_name": record.name
                    },
                    "process_output"
                )
                
        except Exception:
            # Avoid infinite recursion if logging the error fails
            pass
    
    def _should_stream_message(self, message: str, record) -> bool:
        """Determine if a log message should be streamed to the UI."""
        # Skip very verbose/debug messages
        if record.levelno < logging.INFO:
            return False
            
        # Skip certain noisy messages
        skip_patterns = [
            "HTTP Request:",
            "Request URL:",
            "Response status:",
            "Response headers:",
            "Request headers:",
            "No body was attached",
            "A body is sent",
            "127.0.0.1 - -",  # Flask request logs
            "WARNING: This is a development server",
            "Press CTRL+C to quit",
            "Serving Flask app",
            "Debug mode:",
            "Running on http://",
            "Heartbeat received",
            "Added update to queue:",
            "Delivering immediate result"
        ]
        
        for pattern in skip_patterns:
            if pattern in message:
                return False
        
        # Stream messages that are likely to be interesting to users
        interesting_patterns = [
            "✅",  # Success indicators
            "❌",  # Error indicators  
            "🔍",  # Discovery messages
            "📊",  # Data messages
            "🚀",  # Start messages
            "Connected to model",
            "Workspace:",
            "Dataset:",
            "Executing DAX query:",
            "Returned",
            "rows",
            "Discovered",
            "Auto-discovered",
            "SQL endpoint",
            "Using driver:",
            "Disconnected from server",
            "Tool execution",
            "Starting",
            "Successfully",
            "Error executing",
            "Failed",
        ]
        
        for pattern in interesting_patterns:
            if pattern in message:
                return True
                
        # Also stream ERROR and WARNING level messages
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

class LogCapture:
    """Manages log capture and streaming for a session."""
    
    @staticmethod
    def start_log_streaming(updates_queue: queue.Queue, session_id: str) -> StreamingLogHandler:
        """Start capturing logs and streaming them to the UI."""
        # Create the streaming handler
        handler = StreamingLogHandler(updates_queue, session_id)
        handler.setLevel(logging.INFO)
        
        # Add to the root logger so we capture all logs
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        
        return handler
    
    @staticmethod
    def stop_log_streaming(handler: StreamingLogHandler):
        """Stop capturing logs."""
        if handler:
            root_logger = logging.getLogger()
            if handler in root_logger.handlers:
                root_logger.removeHandler(handler)