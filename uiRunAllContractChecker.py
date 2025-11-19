#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI Run All Contract Checker

Validates that Run All coordinator and session management code properly integrates 
with UIProgressTracker for real-time frontend updates during multi-analyzer workflows.

This checker focuses specifically on:
1. Run All Coordinator usage of UIProgressTracker
2. Session Manager progress coordination patterns
3. Proper callback propagation through analyzer chains
4. Overall Run All progress tracking vs individual analyzer tracking
5. Analyzer transition notifications
6. Run All lifecycle events (start, analyzer_switch, completion)

Run All Architecture:
====================
Run All workflows orchestrate multiple analyzers sequentially:
- Each analyzer gets its own session_id and UIProgressTracker
- Run All Coordinator manages overall progress across all analyzers
- Session Manager tracks which analyzer is active
- Frontend needs both: overall progress AND individual analyzer progress

Expected Pattern:
=================
✅ CORRECT Run All Implementation:

    # In RunAllCoordinator
    from src.ui_progress_tracker import UIProgressTracker
    
    # Create overall Run All tracker
    run_all_tracker = UIProgressTracker(
        analysis_name="Run All Analysis",
        total_items=len(analyzers),
        session_id=main_session_id,
        progress_callback=progress_callback
    )
    
    run_all_tracker.start()
    
    for analyzer in analyzers:
        # Each analyzer gets its own tracker
        analyzer_tracker = UIProgressTracker(
            analysis_name=analyzer.name,
            total_items=analyzer.total_items,
            session_id=analyzer.session_id,  # Sub-session
            progress_callback=progress_callback
        )
        
        analyzer_tracker.start()
        # ... run analyzer ...
        analyzer_tracker.complete(success=True)
        
        # Update overall progress
        run_all_tracker.update(analyzer_index + 1)
    
    run_all_tracker.complete(success=True)

❌ INCORRECT Anti-Patterns:

    # Don't create custom Run All progress events
    callback({"type": "run_all_progress", "current": 1, "total": 5})
    
    # Don't bypass UIProgressTracker for coordinator
    yield format_sse(json.dumps({"type": "analyzer_switch"}))
    
    # Don't use raw session manager updates without UIProgressTracker
    session_manager.update_progress(50)  # Should use tracker!

Frontend Contract:
==================
The HTML frontend expects these events from Run All workflows:
- analysis_start (event_type): Run All workflow starting
- progress_update: Overall Run All progress (X of N analyzers complete)
- phase_change: Switching to next analyzer
- analysis_start (nested): Individual analyzer starting
- progress_update (nested): Individual analyzer progress
- analysis_completion (nested): Individual analyzer complete
- analysis_completion: Overall Run All complete

Usage:
    python uiRunAllContractChecker.py                    # Check Run All files
    python uiRunAllContractChecker.py --verbose          # Show detailed analysis
    python uiRunAllContractChecker.py --fix-suggestions  # Show how to fix issues

Exit Codes:
    0 - Run All code follows UIProgressTracker patterns
    1 - Critical violations found in Run All implementation
