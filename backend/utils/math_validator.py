import ast
import json
import logging
import asyncio
from typing import Dict, Any, Optional, Tuple
from backend.docker_mcp_server import DockerMCPServer

logger = logging.getLogger("math_validator")

# Whitelisted modules the math script can import in Docker
# More permissive than internal sandbox because it's in a container
ALLOWED_MATH_IMPORTS = {
    "math",
    "numpy",
    "pandas",
    "mlx",
    "mlx.core",
    "mlx.nn",
    "json",
    "datetime",
    "time",
    "statistics",
    "scipy",
}

class MathValidator:
    """
    Validation pipeline for mathematical scripts intended for NPU execution.
    
    Stages:
    1. AST Syntax & Security Validation
    2. Sandbox Execution via DockerMCPServer (NPU engine)
    3. Result Schema Validation (JSON or Numeric)
    """
    
    def __init__(self):
        self.docker_server = DockerMCPServer()

    def validate_ast(self, script_content: str) -> Tuple[bool, Optional[str]]:
        """Stage 1: AST Syntax and Security Validation"""
        try:
            tree = ast.parse(script_content)
            for node in ast.walk(tree):
                # Block dangerous dunder access
                if isinstance(node, ast.Attribute):
                    if node.attr.startswith("__") and node.attr.endswith("__"):
                        if node.attr not in ("__init__", "__str__", "__repr__", "__len__"):
                            return False, f"Blocked dunder access: {node.attr}"
                
                # Check imports
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        base_module = alias.name.split(".")[0]
                        if base_module not in ALLOWED_MATH_IMPORTS:
                            return False, f"Import not allowed: {alias.name}"
                
                if isinstance(node, ast.ImportFrom):
                    if node.module:
                        base_module = node.module.split(".")[0]
                        if base_module not in ALLOWED_MATH_IMPORTS:
                            return False, f"Import not allowed: {node.module}"

                # Block dangerous calls
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name):
                        if func.id in ("exec", "eval", "compile", "open", "input", "globals", "locals", "vars", "dir", "delattr", "setattr", "exit", "quit", "__import__"):
                            return False, f"Blocked function call: {func.id}"
                        
        except SyntaxError as e:
            return False, f"Syntax error: {e}"
        except Exception as e:
            return False, f"AST parsing error: {e}"
            
        return True, None

    def validate_result_schema(self, output: str) -> bool:
        """Stage 3: Result Schema Validation"""
        if not output or not output.strip():
            return False
            
        output = output.strip()
        
        # Try JSON
        try:
            data = json.loads(output)
            return isinstance(data, (dict, list))
        except json.JSONDecodeError:
            pass
            
        # Try Numeric
        try:
            float(output)
            return True
        except ValueError:
            pass
            
        return False

    async def execute_and_validate(self, script_content: str, engine: str = "npu", timeout: int = 30) -> Dict[str, Any]:
        """Runs the full multi-stage validation and execution pipeline"""
        
        # Stage 1: AST Security Check
        is_safe, error = self.validate_ast(script_content)
        if not is_safe:
            logger.warning(f"MathValidator blocked script: {error}")
            return {
                "status": "rejected",
                "error": f"Security validation failed: {error}",
                "stdout": "",
                "exit_code": -1
            }

        # Stage 2: Sandbox Execution
        logger.info(f"Executing math script on {engine} engine...")
        result = await asyncio.to_thread(
            self.docker_server.execute_script, 
            script_content, 
            timeout=timeout, 
            engine=engine
        )
        
        if result.get("status") != "success":
            return result

        # Stage 3: Result Validation
        stdout = result.get("stdout", "")
        if not self.validate_result_schema(stdout):
            logger.warning(f"Math script output failed schema validation: {stdout[:100]}...")
            result["schema_valid"] = False
        else:
            result["schema_valid"] = True
            
        return result
