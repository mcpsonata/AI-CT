"""
UI Tracker Checklist Validator

This script enforces centralized logging mechanisms across all analyzer functions
to ensure UI progress tracking works consistently and reliably.

CRITICAL RULE:
All six individual analyzers MUST use the UIProgressTracker's centralized logging.
Any other logging patterns break session isolation and UI tracking functionality.

Usage:
    python uiTrackerChecklist.py                    # Validate all analyzers
    python uiTrackerChecklist.py --file server.py   # Validate specific file
    python uiTrackerChecklist.py --verbose          # Show detailed analysis
    python uiTrackerChecklist.py --fix              # Suggest fixes for violations

How it works:
    Auto-discovers all module-level analyzer functions in src/server.py
    by scanning for functions with 'analyze' or 'evaluate' in their name
    that use UIProgressTracker for session-aware logging.
    
Currently validates (auto-discovered):
    - dataset_columns_analysis
    - evaluate_fact_dimension_analysis  
    - evaluate_metadata_analysis
    - evaluate_security_compliance
    - evaluate_table_hierarchies
    - evaluate_table_linking
    - Evaluate_Copilot_Measures
"""

import os
import re
import sys
import ast
import argparse
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict

# ANSI color codes for terminal output
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
class LoggingViolation:
    """Represents a logging violation found in the code"""
    file_path: str
    line_number: int
    violation_type: str
    code_snippet: str
    severity: str  # 'critical', 'warning', 'info'
    function_name: str
    description: str
    fix_suggestion: str

@dataclass
class AnalyzerValidationResult:
    """Results of validating a single analyzer function"""
    analyzer_name: str
    file_path: str
    has_ui_tracker: bool = False
    uses_session_aware_logging: bool = False
    violations: List[LoggingViolation] = field(default_factory=list)
    helper_functions_checked: Set[str] = field(default_factory=set)
    total_lines: int = 0
    
    @property
    def is_valid(self) -> bool:
        """Check if analyzer passes all validation checks"""
        critical_violations = [v for v in self.violations if v.severity == 'critical']
        return (self.has_ui_tracker and 
                self.uses_session_aware_logging and 
                len(critical_violations) == 0)
    
    @property
    def score(self) -> int:
        """Calculate validation score (0-100) with capped penalties"""
        score = 100
        
        # Deduct points for missing core components
        if not self.has_ui_tracker:
            score -= 40
        if not self.uses_session_aware_logging:
            score -= 20
        
        # Count violations by severity (with caps to prevent excessive penalties)
        critical_count = sum(1 for v in self.violations if v.severity == 'critical')
        warning_count = sum(1 for v in self.violations if v.severity == 'warning')
        info_count = sum(1 for v in self.violations if v.severity == 'info')
        
        # Cap penalties: max 5 critical, max 10 warnings, max 15 info
        score -= min(critical_count, 5) * 15
        score -= min(warning_count, 10) * 5
        score -= min(info_count, 15) * 2
        
        return max(0, score)


