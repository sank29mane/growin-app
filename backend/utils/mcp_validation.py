import os
from typing import List, Optional

def validate_mcp_config(command: str, args: Optional[List[str]] = None) -> None:
    """
    Validates MCP server configuration to prevent command injection and usage of dangerous tools.

    Args:
        command: The executable command to run
        args: List of arguments (optional)

    Raises:
        ValueError: If the command is blocked or invalid
    """
    if not command:
        raise ValueError("Command cannot be empty")

    # Normalized command for checking
    cmd_lower = command.lower()
    # Handle both separators manually to be safe against cross-platform attacks
    # (os.path.basename only handles native separator)
    base_cmd = cmd_lower.replace('\\', '/').split('/')[-1]

    # Blocklist of dangerous shell interpreters and tools
    BLOCKED_COMMANDS = {
        # Shells
        'bash', 'sh', 'zsh', 'csh', 'tcsh', 'ksh', 'dash', 'ash', 'rbash',
        'cmd', 'cmd.exe', 'powershell', 'pwsh', 'pwsh.exe',

        # Network & Transfer tools
        'curl', 'wget', 'nc', 'netcat', 'ncat', 'socat',
        'ssh', 'scp', 'sftp', 'telnet', 'ftp',

        # System modification tools
        'rm', 'mv', 'cp', 'chmod', 'chown', 'sudo', 'su',
        'dd', 'mkfs',

        # Script execution engines (often used for bypass)
        'php', 'perl', 'ruby', 'lua',
        'jjs', 'nashorn'
    }

    # Python and Node.js are explicitly allowed as they are standard runtimes for MCP servers.
    # While they can be used maliciously, blocking them would break core MCP functionality.

    if base_cmd in BLOCKED_COMMANDS:
        raise ValueError(f"Command '{base_cmd}' is not allowed for security reasons.")

    # Sentinel: Interpreter Argument Validation
    # Prevent code execution flags that bypass script file requirement
    INTERPRETER_FLAGS = {
        "python": {"-c"},
        "python3": {"-c"},
        "node": {"-e", "--eval"},
        "nodejs": {"-e", "--eval"},
        "php": {"-r"},
        "perl": {"-e"},
        "ruby": {"-e"},
    }

    if args and base_cmd in INTERPRETER_FLAGS:
        blocked_flags = INTERPRETER_FLAGS[base_cmd]
        for arg in args:
            if arg in blocked_flags:
                raise ValueError(f"Interpreter flag '{arg}' is not allowed for security reasons.")
