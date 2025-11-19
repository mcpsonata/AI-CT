"""
Streaming module for real-time updates in the AI Compliance Tool.
Implements Server-Sent Events (SSE) for streaming workflow steps and results.
"""

import json
import time
import logging
from flask import Response
from typing import Dict, Any, Iterator, List, Optional
import queue

# Set up logging
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class StreamingManager:
    """Manages streaming of events to the client."""
    
    @staticmethod
    def format_sse(data: str, event: Optional[str] = None) -> str:
        """Format data as a Server-Sent Event."""
        msg = f"data: {data}\n\n"
        if event is not None:
            msg = f"event: {event}\n{msg}"
        return msg
    
    @staticmethod
    def stream_workflow_steps(updates_queue: queue.Queue) -> Iterator[str]:
        """Generator function that yields SSE formatted workflow steps."""
        # Initial event to establish the connection
        yield StreamingManager.format_sse(
            json.dumps({"type": "connection_established"}),
            event="connection"
        )
        
        # Return immediately to let the client know streaming has started
        yield StreamingManager.format_sse(
            json.dumps({"type": "workflow_start", "message": "Starting autonomous workflow..."}),
            event="workflow_update"
        )
        
        # Keep the connection open and stream updates as they become available
        while True:
            try:
                # Try to get an update from the queue with a very short timeout
                # for more responsive streaming of results
                update = updates_queue.get(timeout=0.1)  # Reduced timeout for faster response
                
                # Determine the appropriate SSE event type based on update type
                event_type = "workflow_update"  # default
                if update.get("type") == "process_output":
                    event_type = "process_output"
                elif update.get("type") == "detailed_logs":
                    event_type = "detailed_logs"
                elif update.get("type") in ["tool_success", "debug_success"]:
                    event_type = "workflow_update"
                elif update.get("type") == "error":
                    event_type = "error"
                elif update.get("type") == "heartbeat":
                    event_type = "heartbeat"
                elif update.get("type") == "progress_update":
                    event_type = "progress_update"
                elif update.get("type") == "connection_update":
                    event_type = "connection_update"
                
                # Format and yield the update
                if update.get("immediate_delivery"):
                    # Add a priority flag for immediate results
                    logger.info("Delivering immediate result to client")
                    
                yield StreamingManager.format_sse(
                    json.dumps(update),
                    event=event_type
                )
                
                # Mark the update as processed
                updates_queue.task_done()
                
                # If this is a completion message, break the loop
                if update.get("type") == "workflow_complete":
                    logger.info("Workflow complete, ending stream")
                    break
                    
            except queue.Empty:
                # No updates available, send a heartbeat to keep the connection alive
                yield StreamingManager.format_sse(
                    json.dumps({"type": "heartbeat", "timestamp": int(time.time() * 1000)}),
                    event="heartbeat"
                )
                time.sleep(1)  # Send heartbeat every 1 second for more responsive streaming
            
            except Exception as e:
                logger.error(f"Error in streaming: {e}")
                yield StreamingManager.format_sse(
                    json.dumps({"type": "error", "message": str(e)}),
                    event="error"
                )
                break
    
    @classmethod
    def create_streaming_response(cls, updates_queue: queue.Queue) -> Response:
        """Create a Flask Response object for SSE streaming."""
        return Response(
            cls.stream_workflow_steps(updates_queue),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no'  # Disable buffering in Nginx
            }
        )
    
    @staticmethod
    def send_update(updates_queue: queue.Queue, data: Dict[str, Any], event_type: str = "update") -> None:
        """Send an update to the specified event queue."""
        try:
            timestamp = int(time.time() * 1000)
            update = {
                "timestamp": timestamp,
                "type": event_type,
                "data": data
            }
            
            # Prioritize results with immediate_result flag for faster feedback
            if event_type in ["tool_success", "debug_success"] and data.get("result"):
                # Mark as immediate result for faster frontend processing
                update["immediate_delivery"] = True
                logger.info(f"Sending immediate result for: {event_type}")
            
            updates_queue.put(update)
            # Only log non-detailed_logs to avoid spam
            if event_type != "detailed_logs":
                logger.info(f"Added update to queue: {event_type}")
        except Exception as e:
            logger.error(f"Error sending update: {e}")
