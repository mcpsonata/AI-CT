"""
Process Output Streaming Module
Captures stdout/stderr from subprocesses and streams them in real-time to the webapp.
"""

import subprocess
import threading
import queue
import logging
import time
from typing import Callable, Optional, Dict, Any
from streaming import StreamingManager

logger = logging.getLogger(__name__)

class ProcessStreamer:
    """Handles real-time streaming of process output to webapp UI."""
    
    def __init__(self, updates_queue: queue.Queue, session_id: str = None):
        self.updates_queue = updates_queue
        self.session_id = session_id
        self.process = None
        self.output_threads = []
        
    def run_command_with_streaming(
        self, 
        command: str, 
        shell: bool = True, 
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Run a command and stream its output in real-time.
        
        Args:
            command: The command to execute
            shell: Whether to run in shell mode
            cwd: Working directory for the command
            env: Environment variables
            
        Returns:
            Dict containing process info and final result
        """
        try:
            logger.info(f"Starting command with streaming: {command}")
            
            # Send initial command start message
            self._send_output_message(f"🚀 Starting command: {command}", "command_start")
            
            # Start the process
            self.process = subprocess.Popen(
                command,
                shell=shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1,  # Line buffered
                cwd=cwd,
                env=env
            )
            
            # Start threads to capture stdout and stderr
            stdout_thread = threading.Thread(
                target=self._stream_output,
                args=(self.process.stdout, "stdout"),
                daemon=True
            )
            stderr_thread = threading.Thread(
                target=self._stream_output,
                args=(self.process.stderr, "stderr"),
                daemon=True
            )
            
            self.output_threads = [stdout_thread, stderr_thread]
            stdout_thread.start()
            stderr_thread.start()
            
            # Wait for process to complete
            return_code = self.process.wait()
            
            # Wait for output threads to finish
            for thread in self.output_threads:
                thread.join(timeout=5)  # 5 second timeout
            
            # Send completion message
            if return_code == 0:
                self._send_output_message(f"✅ Command completed successfully (exit code: {return_code})", "command_success")
            else:
                self._send_output_message(f"❌ Command failed with exit code: {return_code}", "command_error")
            
            return {
                "success": return_code == 0,
                "return_code": return_code,
                "command": command,
                "session_id": self.session_id
            }
            
        except Exception as e:
            error_msg = f"Error running command: {str(e)}"
            logger.error(error_msg)
            self._send_output_message(f"❌ {error_msg}", "command_error")
            return {
                "success": False,
                "error": str(e),
                "command": command,
                "session_id": self.session_id
            }
    
    def _stream_output(self, pipe, stream_type: str):
        """Stream output from stdout or stderr pipe."""
        try:
            for line in iter(pipe.readline, ''):
                if line:
                    # Remove trailing newline but preserve formatting
                    clean_line = line.rstrip('\n\r')
                    if clean_line:  # Only send non-empty lines
                        self._send_output_message(clean_line, f"process_{stream_type}")
            
        except Exception as e:
            logger.error(f"Error streaming {stream_type}: {e}")
        finally:
            pipe.close()
    
    def _send_output_message(self, message: str, message_type: str):
        """Send a process output message to the streaming queue."""
        try:
            timestamp = int(time.time() * 1000)
            
            # Create formatted message data
            output_data = {
                "message": message,
                "timestamp": timestamp,
                "stream_type": message_type,
                "session_id": self.session_id
            }
            
            # Send via StreamingManager
            StreamingManager.send_update(
                self.updates_queue,
                output_data,
                "process_output"  # New event type for process output
            )
            
            logger.debug(f"Sent process output: {message_type} - {message[:100]}...")
            
        except Exception as e:
            logger.error(f"Error sending output message: {e}")
    
    def terminate_process(self):
        """Terminate the running process if it exists."""
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                # Give it a moment to terminate gracefully
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Force kill if it doesn't terminate gracefully
                    self.process.kill()
                    self.process.wait()
                
                self._send_output_message("⚠️ Process terminated by user", "command_terminated")
                
            except Exception as e:
                logger.error(f"Error terminating process: {e}")

class ProcessStreamManager:
    """Manages multiple process streams for different sessions."""
    
    _active_streamers = {}  # session_id -> ProcessStreamer
    
    @classmethod
    def create_streamer(cls, updates_queue: queue.Queue, session_id: str) -> ProcessStreamer:
        """Create a new process streamer for a session."""
        streamer = ProcessStreamer(updates_queue, session_id)
        cls._active_streamers[session_id] = streamer
        return streamer
    
    @classmethod
    def get_streamer(cls, session_id: str) -> Optional[ProcessStreamer]:
        """Get existing streamer for a session."""
        return cls._active_streamers.get(session_id)
    
    @classmethod
    def terminate_session(cls, session_id: str):
        """Terminate all processes for a session."""
        streamer = cls._active_streamers.get(session_id)
        if streamer:
            streamer.terminate_process()
            del cls._active_streamers[session_id]
    
    @classmethod
    def cleanup_all(cls):
        """Terminate all active processes."""
        for session_id in list(cls._active_streamers.keys()):
            cls.terminate_session(session_id)