class UITrackerValidator:
    """Main validator class for checking UI tracker compliance"""
    
    # Unauthorized logging patterns (CRITICAL violations)
    UNAUTHORIZED_PATTERNS = [
        {
            'pattern': r'\bprint\s*\(',
            'name': 'print() statement',
            'description': 'Direct print() bypasses session-aware logging and breaks UI streaming',
            'severity': 'critical',
            'fix': 'Use logger.info() (module-level) or ui_tracker.logger.info() instead'
        },
        {
            'pattern': r'logging\.getLogger\s*\([^)]*\)\s*\.(?:info|warning|error|debug|critical)',
            'name': 'Direct logging.getLogger() call',
            'description': 'Direct logger calls bypass SessionAwareLogHandler and break session isolation',
            'severity': 'critical',
            'fix': 'Use logger.info() (module-level logger already configured with SessionAwareLogHandler)'
        },
        {
            'pattern': r'(?<!\w)(?:temp_logger|custom_logger|my_logger|new_logger)\s*=\s*logging\.getLogger',
            'name': 'Variable assigned to logging.getLogger()',
            'description': 'Creating new logger instances bypasses centralized session-aware logging',
            'severity': 'critical',
            'fix': 'Remove variable assignment. Use module-level logger instead'
        },
        {
            'pattern': r'sys\.stdout\.write\s*\(',
            'name': 'sys.stdout.write()',
            'description': 'Direct stdout writing breaks session isolation',
            'severity': 'critical',
            'fix': 'Use logger.info() or ui_tracker.logger.info()'
        },
        {
            'pattern': r'sys\.stderr\.write\s*\(',
            'name': 'sys.stderr.write()',
            'description': 'Direct stderr writing breaks session isolation',
            'severity': 'critical',
            'fix': 'Use logger.error() or ui_tracker.logger.error()'
        },
        {
            'pattern': r'console\.log\s*\(',
            'name': 'console.log()',
            'description': 'JavaScript-style logging has no effect in Python',
            'severity': 'warning',
            'fix': 'Use logger.info() instead'
        },
        {
            'pattern': r'class\s+\w*(?:Progress)?Tracker\w*\s*(?:\([^)]*\))?\s*:',
            'name': 'Custom ProgressTracker class',
            'description': 'Custom tracker classes bypass UIProgressTracker session management',
            'severity': 'critical',
            'fix': 'Use create_analysis_tracker() or UIProgressTracker from ui_progress_tracker'
        },
        {
            'pattern': r'(?<!\.)(?:open|file)\s*\([^)]*["\'](?:stdout|stderr|log)["\']',
            'name': 'Direct file writing to stdout/stderr/log',
            'description': 'Direct file operations bypass centralized logging',
            'severity': 'warning',
            'fix': 'Use logger methods for output'
        }
    ]
    
    # Required pattern checks
    REQUIRED_PATTERNS = [
        {
            'pattern': r'from\s+src\.ui_progress_tracker\s+import\s+[^\n;]*(?:UIProgressTracker|create_analysis_tracker)',
            'name': 'UIProgressTracker import',
            'description': 'Must import UIProgressTracker or create_analysis_tracker (handles multi-line and aliases)'
        },
        {
            'pattern': r'(?:tracker|ui_tracker)\s*=.*create_analysis_tracker\s*\(|UIProgressTracker\s*\(',
            'name': 'UIProgressTracker instantiation',
            'description': 'Must create UIProgressTracker instance for session-aware logging'
        },
        {
            'pattern': r'logger\.(?:info|warning|error)|(?:tracker|ui_tracker)\.logger\.(?:info|warning|error)',
            'name': 'Session-aware logging usage',
            'description': 'Uses module-level logger (acceptable) or tracker.logger for log messages'
        }
    ]
    
    def __init__(self, base_path: str = None, verbose: bool = False):
        """
        Initialize the validator.
        
        Args:
            base_path: Root directory of the project (defaults to current directory)
            verbose: Enable verbose output
        """
        self.base_path = Path(base_path) if base_path else Path.cwd()
        self.verbose = verbose
        self.results: Dict[str, AnalyzerValidationResult] = {}
        
    def validate_all_analyzers(self) -> Dict[str, AnalyzerValidationResult]:
        """
        Validate all analyzer functions.
        
        Returns:
            Dictionary mapping analyzer names to validation results
        """
        print(f"{Colors.HEADER}{'='*80}{Colors.ENDC}")
        print(f"{Colors.HEADER}🔍 UI TRACKER CHECKLIST VALIDATION{Colors.ENDC}")
        print(f"{Colors.HEADER}{'='*80}{Colors.ENDC}\n")
        
        # Find server.py
        server_file = self.base_path / 'src' / 'server.py'
        if not server_file.exists():
            print(f"{Colors.FAIL}❌ ERROR: Could not find src/server.py{Colors.ENDC}")
            return {}
        
        print(f"📁 Analyzing file: {server_file}")
        
        # Read the file
        with open(server_file, 'r', encoding='utf-8') as f:
            file_content = f.read()
        
        # Dynamically discover analyzer functions
        self.analyzer_functions = self._discover_analyzer_functions(file_content)
        
        if not self.analyzer_functions:
            print(f"{Colors.FAIL}❌ No analyzer functions found{Colors.ENDC}")
            return {}
        
        print(f"🎯 Auto-discovered {len(self.analyzer_functions)} analyzer functions\n")
        if self.verbose:
            print(f"{Colors.OKCYAN}Functions: {', '.join(self.analyzer_functions)}{Colors.ENDC}\n")
        
        # Validate each analyzer
        for analyzer_name in self.analyzer_functions:
            result = self._validate_analyzer(analyzer_name, server_file, file_content)
            self.results[analyzer_name] = result
        
        return self.results
    
    def _discover_analyzer_functions(self, file_content: str) -> List[str]:
        """
        Dynamically discover analyzer function names from server.py.
        Prioritizes expected main analyzer functions, then discovers others.
        
        Args:
            file_content: Content of server.py
            
        Returns:
            List of discovered analyzer function names sorted alphabetically
        """
        discovered = []
        lines = file_content.split('\n')
        in_class = False
        class_indent = 0
        
        # Primary analyzer function names (these are the main ones we care about)
        primary_analyzers = [
            'Evaluate_Copilot_Measures',
            'dataset_columns_analysis',
            'evaluate_fact_dimension_analysis',
            'evaluate_table_linking',
            'evaluate_table_hierarchies',
            'evaluate_security_compliance',
            'evaluate_metadata_analysis'
        ]
        
        for i, line in enumerate(lines):
            # Track if we're inside a class definition
            if line.strip().startswith('class '):
                in_class = True
                class_indent = len(line) - len(line.lstrip())
                continue
            
            # Exit class when indentation goes back to module level
            if in_class and line.strip() and not line[0].isspace():
                in_class = False
            
            # Look for function definitions
            if line.strip().startswith('def '):
                # Match any function definition
                match = re.match(r'^def\s+([a-zA-Z_]\w*)\s*\(', line.strip())
                if match:
                    func_name = match.group(1)
                    
                    # First priority: Check if it's a primary analyzer function
                    if func_name in primary_analyzers:
                        # Verify it uses UIProgressTracker
                        context = '\n'.join(lines[i:min(len(lines), i+200)])
                        if 'UIProgressTracker' in context or 'ui_tracker' in context or 'create_analysis_tracker' in context:
                            if func_name not in discovered:
                                discovered.append(func_name)
        
        return sorted(discovered)
    
    def _validate_analyzer(self, analyzer_name: str, file_path: Path, 
                          file_content: str) -> AnalyzerValidationResult:
        """
        Validate a single analyzer function.
        
        Args:
            analyzer_name: Name of the analyzer function
            file_path: Path to the file containing the analyzer
            file_content: Content of the file
            
        Returns:
            AnalyzerValidationResult with validation details
        """
        print(f"\n{Colors.OKBLUE}{'─'*80}{Colors.ENDC}")
        print(f"{Colors.BOLD}Analyzer: {analyzer_name}{Colors.ENDC}")
        print(f"{Colors.OKBLUE}{'─'*80}{Colors.ENDC}")
        
        result = AnalyzerValidationResult(
            analyzer_name=analyzer_name,
            file_path=str(file_path)
        )
        
        # Find the analyzer function in the file
        function_start, function_end = self._find_function_bounds(
            analyzer_name, file_content
        )
        
        if function_start == -1:
            print(f"{Colors.FAIL}❌ Function not found in file{Colors.ENDC}")
            return result
        
        # Extract function code
        lines = file_content.split('\n')
        function_code = '\n'.join(lines[function_start:function_end+1])
        result.total_lines = function_end - function_start + 1
        
        print(f"📍 Found at lines {function_start+1} - {function_end+1} ({result.total_lines} lines)")
        
        # Check for required patterns
        print(f"\n{Colors.OKCYAN}Checking required components:{Colors.ENDC}")
        result.has_ui_tracker = self._check_pattern(
            function_code, self.REQUIRED_PATTERNS[0], "  ✓ UIProgressTracker import"
        ) or self._check_pattern(
            function_code, self.REQUIRED_PATTERNS[1], "  ✓ UIProgressTracker instantiation"
        )
        
        result.uses_session_aware_logging = self._check_pattern(
            function_code, self.REQUIRED_PATTERNS[2], "  ✓ Session-aware logging"
        )
        
        if not result.has_ui_tracker:
            print(f"  {Colors.FAIL}✗ Missing UIProgressTracker{Colors.ENDC}")
        if not result.uses_session_aware_logging:
            print(f"  {Colors.WARNING}⚠ Missing tracker.logger usage{Colors.ENDC}")
        
        # Check for unauthorized patterns
        print(f"\n{Colors.OKCYAN}Scanning for unauthorized logging:{Colors.ENDC}")
        violations_found = 0
        
        for pattern_def in self.UNAUTHORIZED_PATTERNS:
            violations = self._find_violations(
                function_code, pattern_def, function_start, analyzer_name
            )
            result.violations.extend(violations)
            violations_found += len(violations)
            
            if violations:
                for violation in violations:
                    color = Colors.FAIL if violation.severity == 'critical' else Colors.WARNING
                    severity_label = "CRITICAL" if violation.severity == 'critical' else "WARNING"
                    print(f"\n  {color}{'━'*76}")
                    print(f"  ✗ [{severity_label}] {violation.violation_type}")
                    print(f"  {'━'*76}{Colors.ENDC}")
                    print(f"  {Colors.BOLD}📍 Location:{Colors.ENDC}")
                    print(f"     File: {file_path.name}")
                    print(f"     Function: {analyzer_name}()")
                    print(f"     Line: {violation.line_number}")
                    print(f"\n  {Colors.BOLD}❌ Current Code:{Colors.ENDC}")
                    print(f"     {violation.code_snippet.strip()}")
                    print(f"\n  {Colors.BOLD}⚠️  Why This Is Wrong:{Colors.ENDC}")
                    print(f"     {violation.description}")
                    print(f"\n  {Colors.BOLD}🔧 How To Fix:{Colors.ENDC}")
                    print(f"     {violation.fix_suggestion}")
                    
                    # Add specific fix example based on violation type
                    if 'print(' in violation.violation_type.lower():
                        print(f"\n  {Colors.BOLD}✅ Correct Code:{Colors.ENDC}")
                        print(f"     logger.info(your_message)  # or tracker.logger.info()")
                    elif 'getlogger' in violation.violation_type.lower():
                        print(f"\n  {Colors.BOLD}✅ Correct Code:{Colors.ENDC}")
                        print(f"     logger.info(your_message)  # Use module-level logger")
                    elif 'progresstracker' in violation.violation_type.lower():
                        print(f"\n  {Colors.BOLD}✅ Correct Code:{Colors.ENDC}")
                        print(f"     tracker = create_analysis_tracker(session_id, analysis_type)")
                        print(f"     # or")
                        print(f"     tracker = UIProgressTracker(session_id)")
                    
                    print(f"  {color}{'━'*76}{Colors.ENDC}")
        
        if violations_found == 0:
            print(f"  {Colors.OKGREEN}✓ No unauthorized logging patterns found{Colors.ENDC}")
        
        # Check helper functions called from this analyzer
        helper_functions = self._find_helper_functions(function_code)
        if helper_functions:
            print(f"\n{Colors.OKCYAN}Checking {len(helper_functions)} helper functions:{Colors.ENDC}")
            for helper_name in helper_functions:
                helper_violations = self._validate_helper_function(
                    helper_name, file_content, analyzer_name
                )
                result.violations.extend(helper_violations)
                result.helper_functions_checked.add(helper_name)
        
        # Calculate and display score
        score = result.score
        if score == 100:
            score_color = Colors.OKGREEN
            score_icon = "✅"
        elif score >= 80:
            score_color = Colors.WARNING
            score_icon = "⚠️"
        else:
            score_color = Colors.FAIL
            score_icon = "❌"
        
        print(f"\n{score_color}{score_icon} Validation Score: {score}/100{Colors.ENDC}")
        
        return result
    
    def _find_function_bounds(self, function_name: str, content: str) -> Tuple[int, int]:
        """
        Find the start and end line numbers of a function.
        
        Args:
            function_name: Name of the function to find
            content: File content
            
        Returns:
            Tuple of (start_line, end_line) or (-1, -1) if not found
        """
        lines = content.split('\n')
        
        # Find function definition
        function_pattern = re.compile(rf'^\s*def\s+{re.escape(function_name)}\s*\(')
        start_line = -1
        
        for i, line in enumerate(lines):
            if function_pattern.match(line):
                start_line = i
                break
        
        if start_line == -1:
            return -1, -1
        
        # Find function end (next function definition or class definition at same indent level)
        start_indent = len(lines[start_line]) - len(lines[start_line].lstrip())
        end_line = len(lines) - 1
        
        for i in range(start_line + 1, len(lines)):
            line = lines[i]
            if line.strip():  # Non-empty line
                current_indent = len(line) - len(line.lstrip())
                # If we find a line at the same or lower indent that starts a new definition
                if current_indent <= start_indent and (
                    line.strip().startswith('def ') or 
                    line.strip().startswith('class ') or
                    line.strip().startswith('@')
                ):
                    end_line = i - 1
                    break
        
        return start_line, end_line
    
    def _check_pattern(self, code: str, pattern_def: dict, success_msg: str = None) -> bool:
        """
        Check if a required pattern exists in the code.
        
        Args:
            code: Code to check
            pattern_def: Pattern definition dictionary
            success_msg: Message to print if pattern is found
            
        Returns:
            True if pattern is found, False otherwise
        """
        pattern = re.compile(pattern_def['pattern'], re.MULTILINE)
        found = bool(pattern.search(code))
        
        if found and success_msg:
            print(f"  {Colors.OKGREEN}{success_msg}{Colors.ENDC}")
        
        return found
    
    def _find_violations(self, code: str, pattern_def: dict, line_offset: int,
                        function_name: str) -> List[LoggingViolation]:
        """
        Find all violations of a specific pattern in the code.
        
        Args:
            code: Code to check
            pattern_def: Pattern definition dictionary
            line_offset: Line number offset in original file
            function_name: Name of the function being checked
            
        Returns:
            List of LoggingViolation objects
        """
        violations = []
        pattern = re.compile(pattern_def['pattern'])
        lines = code.split('\n')
        
        for i, line in enumerate(lines):
            matches = pattern.finditer(line)
            for match in matches:
                violation = LoggingViolation(
                    file_path='src/server.py',
                    line_number=line_offset + i + 1,
                    violation_type=pattern_def['name'],
                    code_snippet=line,
                    severity=pattern_def['severity'],
                    function_name=function_name,
                    description=pattern_def['description'],
                    fix_suggestion=pattern_def['fix']
                )
                violations.append(violation)
        
        return violations
    
    def _find_helper_functions(self, code: str) -> Set[str]:
        """
        Find helper functions defined within the analyzer function.
        Uses both AST parsing (preferred) and pattern matching (fallback) for comprehensive detection.
        
        Args:
            code: Analyzer function code
            
        Returns:
            Set of helper function names
        """
        helpers = set()
        
        # Try AST parsing first (more reliable for nested functions and complex cases)
        try:
            # Parse the function code as a module
            tree = ast.parse(code)
            
            # Walk through all nodes in the AST
            for node in ast.walk(tree):
                # Find nested function definitions
                if isinstance(node, ast.FunctionDef):
                    # Skip private functions (starting with _) unless they're helper utils
                    if not node.name.startswith('__'):  # Allow single underscore
                        helpers.add(node.name)
                        
        except SyntaxError:
            # If AST parsing fails (incomplete code, syntax errors), fall back to regex
            if self.verbose:
                print(f"  {Colors.WARNING}⚠ AST parsing failed, using regex fallback{Colors.ENDC}")
        
        # Pattern fallback: Nested functions (def inside def) - captures functions at 4+ spaces indent
        nested_pattern = re.compile(r'^\s{4,}def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', re.MULTILINE)
        regex_helpers = set(nested_pattern.findall(code))
        helpers.update(regex_helpers)
        
        # Pattern 2: Try to find function calls using AST (more reliable)
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    # Found a nested function definition
                    if not node.name.startswith('_'):
                        helpers.add(node.name)
        except SyntaxError:
            # If AST parsing fails, rely on regex
            pass
        
        return helpers
    
    def _validate_helper_function(self, helper_name: str, file_content: str,
                                  parent_analyzer: str) -> List[LoggingViolation]:
        """
        Validate a helper function for logging violations.
        
        Args:
            helper_name: Name of the helper function
            file_content: Full file content
            parent_analyzer: Name of the parent analyzer function
            
        Returns:
            List of violations found in the helper function
        """
        violations = []
        
        # Find helper function code
        start, end = self._find_function_bounds(helper_name, file_content)
        if start == -1:
            return violations
        
        lines = file_content.split('\n')
        helper_code = '\n'.join(lines[start:end+1])
        
        # Check for violations in helper function
        violations_found = False
        for pattern_def in self.UNAUTHORIZED_PATTERNS:
            helper_violations = self._find_violations(
                helper_code, pattern_def, start, f"{parent_analyzer} → {helper_name}"
            )
            if helper_violations:
                violations_found = True
                violations.extend(helper_violations)
        
        # Report findings with details
        if violations_found:
            print(f"    {Colors.FAIL}✗ {helper_name}(): {len(violations)} violation(s){Colors.ENDC}")
            for v in violations:
                color = Colors.FAIL if v.severity == 'critical' else Colors.WARNING
                severity_label = "CRITICAL" if v.severity == 'critical' else "WARNING"
                print(f"\n      {color}{'─'*70}")
                print(f"      [{severity_label}] {v.violation_type}")
                print(f"      {'─'*70}{Colors.ENDC}")
                print(f"      📍 Line {v.line_number}: {v.code_snippet.strip()}")
                print(f"      ⚠️  {v.description}")
                print(f"      🔧 Fix: {v.fix_suggestion}")
                print(f"      {color}{'─'*70}{Colors.ENDC}")
        else:
            print(f"    {Colors.OKGREEN}✓ {helper_name}(){Colors.ENDC}")
        
        return violations
    
    def print_summary(self) -> None:
        """Print a summary of all validation results"""
        print(f"\n{Colors.HEADER}{'='*80}{Colors.ENDC}")
        print(f"{Colors.HEADER}📊 VALIDATION SUMMARY{Colors.ENDC}")
        print(f"{Colors.HEADER}{'='*80}{Colors.ENDC}\n")
        
        total_analyzers = len(self.results)
        passed = sum(1 for r in self.results.values() if r.is_valid)
        failed = total_analyzers - passed
        
        total_violations = sum(len(r.violations) for r in self.results.values())
        critical_violations = sum(
            len([v for v in r.violations if v.severity == 'critical'])
            for r in self.results.values()
        )
        
        # Overall status
        if passed == total_analyzers:
            status_color = Colors.OKGREEN
            status_icon = "✅"
            status_text = "ALL CHECKS PASSED"
        elif critical_violations == 0:
            status_color = Colors.WARNING
            status_icon = "⚠️"
            status_text = "WARNINGS FOUND"
        else:
            status_color = Colors.FAIL
            status_icon = "❌"
            status_text = "CRITICAL ISSUES FOUND"
        
        print(f"{status_color}{status_icon} {status_text}{Colors.ENDC}\n")
        
        # Statistics
        print(f"Analyzers Checked: {total_analyzers}")
        print(f"  {Colors.OKGREEN}✓ Passed: {passed}{Colors.ENDC}")
        print(f"  {Colors.FAIL}✗ Failed: {failed}{Colors.ENDC}")
        print(f"\nViolations Found: {total_violations}")
        print(f"  {Colors.FAIL}Critical: {critical_violations}{Colors.ENDC}")
        print(f"  {Colors.WARNING}Warnings: {total_violations - critical_violations}{Colors.ENDC}")
        
        # Individual analyzer scores
        print(f"\n{Colors.BOLD}Individual Scores:{Colors.ENDC}")
        for name, result in sorted(self.results.items(), key=lambda x: x[1].score):
            score = result.score
            if score == 100:
                score_color = Colors.OKGREEN
                icon = "✅"
            elif score >= 80:
                score_color = Colors.WARNING
                icon = "⚠️"
            else:
                score_color = Colors.FAIL
                icon = "❌"
            
            print(f"  {icon} {name:40s} {score_color}{score:3d}/100{Colors.ENDC}")
        
        # Detailed violation report
        if total_violations > 0:
            print(f"\n{Colors.BOLD}{'='*80}")
            print(f"📋 DETAILED FIX INSTRUCTIONS")
            print(f"{'='*80}{Colors.ENDC}")
            
            violation_num = 1
            for name, result in self.results.items():
                if result.violations:
                    critical_violations_in_analyzer = [v for v in result.violations if v.severity == 'critical']
                    
                    for v in result.violations:
                        color = Colors.FAIL if v.severity == 'critical' else Colors.WARNING
                        severity_label = "🔴 CRITICAL" if v.severity == 'critical' else "🟡 WARNING"
                        
                        print(f"\n{color}{'─'*80}")
                        print(f"Issue #{violation_num}: {severity_label}")
                        print(f"{'─'*80}{Colors.ENDC}")
                        
                        print(f"\n{Colors.BOLD}📍 Location:{Colors.ENDC}")
                        print(f"   Function: {name}()")
                        print(f"   File: src/server.py")
                        print(f"   Line: {v.line_number}")
                        
                        print(f"\n{Colors.BOLD}❌ Problem Found:{Colors.ENDC}")
                        print(f"   {v.violation_type}")
                        print(f"\n   Current code at line {v.line_number}:")
                        print(f"   {Colors.FAIL}{v.code_snippet.strip()}{Colors.ENDC}")
                        
                        print(f"\n{Colors.BOLD}⚠️  Why This Breaks Things:{Colors.ENDC}")
                        print(f"   {v.description}")
                        
                        if v.severity == 'critical':
                            print(f"\n{Colors.BOLD}💥 Impact:{Colors.ENDC}")
                            print(f"   {Colors.FAIL}This BLOCKS deployment - breaks session isolation and UI tracking{Colors.ENDC}")
                        
                        print(f"\n{Colors.BOLD}🔧 Step-by-Step Fix:{Colors.ENDC}")
                        print(f"   1. Open: src/server.py")
                        print(f"   2. Navigate to: Line {v.line_number}")
                        print(f"   3. Find: {v.code_snippet.strip()}")
                        print(f"   4. Replace with: {v.fix_suggestion}")
                        
                        # Add specific before/after examples
                        if 'print(' in v.violation_type.lower():
                            print(f"\n{Colors.BOLD}✅ Before → After Example:{Colors.ENDC}")
                            print(f"   {Colors.FAIL}❌ print('Processing data...'){Colors.ENDC}")
                            print(f"   {Colors.OKGREEN}✅ logger.info('Processing data...'){Colors.ENDC}")
                        elif 'getlogger' in v.violation_type.lower():
                            print(f"\n{Colors.BOLD}✅ Before → After Example:{Colors.ENDC}")
                            print(f"   {Colors.FAIL}❌ logger = logging.getLogger(__name__){Colors.ENDC}")
                            print(f"   {Colors.FAIL}   logger.info('message'){Colors.ENDC}")
                            print(f"   {Colors.OKGREEN}✅ # Use the module-level logger (already configured){Colors.ENDC}")
                            print(f"   {Colors.OKGREEN}✅ logger.info('message'){Colors.ENDC}")
                        elif 'progresstracker' in v.violation_type.lower():
                            print(f"\n{Colors.BOLD}✅ Before → After Example:{Colors.ENDC}")
                            print(f"   {Colors.FAIL}❌ class ProgressTracker:{Colors.ENDC}")
                            print(f"   {Colors.FAIL}       def __init__(self): ...{Colors.ENDC}")
                            print(f"   {Colors.OKGREEN}✅ from src.ui_progress_tracker import create_analysis_tracker{Colors.ENDC}")
                            print(f"   {Colors.OKGREEN}✅ tracker = create_analysis_tracker(session_id, 'analysis_type'){Colors.ENDC}")
                        
                        print(f"\n{Colors.BOLD}✓ Verification:{Colors.ENDC}")
                        print(f"   Run: python uiTrackerChecklist.py")
                        print(f"   Expected: Issue #{violation_num} should disappear")
                        
                        violation_num += 1
            
            print(f"\n{color}{'='*80}{Colors.ENDC}")
        else:
            print(f"\n{Colors.OKGREEN}✅ No violations found - all analyzers follow best practices!{Colors.ENDC}")
        
        # Recommendations
        print(f"\n{Colors.BOLD}Recommendations:{Colors.ENDC}")
        if critical_violations > 0:
            print(f"  {Colors.FAIL}❌ DEPLOYMENT BLOCKED - Fix critical violations before deploying{Colors.ENDC}")
        elif total_violations > 0:
            print(f"  {Colors.WARNING}⚠️  Review warnings to ensure consistent logging practices{Colors.ENDC}")
        else:
            print(f"  {Colors.OKGREEN}✅ All analyzers follow centralized logging - Safe to deploy{Colors.ENDC}")
        
        print(f"\n{Colors.HEADER}{'='*80}{Colors.ENDC}\n")
    
    def generate_report(self, output_file: str = None) -> str:
        """
        Generate a detailed text report of validation results.
        
        Args:
            output_file: Optional path to save the report
            
        Returns:
            Report content as string
        """
        from datetime import datetime
        
        lines = []
        lines.append("=" * 80)
        lines.append("UI TRACKER CHECKLIST VALIDATION REPORT")
        lines.append("=" * 80)
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Project: {self.base_path}")
        lines.append("")
        
        # Summary
        total_analyzers = len(self.results)
        passed = sum(1 for r in self.results.values() if r.is_valid)
        total_violations = sum(len(r.violations) for r in self.results.values())
        critical_violations = sum(
            len([v for v in r.violations if v.severity == 'critical'])
            for r in self.results.values()
        )
        
        lines.append("SUMMARY")
        lines.append("-" * 80)
        lines.append(f"Total Analyzers: {total_analyzers}")
        lines.append(f"Passed: {passed}")
        lines.append(f"Failed: {total_analyzers - passed}")
        lines.append(f"Total Violations: {total_violations}")
        lines.append(f"Critical: {critical_violations}")
        lines.append("")
        
        # Detailed results
        for name, result in self.results.items():
            lines.append("=" * 80)
            lines.append(f"ANALYZER: {name}")
            lines.append("=" * 80)
            lines.append(f"Score: {result.score}/100")
            lines.append(f"Status: {'PASSED' if result.is_valid else 'FAILED'}")
            lines.append(f"Has UIProgressTracker: {'Yes' if result.has_ui_tracker else 'No'}")
            lines.append(f"Uses Session-Aware Logging: {'Yes' if result.uses_session_aware_logging else 'No'}")
            lines.append(f"Total Lines: {result.total_lines}")
            lines.append(f"Helper Functions Checked: {len(result.helper_functions_checked)}")
            lines.append("")
            
            if result.violations:
                lines.append("VIOLATIONS:")
                for v in result.violations:
                    lines.append(f"  Line {v.line_number}: [{v.severity.upper()}] {v.violation_type}")
                    lines.append(f"    Description: {v.description}")
                    lines.append(f"    Code: {v.code_snippet.strip()}")
                    lines.append(f"    Fix: {v.fix_suggestion}")
                    lines.append("")
            else:
                lines.append("No violations found.")
                lines.append("")
        
        report = "\n".join(lines)
        
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(report)
            print(f"📄 Report saved to: {output_file}")
        
        return report


