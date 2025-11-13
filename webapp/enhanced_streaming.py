"""
Enhanced Streaming Manager for Run All Mode

Manages multiple concurrent streams without conflicts and provides proper
message routing between different analyzers running in sequence.

Features:
- Multi-analyzer stream coordination
- Message routing without conflicts
- Clean analyzer transitions
- Priority message handling
- Stream isolation and cleanup
- Memory-efficient queue management

Usage:
    streaming_manager = EnhancedStreamingManager()
    streaming_manager.create_run_all_stream(main_session_id, analyzer_sessions)
    streaming_manager.route_message_to_correct_stream(session_id, message)
    streaming_manager.switch_to_next_analyzer(main_session_id)
"""

import time
import json
import queue
import threading
import logging
from typing import Dict, Any, List, Optional, Set
from datetime import datetime
from flask import Response
from dataclasses import dataclass, field

# Set up logging
logger = logging.getLogger(__name__)

@dataclass
class StreamInfo:
    """Information about an active stream."""
    stream_type: str  # "individual", "run_all"
    main_queue: queue.Queue
    analyzer_queues: Dict[int, queue.Queue] = field(default_factory=dict)
    analyzer_sessions: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    current_analyzer: int = 0
    message_routing: Dict[str, int] = field(default_factory=dict)
    created_at: datetime = field(default=datetime.now())
    last_activity: datetime = field(default=datetime.now())
    is_active: bool = True
    
