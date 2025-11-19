"""
Run All Session Manager

Manages multiple analyzers running sequentially with proper session isolation.
Prevents conflicts between different analyzers and ensures clean transitions.

Features:
- Individual session management for each analyzer
- Progress tracking across multiple analyzers  
- Clean analyzer transitions without conflicts
- Overall Run All progress coordination
- Error handling and recovery for individual analyzers
- Memory management for long-running sequences

Usage:
    session_manager = RunAllSessionManager("main_session_123")
    session_manager.initialize_run_all(analyzer_list)
    
    while True:
        analyzer_info = session_manager.start_next_analyzer()
        if not analyzer_info:
            break
        # Execute analyzer...
        session_manager.complete_current_analyzer(results)
"""

import time
import logging
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, field

# Set up logging
logger = logging.getLogger(__name__)

@dataclass
class AnalyzerSessionInfo:
    """Information about a single analyzer session."""
    session_id: str
    analyzer_name: str  
    analyzer_function: str
    status: str = "pending"  # pending, running, completed, error
    progress: float = 0.0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration: Optional[float] = None
    results: Optional[Any] = None
    error_message: Optional[str] = None
    batch_info: Dict[str, Any] = field(default_factory=dict)
    statistics: Dict[str, Any] = field(default_factory=dict)