"""

import os
import re
import sys
import ast
import argparse
from typing import List, Dict, Any, Set, Tuple, Optional
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime


# ANSI color codes
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


@dataclass
class RunAllViolation:
    """Represents a Run All contract violation."""
    file_path: str
    line_number: int
    violation_type: str
    severity: str  # 'critical', 'warning', 'info'
    message: str
    code_snippet: str
    fix_suggestion: str
    context: str = ""  # Additional context about the violation


@dataclass
class RunAllFileResult:
    """Results from validating a Run All related file."""
    file_path: str
    file_type: str  # 'coordinator', 'session_manager', 'evaluator', 'webapp'
    violations: List[RunAllViolation] = field(default_factory=list)
    has_tracker_import: bool = False
    has_tracker_usage: bool = False
    uses_progress_callback: bool = False
    line_count: int = 0
    score: float = 100.0


# Files that implement Run All functionality
RUN_ALL_FILES = {
    'coordinator': ['src/run_all_coordinator.py'],
    'session_manager': ['src/run_all_session_manager.py'],
    'evaluator': ['lib/copilot_data_evaluator.py'],  # May have execute_run_all_analysis
    'webapp': ['webapp/app.py']  # Has run_all_analyzers endpoint
}

# Required UIProgressTracker patterns for Run All
REQUIRED_PATTERNS = {
    "tracker_import": r'from src\.ui_progress_tracker import UIProgressTracker',
    "tracker_creation": r'UIProgressTracker\s*\(',
    "tracker_start": r'\.start\(\)',
    "tracker_update": r'\.update\(',
    "tracker_complete": r'\.complete\(',
    "progress_callback": r'progress_callback',
    "session_id_param": r'session_id\s*[=:]'
}

# Anti-patterns that indicate incorrect Run All implementation
RUN_ALL_ANTI_PATTERNS = {
    "manual_run_all_event": r'{\s*["\']type["\']\s*:\s*["\'](?:run_all_start|run_all_progress|run_all_complete|analyzer_switch)["\']',
    "direct_session_update": r'session_manager\.(?:update_progress|send_progress|notify)',
    "custom_overall_progress": r'{\s*["\']overall_progress["\']\s*:',
    "bypass_tracker_coordinator": r'format_sse\([^)]*(?:run_all|coordinator)',
}

# Expected coordinator lifecycle
COORDINATOR_LIFECYCLE = [
    "initialize_run_all",  # Session manager setup
    "create_run_all_stream",  # Streaming setup
    "start_next_analyzer",  # Analyzer iteration
    "complete_current_analyzer",  # Analyzer completion
    "get_run_all_summary"  # Final summary
]


class RunAllContractChecker:
    """
    Validates Run All coordinator code for proper UIProgressTracker integration.
    """
    
    def __init__(self, base_path: str = None, verbose: bool = False):
        """
        Initialize Run All Contract Checker.
        
        Args:
            base_path: Base directory path (default: current directory)
            verbose: Enable verbose output
        """
        self.base_path = Path(base_path) if base_path else Path.cwd()
        self.verbose = verbose
        self.results: Dict[str, RunAllFileResult] = {}
        
        if self.verbose:
            print(f"{Colors.OKCYAN}🔍 Run All Contract Checker initialized{Colors.ENDC}")
            print(f"{Colors.OKCYAN}📁 Base path: {self.base_path}{Colors.ENDC}")
    
    def check_all_run_all_files(self) -> Dict[str, RunAllFileResult]:
        """
        Check all Run All related files for UIProgressTracker integration.
        
        Returns:
            Dictionary mapping file paths to validation results
        """
        print(f"\n{Colors.HEADER}{'=' * 80}{Colors.ENDC}")
        print(f"{Colors.HEADER}{Colors.BOLD}🔍 RUN ALL CONTRACT VALIDATION{Colors.ENDC}")
        print(f"{Colors.HEADER}{'=' * 80}{Colors.ENDC}\n")
        
        # Check each Run All file type
        for file_type, file_list in RUN_ALL_FILES.items():
            print(f"{Colors.OKBLUE}📂 Checking {file_type} files:{Colors.ENDC}")
            
            for file_rel_path in file_list:
                file_path = self.base_path / file_rel_path
                
                if not file_path.exists():
                    if self.verbose:
                        print(f"  {Colors.WARNING}⚠️  File not found: {file_path}{Colors.ENDC}")
                    continue
                
                print(f"  {Colors.OKCYAN}📄 {file_rel_path}{Colors.ENDC}")
                result = self._validate_run_all_file(file_path, file_type)
                self.results[str(file_rel_path)] = result
                
                # Print immediate feedback
                if result.violations:
                    critical = [v for v in result.violations if v.severity == 'critical']
                    warnings = [v for v in result.violations if v.severity == 'warning']
                    
                    if critical:
                        print(f"     {Colors.FAIL}❌ {len(critical)} critical issue(s){Colors.ENDC}")
                    if warnings:
                        print(f"     {Colors.WARNING}⚠️  {len(warnings)} warning(s){Colors.ENDC}")
                else:
                    print(f"     {Colors.OKGREEN}✅ Properly uses UIProgressTracker{Colors.ENDC}")
            
            print()
        
        return self.results
    
    def _validate_run_all_file(self, file_path: Path, file_type: str) -> RunAllFileResult:
        """
        Validate a single Run All file.
        
        Args:
            file_path: Path to file
            file_type: Type of Run All file (coordinator, session_manager, etc.)
            
        Returns:
            RunAllFileResult with violations
        """
        result = RunAllFileResult(
            file_path=str(file_path),
            file_type=file_type
        )
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                lines = content.split('\n')
                result.line_count = len(lines)
            
            # Check for UIProgressTracker import
            result.has_tracker_import = bool(re.search(REQUIRED_PATTERNS['tracker_import'], content))
            result.has_tracker_usage = bool(re.search(REQUIRED_PATTERNS['tracker_creation'], content))
            result.uses_progress_callback = 'progress_callback' in content
            
            # Type-specific validations
            if file_type == 'coordinator':
                self._validate_coordinator(content, lines, result)
            elif file_type == 'session_manager':
                self._validate_session_manager(content, lines, result)
            elif file_type == 'evaluator':
                self._validate_evaluator(content, lines, result)
            elif file_type == 'webapp':
                self._validate_webapp(content, lines, result)
            
            # Common checks for all Run All files
            self._check_anti_patterns(content, lines, result)
            self._check_progress_callback_propagation(content, lines, result)
            
            # Calculate score
            result.score = self._calculate_score(result)
            
        except Exception as e:
            print(f"{Colors.FAIL}❌ Error reading file {file_path}: {str(e)}{Colors.ENDC}")
        
        return result
    
    def _validate_coordinator(self, content: str, lines: List[str], 
                            result: RunAllFileResult) -> None:
        """Validate Run All Coordinator specific patterns."""
        
        # CRITICAL: Coordinator must import UIProgressTracker
        if not result.has_tracker_import:
            result.violations.append(RunAllViolation(
                file_path=result.file_path,
                line_number=1,
                violation_type="missing_tracker_import",
                severity="critical",
                message="Run All Coordinator must import UIProgressTracker for overall progress tracking",
                code_snippet="# Missing: from src.ui_progress_tracker import UIProgressTracker",
                fix_suggestion="Add UIProgressTracker import at top of file to track overall Run All progress"
            ))
        
        # Check for execute_run_all_workflow method
        if 'execute_run_all_workflow' in content:
            # Find the method and check if it uses UIProgressTracker
            for line_num, line in enumerate(lines, start=1):
                if 'def execute_run_all_workflow' in line:
                    # Get method context (next 50 lines)
                    method_start = line_num - 1
                    method_end = min(len(lines), line_num + 50)
                    method_content = '\n'.join(lines[method_start:method_end])
                    
                    # Check if method creates UIProgressTracker for overall progress
                    if 'UIProgressTracker(' not in method_content:
                        result.violations.append(RunAllViolation(
                            file_path=result.file_path,
                            line_number=line_num,
                            violation_type="coordinator_no_tracker",
                            severity="critical",
                            message="execute_run_all_workflow should create UIProgressTracker for overall Run All progress",
                            code_snippet=line.strip(),
                            fix_suggestion="Create UIProgressTracker(analysis_name='Run All', total_items=len(analyzers), session_id=session_id, progress_callback=callback)",
                            context="Coordinator needs overall tracker to show X of N analyzers complete"
                        ))
                    
                    # Check if progress_callback is a parameter
                    if 'progress_callback' not in method_content[:200]:
                        result.violations.append(RunAllViolation(
                            file_path=result.file_path,
                            line_number=line_num,
                            violation_type="missing_progress_callback_param",
                            severity="warning",
                            message="execute_run_all_workflow should accept progress_callback parameter",
                            code_snippet=line.strip(),
                            fix_suggestion="Add progress_callback: Optional[Callable] parameter to method signature"
                        ))
                    
                    break
        
        # Check for analyzer iteration pattern
        if 'start_next_analyzer' in content:
            # Should propagate callback to each analyzer
            for line_num, line in enumerate(lines, start=1):
                if 'start_next_analyzer' in line:
                    # Get context
                    context_start = max(0, line_num - 5)
                    context_end = min(len(lines), line_num + 20)
                    context = '\n'.join(lines[context_start:context_end])
                    
                    # Check if analyzer execution passes progress_callback
                    if 'progress_callback' not in context:
                        result.violations.append(RunAllViolation(
                            file_path=result.file_path,
                            line_number=line_num,
                            violation_type="callback_not_propagated",
                            severity="warning",
                            message="Analyzer execution should propagate progress_callback to individual analyzers",
                            code_snippet=line.strip(),
                            fix_suggestion="Pass progress_callback to each analyzer so they can send updates via UIProgressTracker"
                        ))
                    break
    
    def _validate_session_manager(self, content: str, lines: List[str], 
                                 result: RunAllFileResult) -> None:
        """Validate Run All Session Manager patterns."""
        
        # Session manager coordinates but shouldn't create progress events directly
        # It should work WITH UIProgressTracker, not bypass it
        
        # Check for direct progress updates
        for line_num, line in enumerate(lines, start=1):
            # Look for methods that might be sending progress directly
            if re.search(r'def (?:update_progress|send_progress|notify_progress)', line):
                result.violations.append(RunAllViolation(
                    file_path=result.file_path,
                    line_number=line_num,
                    violation_type="session_manager_direct_progress",
                    severity="warning",
                    message="Session manager should coordinate state, not send progress events directly",
                    code_snippet=line.strip(),
                    fix_suggestion="Let UIProgressTracker send events. Session manager should track state only.",
                    context="Separation of concerns: SessionManager=state, UIProgressTracker=events"
                ))
        
        # Check for proper session_id management
        if 'initialize_run_all' in content:
            for line_num, line in enumerate(lines, start=1):
                if 'def initialize_run_all' in line:
                    # Get method context
                    method_start = line_num - 1
                    method_end = min(len(lines), line_num + 30)
                    method_content = '\n'.join(lines[method_start:method_end])
                    
                    # Should handle main_session_id
                    if 'main_session_id' not in method_content and 'session_id' not in method_content:
                        result.violations.append(RunAllViolation(
                            file_path=result.file_path,
                            line_number=line_num,
                            violation_type="missing_session_id_handling",
                            severity="warning",
                            message="initialize_run_all should manage main session_id for Run All workflow",
                            code_snippet=line.strip(),
                            fix_suggestion="Store main_session_id and generate sub-session IDs for each analyzer"
                        ))
                    break
    
    def _validate_evaluator(self, content: str, lines: List[str], 
                          result: RunAllFileResult) -> None:
        """Validate evaluator's execute_run_all_analysis method."""
        
        if 'execute_run_all_analysis' not in content:
            # This is OK - not all evaluators need Run All
            return
        
        # If it has execute_run_all_analysis, it should use RunAllCoordinator
        if 'RunAllCoordinator' not in content:
            for line_num, line in enumerate(lines, start=1):
                if 'def execute_run_all_analysis' in line:
                    result.violations.append(RunAllViolation(
                        file_path=result.file_path,
                        line_number=line_num,
                        violation_type="missing_coordinator_usage",
                        severity="critical",
                        message="execute_run_all_analysis should use RunAllCoordinator instead of custom implementation",
                        code_snippet=line.strip(),
                        fix_suggestion="Import and use RunAllCoordinator.execute_run_all_workflow() for proper tracking",
                        context="Coordinator handles all UIProgressTracker integration"
                    ))
                    break
    
    def _validate_webapp(self, content: str, lines: List[str], 
                        result: RunAllFileResult) -> None:
        """Validate webapp's run_all_analyzers endpoint."""
        
        if 'def run_all_analyzers' not in content and '@app.route' not in content:
            return
        
        # Check if endpoint calls proper coordinator/evaluator method
        for line_num, line in enumerate(lines, start=1):
            if 'def run_all_analyzers' in line or '/run-all' in line:
                # Get function context
                func_start = line_num - 1
                func_end = min(len(lines), line_num + 50)
                func_content = '\n'.join(lines[func_start:func_end])
                
                # Should call execute_run_all_analysis or coordinator
                if 'execute_run_all_analysis' not in func_content and 'execute_run_all_workflow' not in func_content:
                    result.violations.append(RunAllViolation(
                        file_path=result.file_path,
                        line_number=line_num,
                        violation_type="webapp_missing_coordinator_call",
                        severity="warning",
                        message="run_all_analyzers endpoint should call execute_run_all_analysis or coordinator",
                        code_snippet=line.strip(),
                        fix_suggestion="Call copilot_evaluator.execute_run_all_analysis() to use proper Run All architecture"
                    ))
                
                break
    
    def _check_anti_patterns(self, content: str, lines: List[str], 
                           result: RunAllFileResult) -> None:
        """Check for Run All specific anti-patterns."""
        
        for line_num, line in enumerate(lines, start=1):
            # Check for manual Run All event creation
            if re.search(RUN_ALL_ANTI_PATTERNS['manual_run_all_event'], line):
                result.violations.append(RunAllViolation(
                    file_path=result.file_path,
                    line_number=line_num,
                    violation_type="manual_run_all_event",
                    severity="critical",
                    message="Manually creating Run All events - use UIProgressTracker instead!",
                    code_snippet=line.strip(),
                    fix_suggestion="Replace manual event with UIProgressTracker.update() or UIProgressTracker.complete()"
                ))
            
            # Check for bypassing tracker with direct streaming
            if re.search(RUN_ALL_ANTI_PATTERNS['bypass_tracker_coordinator'], line):
                result.violations.append(RunAllViolation(
                    file_path=result.file_path,
                    line_number=line_num,
                    violation_type="bypass_tracker",
                    severity="critical",
                    message="Directly streaming Run All events - should use UIProgressTracker!",
                    code_snippet=line.strip(),
                    fix_suggestion="Use UIProgressTracker with progress_callback instead of format_sse()"
                ))
    
    def _check_progress_callback_propagation(self, content: str, lines: List[str], 
                                           result: RunAllFileResult) -> None:
        """Check that progress_callback is properly propagated through the call chain."""
        
        if not result.uses_progress_callback:
            # File should at least mention progress_callback if it does Run All work
            if any(keyword in content for keyword in ['run_all', 'execute_run_all', 'RunAllCoordinator']):
                result.violations.append(RunAllViolation(
                    file_path=result.file_path,
                    line_number=1,
                    violation_type="missing_progress_callback",
                    severity="warning",
                    message="Run All file should use progress_callback for UIProgressTracker integration",
                    code_snippet="# No progress_callback found in file",
                    fix_suggestion="Add progress_callback parameter and pass to UIProgressTracker"
                ))
    
    def _calculate_score(self, result: RunAllFileResult) -> float:
        """Calculate validation score (0-100)."""
        score = 100.0
        
        for violation in result.violations:
            if violation.severity == 'critical':
                score -= 25
            elif violation.severity == 'warning':
                score -= 10
            elif violation.severity == 'info':
                score -= 2
        
        return max(0.0, score)
    
    def print_summary(self) -> None:
        """Print validation summary with detailed fix instructions."""
        print(f"\n{Colors.HEADER}{'=' * 80}{Colors.ENDC}")
        print(f"{Colors.HEADER}{Colors.BOLD}📊 RUN ALL VALIDATION SUMMARY{Colors.ENDC}")
        print(f"{Colors.HEADER}{'=' * 80}{Colors.ENDC}\n")
        
        # Count totals
        total_files = len(self.results)
        files_with_violations = sum(1 for r in self.results.values() if r.violations)
        total_violations = sum(len(r.violations) for r in self.results.values())
        total_critical = sum(
            sum(1 for v in r.violations if v.severity == 'critical')
            for r in self.results.values()
        )
        
        if total_critical > 0:
            print(f"{Colors.FAIL}{Colors.BOLD}❌ CRITICAL RUN ALL CONTRACT VIOLATIONS{Colors.ENDC}\n")
        elif total_violations > 0:
            print(f"{Colors.WARNING}{Colors.BOLD}⚠️  RUN ALL WARNINGS FOUND{Colors.ENDC}\n")
        else:
            print(f"{Colors.OKGREEN}{Colors.BOLD}✅ ALL RUN ALL FILES FOLLOW CONTRACT{Colors.ENDC}\n")
        
        print(f"Files Checked: {total_files}")
        print(f"  {Colors.OKGREEN}✓ Compliant: {total_files - files_with_violations}{Colors.ENDC}")
        print(f"  {Colors.FAIL}✗ With Violations: {files_with_violations}{Colors.ENDC}\n")
        
        print(f"Total Violations: {total_violations}")
        print(f"  {Colors.FAIL}Critical: {total_critical}{Colors.ENDC}")
        print(f"  {Colors.WARNING}Warnings: {total_violations - total_critical}{Colors.ENDC}\n")
        
        # File scores
        print(f"{Colors.BOLD}File Scores:{Colors.ENDC}")
        for file_path, result in sorted(self.results.items(), key=lambda x: x[1].score):
            score = result.score
            if score == 100:
                color = Colors.OKGREEN
                icon = "✅"
            elif score >= 75:
                color = Colors.WARNING
                icon = "⚠️ "
            else:
                color = Colors.FAIL
                icon = "❌"
            
            # Tracker usage indicators
            tracker_status = ""
            if result.has_tracker_import:
                tracker_status += " [imports UIProgressTracker]"
            if result.has_tracker_usage:
                tracker_status += " [uses UIProgressTracker]"
            if result.uses_progress_callback:
                tracker_status += " [has callback]"
            
            short_path = file_path.replace('\\', '/')
            print(f"  {color}{icon} {short_path:45s} {score:6.1f}/100{tracker_status}{Colors.ENDC}")
        
        # Detailed violations
        if total_violations > 0:
            print(f"\n{Colors.BOLD}{'='*80}")
            print(f"🔧 DETAILED FIX INSTRUCTIONS")
            print(f"{'='*80}{Colors.ENDC}")
            
            for file_path, result in self.results.items():
                if not result.violations:
                    continue
                
                print(f"\n{Colors.BOLD}📁 File: {file_path}{Colors.ENDC}")
                print(f"{Colors.BOLD}   Type: {result.file_type}{Colors.ENDC}")
                print(f"{Colors.BOLD}   Score: {result.score}/100{Colors.ENDC}\n")
                
                for idx, violation in enumerate(result.violations, start=1):
                    if violation.severity == 'critical':
                        color = Colors.FAIL
                        severity_label = "🔴 CRITICAL"
                    elif violation.severity == 'warning':
                        color = Colors.WARNING
                        severity_label = "🟡 WARNING"
                    else:
                        color = Colors.OKBLUE
                        severity_label = "🔵 INFO"
                    
                    print(f"{color}{'─'*80}")
                    print(f"Issue #{idx}: {severity_label} - {violation.violation_type}")
                    print(f"{'─'*80}{Colors.ENDC}")
                    
                    print(f"\n{Colors.BOLD}📍 Location:{Colors.ENDC}")
                    print(f"   Line: {violation.line_number}")
                    print(f"   Code: {violation.code_snippet}")
                    
                    print(f"\n{Colors.BOLD}❌ Problem:{Colors.ENDC}")
                    print(f"   {violation.message}")
                    
                    if violation.context:
                        print(f"\n{Colors.BOLD}📝 Context:{Colors.ENDC}")
                        print(f"   {violation.context}")
                    
                    print(f"\n{Colors.BOLD}🔧 Fix:{Colors.ENDC}")
                    print(f"   {violation.fix_suggestion}")
                    
                    # Add specific examples based on violation type
                    if violation.violation_type == "coordinator_no_tracker":
                        print(f"\n{Colors.BOLD}✅ Example Fix:{Colors.ENDC}")
                        print(f"{Colors.OKGREEN}   run_all_tracker = UIProgressTracker({Colors.ENDC}")
                        print(f"{Colors.OKGREEN}       analysis_name='Run All Analysis',{Colors.ENDC}")
                        print(f"{Colors.OKGREEN}       total_items=len(self.analyzer_definitions),{Colors.ENDC}")
                        print(f"{Colors.OKGREEN}       session_id=session_id,{Colors.ENDC}")
                        print(f"{Colors.OKGREEN}       progress_callback=progress_callback{Colors.ENDC}")
                        print(f"{Colors.OKGREEN}   ){Colors.ENDC}")
                        print(f"{Colors.OKGREEN}   run_all_tracker.start(){Colors.ENDC}")
                    
                    elif violation.violation_type == "missing_tracker_import":
                        print(f"\n{Colors.BOLD}✅ Add to imports:{Colors.ENDC}")
                        print(f"{Colors.OKGREEN}   from src.ui_progress_tracker import UIProgressTracker{Colors.ENDC}")
                    
                    elif violation.violation_type == "callback_not_propagated":
                        print(f"\n{Colors.BOLD}✅ Example Fix:{Colors.ENDC}")
                        print(f"{Colors.OKGREEN}   analyzer_results = await self._execute_single_analyzer_async({Colors.ENDC}")
                        print(f"{Colors.OKGREEN}       analyzer_info, {Colors.ENDC}")
                        print(f"{Colors.OKGREEN}       progress_callback=progress_callback,  # Pass callback!{Colors.ENDC}")
                        print(f"{Colors.OKGREEN}       tables_to_analyze=tables_to_analyze{Colors.ENDC}")
                        print(f"{Colors.OKGREEN}   ){Colors.ENDC}")
                    
                    print()
        
        # Recommendations
        print(f"\n{Colors.BOLD}{'='*80}")
        print(f"💡 RECOMMENDATIONS")
        print(f"{'='*80}{Colors.ENDC}\n")
        
        if total_critical > 0:
            print(f"{Colors.FAIL}❌ CRITICAL ISSUES FOUND{Colors.ENDC}")
            print(f"   Run All workflows may not provide proper UI feedback")
            print(f"   Fix critical issues before deploying Run All functionality\n")
        
        print(f"{Colors.BOLD}Best Practices for Run All:{Colors.ENDC}")
        print(f"  1. {Colors.OKGREEN}✓{Colors.ENDC} Coordinator creates overall UIProgressTracker (total_items = num analyzers)")
        print(f"  2. {Colors.OKGREEN}✓{Colors.ENDC} Each analyzer gets its own UIProgressTracker (for detailed progress)")
        print(f"  3. {Colors.OKGREEN}✓{Colors.ENDC} Same progress_callback propagated to all trackers")
        print(f"  4. {Colors.OKGREEN}✓{Colors.ENDC} Session Manager tracks state, UIProgressTracker sends events")
        print(f"  5. {Colors.OKGREEN}✓{Colors.ENDC} All trackers include session_id (main or sub-session)")
        
        print(f"\n{Colors.HEADER}{'=' * 80}{Colors.ENDC}\n")


def main():
    """Main entry point."""
    if sys.platform == 'win32':
        try:
            import codecs
            sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
            sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
        except Exception:
            pass
    
    parser = argparse.ArgumentParser(
        description='Validate Run All coordinator UIProgressTracker integration',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--path', type=str, default=None,
                       help='Base path of the project')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable verbose output')
    parser.add_argument('--fix-suggestions', '-f', action='store_true',
                       help='Show detailed fix suggestions')
    
    args = parser.parse_args()
    
    # Create checker and run
    checker = RunAllContractChecker(base_path=args.path, verbose=args.verbose)
    results = checker.check_all_run_all_files()
    
    if not results:
        print(f"{Colors.FAIL}❌ No Run All files found to validate{Colors.ENDC}")
        return 1
    
    # Print summary
    checker.print_summary()
    
    # Exit code based on critical violations
    total_critical = sum(
        sum(1 for v in r.violations if v.severity == 'critical')
        for r in results.values()
    )
    
    return 1 if total_critical > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