class EnhancedStreamingManager:
    """
    Advanced streaming manager that handles multiple analyzer sessions
    without conflicts or message mixing.
    
    This class solves the queue conflicts and message routing issues that
    occurred in the old implementation when multiple analyzers were running
    in sequence during Run All mode.
    """
    
    def __init__(self):
        """Initialize Enhanced Streaming Manager."""
        self.active_streams: Dict[str, StreamInfo] = {}
        self.stream_lock = threading.Lock()
        self.message_buffer: Dict[str, List[Dict[str, Any]]] = {}
        self.cleanup_thread = None
        self.is_running = True
        
        # Start cleanup thread for inactive streams
        self._start_cleanup_thread()
        
        logger.info("🌊 EnhancedStreamingManager initialized")
    
    def create_run_all_stream(self, main_session_id: str, 
                            analyzer_sessions: Dict[int, Dict[str, Any]]) -> StreamInfo:
        """
        Create coordinated streaming for Run All mode.
        
        Args:
            main_session_id: Main session ID for Run All workflow
            analyzer_sessions: Dictionary of analyzer session information
            
        Returns:
            StreamInfo object for the created stream
        """
        with self.stream_lock:
            # Clean up any existing stream for this session
            if main_session_id in self.active_streams:
                self._cleanup_stream(main_session_id)
            
            # Create new stream info
            stream_info = StreamInfo(
                stream_type="run_all",
                main_queue=queue.Queue(maxsize=1000),  # Large queue for Run All
                analyzer_sessions=analyzer_sessions.copy(),
                current_analyzer=0
            )
            
            # Create separate queue for each analyzer to prevent mixing
            for idx, session_info in analyzer_sessions.items():
                analyzer_session_id = session_info.get("session_id", f"analyzer_{idx}")
                stream_info.analyzer_queues[idx] = queue.Queue(maxsize=500)
                
                # Set up message routing from analyzer session ID to analyzer index
                stream_info.message_routing[analyzer_session_id] = idx
                stream_info.message_routing[main_session_id] = -1  # Main session messages
            
            # Store stream info
            self.active_streams[main_session_id] = stream_info
            
            # Initialize message buffer
            self.message_buffer[main_session_id] = []
            
            logger.info(f"📡 Created Run All stream for session {main_session_id} "
                       f"with {len(analyzer_sessions)} analyzers")
            
            return stream_info
    
    def create_individual_stream(self, session_id: str) -> StreamInfo:
        """
        Create streaming for individual analyzer mode.
        
        Args:
            session_id: Session ID for individual analyzer
            
        Returns:
            StreamInfo object for the created stream
        """
        with self.stream_lock:
            # Clean up any existing stream
            if session_id in self.active_streams:
                self._cleanup_stream(session_id)
            
            # Create individual stream
            stream_info = StreamInfo(
                stream_type="individual",
                main_queue=queue.Queue(maxsize=500)
            )
            
            self.active_streams[session_id] = stream_info
            self.message_buffer[session_id] = []
            
            logger.info(f"📱 Created individual stream for session {session_id}")
            
            return stream_info
    
    def route_message_to_correct_stream(self, session_id: str, 
                                      message: Dict[str, Any]) -> bool:
        """
        Route messages to the correct stream queue without conflicts.
        
        Args:
            session_id: Session ID that sent the message
            message: Message data to route
            
        Returns:
            True if message was routed successfully, False otherwise
        """
        with self.stream_lock:
            # Find which stream this message belongs to
            target_stream = None
            target_session_id = None
            
            # Check all active streams
            for main_session_id, stream_info in self.active_streams.items():
                if not stream_info.is_active:
                    continue
                
                if stream_info.stream_type == "individual":
                    # Simple case: direct match
                    if session_id == main_session_id:
                        target_stream = stream_info
                        target_session_id = main_session_id
                        break
                        
                elif stream_info.stream_type == "run_all":
                    # Complex case: check if message belongs to this Run All workflow
                    if session_id == main_session_id:
                        # Main session message
                        target_stream = stream_info
                        target_session_id = main_session_id
                        break
                    elif session_id in stream_info.message_routing:
                        # Analyzer-specific message
                        target_stream = stream_info
                        target_session_id = main_session_id
                        break
            
            if not target_stream:
                logger.warning(f"⚠️ No active stream found for session {session_id}")
                return False
            
            # Route message based on stream type
            success = self._route_to_stream(target_stream, session_id, message, target_session_id)
            
            if success:
                # Update last activity
                target_stream.last_activity = datetime.now()
            
            return success
    
    def _route_to_stream(self, stream_info: StreamInfo, original_session_id: str,
                        message: Dict[str, Any], main_session_id: str) -> bool:
        """Route message to the appropriate queue within a stream."""
        try:
            # Add routing metadata to message
            enhanced_message = {
                **message,
                "routing_info": {
                    "original_session_id": original_session_id,
                    "main_session_id": main_session_id,
                    "stream_type": stream_info.stream_type,
                    "routed_at": datetime.now().isoformat()
                }
            }
            
            if stream_info.stream_type == "individual":
                # Simple routing for individual streams
                stream_info.main_queue.put(enhanced_message, timeout=1.0)
                
            elif stream_info.stream_type == "run_all":
                # Complex routing for Run All streams
                if original_session_id == main_session_id:
                    # Main session message - goes directly to main queue
                    enhanced_message["routing_info"]["message_source"] = "main_session"
                    stream_info.main_queue.put(enhanced_message, timeout=1.0)
                    
                elif original_session_id in stream_info.message_routing:
                    # Analyzer-specific message
                    analyzer_idx = stream_info.message_routing[original_session_id]
                    current_analyzer = stream_info.current_analyzer
                    
                    # Only route messages from the currently active analyzer
                    if analyzer_idx == current_analyzer:
                        enhanced_message["routing_info"]["message_source"] = "current_analyzer"
                        enhanced_message["routing_info"]["analyzer_index"] = analyzer_idx
                        enhanced_message["routing_info"]["analyzer_name"] = (
                            stream_info.analyzer_sessions.get(analyzer_idx, {}).get("analyzer_name", "Unknown")
                        )
                        
                        # Add analyzer context to message
                        enhanced_message["analyzer_context"] = {
                            "analyzer_index": analyzer_idx,
                            "analyzer_name": enhanced_message["routing_info"]["analyzer_name"],
                            "is_current_analyzer": True
                        }
                        
                        stream_info.main_queue.put(enhanced_message, timeout=1.0)
                    else:
                        # Message from inactive analyzer - buffer it or discard
                        logger.debug(f"🔇 Discarding message from inactive analyzer {analyzer_idx} "
                                   f"(current: {current_analyzer})")
                        # Could implement buffering here if needed
                        
                else:
                    logger.warning(f"⚠️ Unknown session ID in Run All routing: {original_session_id}")
                    return False
            
            return True
            
        except queue.Full:
            logger.error(f"❌ Queue full for session {main_session_id}, dropping message")
            return False
        except Exception as e:
            logger.error(f"❌ Error routing message: {e}")
            return False
    
    def switch_to_next_analyzer(self, main_session_id: str) -> bool:
        """
        Clean switch to next analyzer stream.
        
        Args:
            main_session_id: Main session ID for Run All workflow
            
        Returns:
            True if switch was successful, False otherwise
        """
        with self.stream_lock:
            if main_session_id not in self.active_streams:
                logger.error(f"❌ No active stream found for session {main_session_id}")
                return False
            
            stream_info = self.active_streams[main_session_id]
            
            if stream_info.stream_type != "run_all":
                logger.error(f"❌ Cannot switch analyzers for non-Run All stream: {main_session_id}")
                return False
            
            # Move to next analyzer
            old_analyzer = stream_info.current_analyzer
            stream_info.current_analyzer += 1
            
            # Check if we've reached the end
            if stream_info.current_analyzer >= len(stream_info.analyzer_sessions):
                logger.info(f"🏁 All analyzers complete for session {main_session_id}")
                # Don't increment beyond available analyzers
                stream_info.current_analyzer = len(stream_info.analyzer_sessions) - 1
                return True
            
            # Send transition message to indicate analyzer switch
            transition_message = {
                "type": "analyzer_transition",
                "from_analyzer": old_analyzer,
                "to_analyzer": stream_info.current_analyzer,
                "timestamp": datetime.now().isoformat(),
                "routing_info": {
                    "message_source": "system",
                    "main_session_id": main_session_id
                }
            }
            
            try:
                stream_info.main_queue.put(transition_message, timeout=1.0)
                logger.info(f"🔄 Switched from analyzer {old_analyzer} to {stream_info.current_analyzer} "
                           f"for session {main_session_id}")
                return True
                
            except queue.Full:
                logger.error(f"❌ Could not send transition message - queue full")
                return False
    
    def create_streaming_response(self, main_session_id: str) -> Response:
        """
        Create a Flask Response object for SSE streaming.
        
        Args:
            main_session_id: Session ID to create streaming response for
            
        Returns:
            Flask Response object for Server-Sent Events
        """
        if main_session_id not in self.active_streams:
            # Create individual stream if none exists
            self.create_individual_stream(main_session_id)
        
        return Response(
            self._stream_events(main_session_id),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',  # Disable buffering in Nginx
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Cache-Control'
            }
        )
    
    def _stream_events(self, main_session_id: str):
        """
        Generator function that yields SSE formatted events.
        
        Args:
            main_session_id: Session ID to stream events for
        """
        # Initial connection event
        yield self._format_sse(
            json.dumps({
                "type": "connection_established",
                "session_id": main_session_id,
                "timestamp": datetime.now().isoformat()
            }),
            event="connection"
        )
        
        # Send any buffered messages first
        if main_session_id in self.message_buffer:
            for buffered_message in self.message_buffer[main_session_id]:
                yield self._format_sse(
                    json.dumps(buffered_message),
                    event=self._get_event_type(buffered_message)
                )
            # Clear buffer after sending
            self.message_buffer[main_session_id] = []
        
        # Main streaming loop
        while main_session_id in self.active_streams:
            stream_info = self.active_streams[main_session_id]
            
            if not stream_info.is_active:
                break
            
            try:
                # Get message from queue with short timeout for responsiveness
                message = stream_info.main_queue.get(timeout=0.5)
                
                # Determine event type
                event_type = self._get_event_type(message)
                
                # Add stream metadata
                message["stream_metadata"] = {
                    "session_id": main_session_id,
                    "stream_type": stream_info.stream_type,
                    "current_analyzer": stream_info.current_analyzer if stream_info.stream_type == "run_all" else None,
                    "delivery_time": datetime.now().isoformat()
                }
                
                # Format and yield the message
                yield self._format_sse(
                    json.dumps(message),
                    event=event_type
                )
                
                # Mark task as done
                stream_info.main_queue.task_done()
                
                # Check if this is a completion message
                if message.get("type") in ["workflow_complete", "run_all_complete"]:
                    logger.info(f"🏁 Workflow complete for session {main_session_id}, ending stream")
                    break
                    
            except queue.Empty:
                # No messages available, send heartbeat
                yield self._format_sse(
                    json.dumps({
                        "type": "heartbeat",
                        "timestamp": datetime.now().isoformat(),
                        "session_id": main_session_id
                    }),
                    event="heartbeat"
                )
                
            except Exception as e:
                logger.error(f"❌ Error in streaming for session {main_session_id}: {e}")
                yield self._format_sse(
                    json.dumps({
                        "type": "stream_error",
                        "message": str(e),
                        "timestamp": datetime.now().isoformat()
                    }),
                    event="error"
                )
                break
        
        # Clean up stream when done
        self._cleanup_stream(main_session_id)
    
    def _get_event_type(self, message: Dict[str, Any]) -> str:
        """Determine the appropriate SSE event type for a message."""
        message_type = message.get("type", "update")
        
        # Map message types to SSE event types
        event_mapping = {
            "workflow_start": "workflow_update",
            "workflow_complete": "workflow_update", 
            "run_all_start": "run_all_update",
            "run_all_complete": "run_all_update",
            "analyzer_start": "analyzer_update",
            "analyzer_complete": "analyzer_update",
            "analyzer_progress": "analyzer_update",
            "analyzer_transition": "analyzer_update",
            "analyzer_error": "error",
            "ai_processing_start": "progress_update",
            "ai_processing_progress": "progress_update",
            "ai_processing_complete": "progress_update",
            "batch_complete": "progress_update",
            "detailed_logs": "detailed_logs",
            "process_output": "process_output",
            "heartbeat": "heartbeat",
            "stream_error": "error"
        }
        
        return event_mapping.get(message_type, "update")
    
    def _format_sse(self, data: str, event: Optional[str] = None) -> str:
        """Format data as a Server-Sent Event."""
        msg = f"data: {data}\n\n"
        if event:
            msg = f"event: {event}\n{msg}"
        return msg
    
    def _cleanup_stream(self, session_id: str) -> None:
        """Clean up resources for a stream."""
        if session_id in self.active_streams:
            stream_info = self.active_streams[session_id]
            stream_info.is_active = False
            
            # Clear queues
            try:
                while not stream_info.main_queue.empty():
                    stream_info.main_queue.get_nowait()
                    stream_info.main_queue.task_done()
                    
                for analyzer_queue in stream_info.analyzer_queues.values():
                    while not analyzer_queue.empty():
                        analyzer_queue.get_nowait()
                        analyzer_queue.task_done()
                        
            except Exception as e:
                logger.error(f"❌ Error clearing queues for session {session_id}: {e}")
            
            # Remove from active streams
            del self.active_streams[session_id]
            
            # Clear message buffer
            if session_id in self.message_buffer:
                del self.message_buffer[session_id]
            
            logger.info(f"🧹 Cleaned up stream for session {session_id}")
    
    def _start_cleanup_thread(self) -> None:
        """Start background thread for cleaning up inactive streams."""
        def cleanup_worker():
            while self.is_running:
                try:
                    current_time = datetime.now()
                    inactive_sessions = []
                    
                    with self.stream_lock:
                        for session_id, stream_info in self.active_streams.items():
                            # Mark streams inactive after 1 hour of no activity
                            if (current_time - stream_info.last_activity).total_seconds() > 3600:
                                inactive_sessions.append(session_id)
                    
                    # Clean up inactive sessions
                    for session_id in inactive_sessions:
                        logger.info(f"🧹 Cleaning up inactive stream: {session_id}")
                        self._cleanup_stream(session_id)
                    
                    time.sleep(300)  # Check every 5 minutes
                    
                except Exception as e:
                    logger.error(f"❌ Error in cleanup thread: {e}")
                    time.sleep(60)  # Wait 1 minute before retrying
        
        self.cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
        self.cleanup_thread.start()
        logger.info("🧹 Started cleanup thread for inactive streams")
    
    def send_update(self, session_id: str, data: Dict[str, Any], 
                   event_type: str = "update") -> bool:
        """
        Send an update to the specified session.
        
        Args:
            session_id: Target session ID
            data: Data to send
            event_type: Type of event
            
        Returns:
            True if update was sent successfully
        """
        update = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            "data": data
        }
        
        # Add immediate delivery flag for priority messages
        if event_type in ["tool_success", "debug_success", "analyzer_complete"]:
            update["immediate_delivery"] = True
        
        return self.route_message_to_correct_stream(session_id, update)
    
    def shutdown(self) -> None:
        """Shutdown the streaming manager and clean up all resources."""
        logger.info("🛑 Shutting down EnhancedStreamingManager")
        
        self.is_running = False
        
        # Clean up all active streams
        with self.stream_lock:
            session_ids = list(self.active_streams.keys())
            for session_id in session_ids:
                self._cleanup_stream(session_id)
        
        # Wait for cleanup thread to finish
        if self.cleanup_thread and self.cleanup_thread.is_alive():
            self.cleanup_thread.join(timeout=5)
        
        logger.info("✅ EnhancedStreamingManager shutdown complete")


# Global instance (singleton pattern)
_streaming_manager_instance = None

def get_enhanced_streaming_manager() -> EnhancedStreamingManager:
    """Get the global enhanced streaming manager instance."""
    global _streaming_manager_instance
    if _streaming_manager_instance is None:
        _streaming_manager_instance = EnhancedStreamingManager()
    return _streaming_manager_instance