class RunAllSessionManager:
    """
    Manages the complete Run All workflow with proper session isolation.
    Prevents conflicts between multiple analyzers and ensures clean transitions.
    
    This class addresses the session management issues that caused confusion
    in the old implementation where multiple analyzers would interfere with
    each other's progress tracking and EventSource connections.
    """
    
    def __init__(self, main_session_id: str, progress_callback: Optional[Callable] = None):
        """
        Initialize Run All Session Manager.
        
        Args:
            main_session_id: Main session ID for the entire Run All workflow
            progress_callback: Optional callback for progress updates
        """
        self.main_session_id = main_session_id
        self.progress_callback = progress_callback
        self.analyzer_sessions: Dict[int, AnalyzerSessionInfo] = {}
        self.current_analyzer_index = 0
        self.total_analyzers = 0
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.overall_status = "pending"  # pending, running, completed, error
        self.overall_progress = 0.0
        self.failed_analyzers: List[int] = []
        self.completed_analyzers: List[int] = []
        
        logger.info(f"🎯 RunAllSessionManager initialized for main session: {main_session_id}")
    
    def initialize_run_all(self, analyzer_definitions: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        Initialize Run All session with multiple analyzers.
        
        Args:
            analyzer_definitions: List of analyzer definitions with name and function info
            
        Returns:
            Initialization summary with session information
        """
        self.total_analyzers = len(analyzer_definitions)
        self.start_time = datetime.now()
        self.overall_status = "running"
        self.current_analyzer_index = 0
        
        # Create isolated session for each analyzer
        for idx, analyzer_def in enumerate(analyzer_definitions):
            analyzer_name = analyzer_def.get("name", f"Analyzer_{idx}")
            analyzer_function = analyzer_def.get("function", "unknown_function")
            
            # Create unique session ID for this analyzer
            session_id = f"{self.main_session_id}_analyzer_{idx}_{analyzer_function}"
            
            self.analyzer_sessions[idx] = AnalyzerSessionInfo(
                session_id=session_id,
                analyzer_name=analyzer_name,
                analyzer_function=analyzer_function,
                status="pending",
                batch_info={
                    "total_batches": 0,
                    "completed_batches": 0,
                    "current_batch": 0
                },
                statistics={
                    "tables_processed": 0,
                    "columns_processed": 0,
                    "items_analyzed": 0,
                    "ai_calls_made": 0,
                    "errors_encountered": 0
                }
            )
        
        initialization_summary = {
            "main_session_id": self.main_session_id,
            "total_analyzers": self.total_analyzers,
            "analyzer_list": [
                {
                    "index": idx,
                    "name": info.analyzer_name,
                    "function": info.analyzer_function,
                    "session_id": info.session_id
                }
                for idx, info in self.analyzer_sessions.items()
            ],
            "start_time": self.start_time.isoformat(),
            "estimated_duration_minutes": self._estimate_total_duration()
        }
        
        logger.info(f"🚀 Run All initialized: {self.total_analyzers} analyzers, "
                   f"estimated duration: {initialization_summary['estimated_duration_minutes']:.1f} minutes")
        
        return initialization_summary
    
    def start_next_analyzer(self) -> Optional[AnalyzerSessionInfo]:
        """
        Start the next analyzer with proper session management.
        
        Returns:
            AnalyzerSessionInfo for the next analyzer, or None if all are complete
        """
        if self.current_analyzer_index >= self.total_analyzers:
            return None  # All analyzers complete
        
        analyzer_info = self.analyzer_sessions[self.current_analyzer_index]
        analyzer_info.status = "running"
        analyzer_info.start_time = datetime.now()
        
        # Calculate overall progress based on completed analyzers
        self.overall_progress = (self.current_analyzer_index / self.total_analyzers) * 100
        
        # Log analyzer start with context
        logger.info(f"▶️ Starting analyzer {self.current_analyzer_index + 1}/{self.total_analyzers}: "
                   f"{analyzer_info.analyzer_name} (Session: {analyzer_info.session_id})")
        
        # Send overall progress update  
        self._send_run_all_progress_update(
            f"Starting analyzer {self.current_analyzer_index + 1}/{self.total_analyzers}: {analyzer_info.analyzer_name}",
            self.overall_progress,
            analyzer_info,
            "analyzer_start"
        )
        
        return analyzer_info
    
    def update_analyzer_progress(self, progress: float, batch_info: Dict[str, Any] = None, 
                               statistics: Dict[str, Any] = None, message: str = "") -> None:
        """
        Update progress for the currently running analyzer.
        
        Args:
            progress: Progress percentage (0-100) for current analyzer
            batch_info: Batch processing information
            statistics: Processing statistics
            message: Progress message
        """
        if self.current_analyzer_index >= self.total_analyzers:
            return
        
        analyzer_info = self.analyzer_sessions[self.current_analyzer_index]
        analyzer_info.progress = progress
        
        # Update batch info if provided
        if batch_info:
            analyzer_info.batch_info.update(batch_info)
        
        # Update statistics if provided
        if statistics:
            analyzer_info.statistics.update(statistics)
        
        # Calculate combined overall progress
        # Completed analyzers contribute 100% each, current analyzer contributes its progress
        completed_contribution = self.current_analyzer_index * 100
        current_contribution = progress
        combined_progress = (completed_contribution + current_contribution) / self.total_analyzers
        
        self.overall_progress = min(100.0, combined_progress)
        
        # Send progress update
        progress_message = message or f"Analyzer {self.current_analyzer_index + 1}/{self.total_analyzers}: {analyzer_info.analyzer_name} ({progress:.1f}%)"
        
        self._send_run_all_progress_update(
            progress_message,
            self.overall_progress,
            analyzer_info,
            "analyzer_progress",
            {
                "analyzer_progress": progress,
                "batch_info": analyzer_info.batch_info,
                "statistics": analyzer_info.statistics
            }
        )
    
    def complete_current_analyzer(self, results: Any, statistics: Dict[str, Any] = None) -> bool:
        """
        Complete current analyzer and prepare for next.
        
        Args:
            results: Results from the completed analyzer
            statistics: Final statistics for the analyzer
            
        Returns:
            True if more analyzers remain, False if all are complete
        """
        if self.current_analyzer_index >= self.total_analyzers:
            return False
        
        analyzer_info = self.analyzer_sessions[self.current_analyzer_index]
        analyzer_info.status = "completed"
        analyzer_info.end_time = datetime.now()
        analyzer_info.results = results
        analyzer_info.progress = 100.0
        
        # Calculate duration
        if analyzer_info.start_time:
            analyzer_info.duration = (analyzer_info.end_time - analyzer_info.start_time).total_seconds()
        
        # Update final statistics
        if statistics:
            analyzer_info.statistics.update(statistics)
        
        # Add to completed list
        self.completed_analyzers.append(self.current_analyzer_index)
        
        # Calculate updated overall progress
        self.overall_progress = (len(self.completed_analyzers) / self.total_analyzers) * 100
        
        # Log completion
        duration_str = f"{analyzer_info.duration:.1f}s" if analyzer_info.duration else "unknown"
        logger.info(f"✅ Completed analyzer {self.current_analyzer_index + 1}/{self.total_analyzers}: "
                   f"{analyzer_info.analyzer_name} in {duration_str}")
        
        # Send completion update
        self._send_run_all_progress_update(
            f"✅ Completed analyzer {self.current_analyzer_index + 1}/{self.total_analyzers}: {analyzer_info.analyzer_name}",
            self.overall_progress,
            analyzer_info,
            "analyzer_complete",
            {
                "duration": analyzer_info.duration,
                "final_statistics": analyzer_info.statistics,
                "results_summary": self._summarize_results(results)
            }
        )
        
        # Move to next analyzer
        self.current_analyzer_index += 1
        
        # Check if all analyzers are complete
        if self.current_analyzer_index >= self.total_analyzers:
            self._complete_run_all_workflow()
            return False
        else:
            return True
    
    def handle_analyzer_error(self, error_message: str, error_details: Dict[str, Any] = None) -> bool:
        """
        Handle error in current analyzer.
        
        Args:
            error_message: Error description
            error_details: Additional error information
            
        Returns:
            True if should continue with next analyzer, False if should abort
        """
        if self.current_analyzer_index >= self.total_analyzers:
            return False
        
        analyzer_info = self.analyzer_sessions[self.current_analyzer_index]
        analyzer_info.status = "error"
        analyzer_info.end_time = datetime.now()
        analyzer_info.error_message = error_message
        
        # Calculate duration
        if analyzer_info.start_time:
            analyzer_info.duration = (analyzer_info.end_time - analyzer_info.start_time).total_seconds()
        
        # Add to failed list
        self.failed_analyzers.append(self.current_analyzer_index)
        
        # Log error
        logger.error(f"❌ Analyzer {self.current_analyzer_index + 1}/{self.total_analyzers} failed: "
                    f"{analyzer_info.analyzer_name} - {error_message}")
        
        # Send error update
        self._send_run_all_progress_update(
            f"❌ Analyzer {self.current_analyzer_index + 1}/{self.total_analyzers} failed: {error_message}",
            self.overall_progress,
            analyzer_info,
            "analyzer_error",
            {
                "error_message": error_message,
                "error_details": error_details or {},
                "duration": analyzer_info.duration
            }
        )
        
        # Move to next analyzer (continue despite error)
        self.current_analyzer_index += 1
        
        # Check if all analyzers are complete (including failed ones)
        if self.current_analyzer_index >= self.total_analyzers:
            self._complete_run_all_workflow()
            return False
        else:
            return True
    
    def get_current_analyzer_session_id(self) -> Optional[str]:
        """Get the session ID for the currently running analyzer."""
        if self.current_analyzer_index < self.total_analyzers:
            return self.analyzer_sessions[self.current_analyzer_index].session_id
        return None
    
    def get_run_all_summary(self) -> Dict[str, Any]:
        """Get comprehensive summary of the Run All execution."""
        total_duration = None
        if self.start_time and self.end_time:
            total_duration = (self.end_time - self.start_time).total_seconds()
        elif self.start_time:
            total_duration = (datetime.now() - self.start_time).total_seconds()
        
        # Aggregate statistics
        total_stats = {
            "tables_processed": sum(info.statistics.get("tables_processed", 0) for info in self.analyzer_sessions.values()),
            "columns_processed": sum(info.statistics.get("columns_processed", 0) for info in self.analyzer_sessions.values()),
            "items_analyzed": sum(info.statistics.get("items_analyzed", 0) for info in self.analyzer_sessions.values()),
            "ai_calls_made": sum(info.statistics.get("ai_calls_made", 0) for info in self.analyzer_sessions.values()),
            "errors_encountered": sum(info.statistics.get("errors_encountered", 0) for info in self.analyzer_sessions.values())
        }
        
        return {
            "main_session_id": self.main_session_id,
            "overall_status": self.overall_status,
            "overall_progress": self.overall_progress,
            "total_analyzers": self.total_analyzers,
            "completed_analyzers": len(self.completed_analyzers),
            "failed_analyzers": len(self.failed_analyzers),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "total_duration": total_duration,
            "analyzer_details": [
                {
                    "index": idx,
                    "name": info.analyzer_name,
                    "status": info.status,
                    "progress": info.progress,
                    "duration": info.duration,
                    "statistics": info.statistics,
                    "error_message": info.error_message
                }
                for idx, info in self.analyzer_sessions.items()
            ],
            "aggregated_statistics": total_stats
        }
    
    def _complete_run_all_workflow(self) -> None:
        """Complete the entire Run All workflow."""
        self.end_time = datetime.now()
        self.overall_status = "completed"
        self.overall_progress = 100.0
        
        total_duration = (self.end_time - self.start_time).total_seconds() if self.start_time else 0
        
        # Log completion summary
        success_count = len(self.completed_analyzers)
        failure_count = len(self.failed_analyzers)
        
        logger.info(f"✅ Run All workflow complete! {success_count} successful, {failure_count} failed, "
                   f"total duration: {total_duration:.1f}s")
        
        # Send final completion update
        self._send_run_all_progress_update(
            f"✅ Run All Complete! {success_count}/{self.total_analyzers} analyzers successful",
            100.0,
            None,
            "run_all_complete",
            {
                "total_duration": total_duration,
                "success_count": success_count,
                "failure_count": failure_count,
                "summary": self.get_run_all_summary()
            }
        )
    
    def _estimate_total_duration(self) -> float:
        """Estimate total duration for all analyzers in minutes."""
        # Rough estimates based on typical analyzer execution times
        analyzer_time_estimates = {
            "dataset_columns_analysis": 3.0,  # 3 minutes
            "evaluate_fact_dimension_analysis": 2.5,  # 2.5 minutes
            "evaluate_metadata_analysis": 1.5,  # 1.5 minutes
            "evaluate_table_linking": 2.0,  # 2 minutes
            "evaluate_table_hierarchies": 1.5,  # 1.5 minutes
            "evaluate_measures_analysis": 2.0,  # 2 minutes
            "evaluate_security_roles": 1.0   # 1 minute
        }
        
        total_estimate = 0.0
        for info in self.analyzer_sessions.values():
            estimated_time = analyzer_time_estimates.get(info.analyzer_function, 2.0)
            total_estimate += estimated_time
        
        return total_estimate
    
    def _summarize_results(self, results: Any) -> Dict[str, Any]:
        """Create a summary of analyzer results."""
        if not results:
            return {"status": "no_results"}
        
        if isinstance(results, dict):
            return {
                "status": "success",
                "type": "dict",
                "keys": list(results.keys())[:10],  # First 10 keys
                "total_keys": len(results) if hasattr(results, '__len__') else 1
            }
        elif isinstance(results, list):
            return {
                "status": "success", 
                "type": "list",
                "length": len(results),
                "sample_item": str(results[0])[:100] if results else None
            }
        else:
            return {
                "status": "success",
                "type": type(results).__name__,
                "preview": str(results)[:100]
            }
    
    def _send_run_all_progress_update(self, message: str, progress: float, 
                                    analyzer_info: Optional[AnalyzerSessionInfo],
                                    update_type: str, extra_data: Dict[str, Any] = None) -> None:
        """
        Send Run All progress update.
        
        This method would typically integrate with the UI progress tracking system.
        For now, it logs the updates. In the full implementation, this would send
        updates to the streaming system.
        """
        update_data = {
            "type": "run_all_progress",
            "message": message,
            "overall_progress": progress,
            "update_type": update_type,
            "main_session_id": self.main_session_id,
            "current_analyzer_index": self.current_analyzer_index,
            "total_analyzers": self.total_analyzers,
            "timestamp": datetime.now().isoformat()
        }
        
        if analyzer_info:
            update_data["analyzer_info"] = {
                "session_id": analyzer_info.session_id,
                "name": analyzer_info.analyzer_name,
                "status": analyzer_info.status,
                "progress": analyzer_info.progress,
                "batch_info": analyzer_info.batch_info
            }
        
        if extra_data:
            update_data["extra_data"] = extra_data
        
        # Log the update (in full implementation, this would go to streaming system)
        logger.info(f"📊 RUN ALL UPDATE: {message} ({progress:.1f}%)")
        
        # TODO: Integrate with streaming system
        # self.streaming_manager.send_run_all_update(update_data)


# Factory function for easy integration
def create_run_all_session_manager(main_session_id: str, progress_callback: Optional[Callable] = None) -> RunAllSessionManager:
    """
    Factory function to create a new Run All Session Manager.
    
    Args:
        main_session_id: Main session identifier for Run All workflow
        progress_callback: Optional callback for progress updates
    
    Returns:
        Configured RunAllSessionManager instance
    """
    return RunAllSessionManager(main_session_id, progress_callback)