"""
Safe Python Executor - Sandboxed Python execution for Coordinator Agent

Provides a restricted environment for the Coordinator model to execute
small Python scripts for data cleaning, ticker normalization, and
simple calculations without access to sensitive system operations.

Security Features:
- Whitelisted imports only
- Restricted builtins (no file I/O, network, subprocess)
- Execution timeout (5 seconds)
- Memory limited output
"""

import ast
import sys
import logging
from typing import Dict, Any, Optional, Tuple
from io import StringIO
import traceback

logger = logging.getLogger(__name__)

# Whitelisted modules the agent can import
ALLOWED_IMPORTS = {
    "re",
    "json",
    "math",
    "datetime",
    "time",
    "decimal",
    "statistics",
    "collections",
}

# Safe builtins - no file/network/system access
SAFE_BUILTINS = {
    # Types
    "True": True,
    "False": False,
    "None": None,
    # Type constructors
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "frozenset": frozenset,
    # Safe operations
    "len": len,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "sorted": sorted,
    "reversed": reversed,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "pow": pow,
    "divmod": divmod,
    # String operations
    "ord": ord,
    "chr": chr,
    "repr": repr,
    # Type checking
    "isinstance": isinstance,
    "issubclass": issubclass,
    "type": type,
    # "hasattr" and "getattr" removed for security (prevents sandbox escapes)
    # Iteration
    "iter": iter,
    "next": next,
    "all": all,
    "any": any,
    # Print (captured to stdout)
    "print": print,
}

# Dangerous patterns to block
BLOCKED_PATTERNS = [
    "import os",
    "import sys",
    "import subprocess",
    "import shutil",
    "import socket",
    "import requests",
    "import urllib",
    "import http",
    "__import__",
    "exec(",
    "eval(",
    "compile(",
    "open(",
    "input(",
    "globals(",
    "locals(",
    "vars(",
    "dir(",
    "delattr",
    "setattr",
    "__builtins__",
    "__class__",
    "__bases__",
    "__subclasses__",
    "__mro__",
    "__code__",
    "__globals__",
]


class SafePythonExecutor:
    """
    Executes Python code in a restricted sandbox.
    
    Designed for the Coordinator Agent to perform:
    - Ticker symbol normalization (AAPL -> AAPL, lloy -> LLOY.L)
    - Data format conversions (GBX to GBP, date parsing)
    - Simple calculations and string manipulations
    """
    
    def __init__(self, timeout_seconds: float = 5.0, max_output_chars: int = 4096):
        self.timeout = timeout_seconds
        self.max_output = max_output_chars
        self._prepare_safe_modules()
    
    def _prepare_safe_modules(self):
        """Pre-import allowed modules for the sandbox."""
        self.safe_modules = {}
        for module_name in ALLOWED_IMPORTS:
            try:
                self.safe_modules[module_name] = __import__(module_name)
            except ImportError:
                logger.warning(f"Could not import {module_name} for sandbox")
    
    def _validate_code(self, code: str) -> Tuple[bool, Optional[str]]:
        """
        Static analysis to block dangerous patterns before execution.
        
        Returns:
            (is_safe, error_message)
        """
        # Check for blocked patterns
        code_lower = code.lower()
        for pattern in BLOCKED_PATTERNS:
            if pattern.lower() in code_lower:
                return False, f"Blocked pattern detected: {pattern}"
        
        # Parse AST to check for dangerous constructs
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                # Block attribute access to dangerous dunder methods
                if isinstance(node, ast.Attribute):
                    if node.attr.startswith("__") and node.attr.endswith("__"):
                        if node.attr not in ("__init__", "__str__", "__repr__", "__len__"):
                            return False, f"Blocked dunder access: {node.attr}"
                
                # Check imports are in whitelist
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                            return False, f"Import not allowed: {alias.name}"
                
                if isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[0] not in ALLOWED_IMPORTS:
                        return False, f"Import not allowed: {node.module}"
                        
        except SyntaxError as e:
            return False, f"Syntax error: {e}"
        
        return True, None
    
    def execute(self, code: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Execute Python code in the sandbox.
        
        Args:
            code: Python code to execute
            context: Optional dict of variables to inject (e.g., {"ticker": "AAPL"})
            
        Returns:
            {
                "success": bool,
                "result": Any,  # Last expression value or None
                "output": str,  # Captured stdout
                "error": str | None
            }
        """
        # Validate code first
        is_safe, error = self._validate_code(code)
        if not is_safe:
            logger.warning(f"SafePython blocked unsafe code: {error}")
            return {
                "success": False,
                "result": None,
                "output": "",
                "error": f"Security violation: {error}"
            }
        
        # Create a restricted __import__ that only allows whitelisted modules
        def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
            base_module = name.split(".")[0]
            if base_module not in ALLOWED_IMPORTS:
                raise ImportError(f"Import not allowed: {name}")
            return __builtins__["__import__"](name, globals, locals, fromlist, level)
        
        # Prepare execution environment
        safe_globals = {
            "__builtins__": {**SAFE_BUILTINS, "__import__": restricted_import},
            **self.safe_modules
        }
        
        # Inject context variables
        safe_locals = {}
        if context:
            for key, value in context.items():
                # Only allow primitive types and lists/dicts
                if isinstance(value, (str, int, float, bool, list, dict, type(None))):
                    safe_locals[key] = value
        
        # Capture stdout
        old_stdout = sys.stdout
        sys.stdout = captured_output = StringIO()
        
        result = None
        error = None
        
        try:
            # Use threading timeout for safety
            import signal
            
            def timeout_handler(signum, frame):
                raise TimeoutError("Execution timed out")
            
            # Set timeout (Unix only, graceful fallback on Windows)
            try:
                old_handler = signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(int(self.timeout))
            except (AttributeError, ValueError):
                pass  # Windows or signal not available
            
            try:
                # Execute the code
                exec(compile(code, "<sandbox>", "exec"), safe_globals, safe_locals)
                
                # Try to get a 'result' variable if defined
                if "result" in safe_locals:
                    result = safe_locals["result"]
                    
            finally:
                try:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)
                except (AttributeError, ValueError):
                    pass
                    
        except TimeoutError:
            error = f"Execution timed out after {self.timeout}s"
            logger.warning(f"SafePython timeout: {code[:100]}...")
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)}"
            logger.warning(f"SafePython execution error: {error}")
        finally:
            sys.stdout = old_stdout
        
        # Get captured output
        output = captured_output.getvalue()
        if len(output) > self.max_output:
            output = output[:self.max_output] + "\n... (truncated)"
        
        return {
            "success": error is None,
            "result": result,
            "output": output,
            "error": error
        }


# Singleton instance
_executor: Optional[SafePythonExecutor] = None


def get_safe_executor() -> SafePythonExecutor:
    """Get or create the global SafePythonExecutor instance."""
    global _executor
    if _executor is None:
        _executor = SafePythonExecutor()
    return _executor


# Convenience function for Coordinator
def run_safe_python(code: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Execute Python code safely in the sandbox.
    
    Example:
        result = run_safe_python(
            '''
            import re
            ticker = ticker.upper().strip()
            if not ticker.endswith('.L') and ticker in UK_TICKERS:
                result = ticker + '.L'
            else:
                result = ticker
            ''',
            context={"ticker": "lloy", "UK_TICKERS": ["LLOY", "HSBA", "BP"]}
        )
        print(result["result"])  # "LLOY.L"
    """
    return get_safe_executor().execute(code, context)