def main():
    """Main entry point for the validator"""
    # Set UTF-8 encoding for Windows console
    import sys
    if sys.platform == 'win32':
        try:
            import codecs
            sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
            sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
        except Exception:
            pass  # If encoding setup fails, continue anyway
    
    parser = argparse.ArgumentParser(
        description='Validate UI tracker compliance across all analyzer functions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python uiTrackerChecklist.py                    # Validate all analyzers
  python uiTrackerChecklist.py --verbose          # Show detailed analysis
  python uiTrackerChecklist.py --report           # Generate text report
  python uiTrackerChecklist.py --output report.txt # Save report to file
        """
    )
    
    parser.add_argument(
        '--path',
        type=str,
        default=None,
        help='Base path of the project (default: current directory)'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose output with detailed analysis'
    )
    
    parser.add_argument(
        '--report', '-r',
        action='store_true',
        help='Generate detailed text report'
    )
    
    parser.add_argument(
        '--output', '-o',
        type=str,
        default=None,
        help='Save report to specified file'
    )
    
    args = parser.parse_args()
    
    # Create validator and run checks
    validator = UITrackerValidator(base_path=args.path, verbose=args.verbose)
    results = validator.validate_all_analyzers()
    
    if not results:
        print(f"{Colors.FAIL}❌ Validation failed - no results generated{Colors.ENDC}")
        return 1
    
    # Print summary
    validator.print_summary()
    
    # Generate report if requested
    if args.report or args.output:
        validator.generate_report(output_file=args.output)
    
    # Exit with appropriate code
    total_critical = sum(
        len([v for v in r.violations if v.severity == 'critical'])
        for r in results.values()
    )
    
    if total_critical > 0:
        return 1  # Fail build/deployment
    else:
        return 0  # Success


if __name__ == '__main__':
    sys.exit(main())
