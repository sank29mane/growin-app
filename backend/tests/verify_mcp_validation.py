import sys
import os
import unittest

# Add backend to sys.path to allow imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.mcp_validation import validate_mcp_config

class TestMCPValidation(unittest.TestCase):
    def test_valid_commands(self):
        valid_commands = [
            "python", "python3", "node", "npm", "uv", "uvx",
            "/usr/bin/python", "custom_tool"
        ]
        for cmd in valid_commands:
            try:
                validate_mcp_config(cmd)
            except ValueError:
                self.fail(f"Valid command '{cmd}' raised ValueError")

    def test_blocked_commands(self):
        blocked_commands = [
            "bash", "sh", "zsh", "cmd", "cmd.exe", "powershell", "pwsh",
            "rm", "mv", "cp", "curl", "wget", "nc", "ssh"
        ]
        for cmd in blocked_commands:
            with self.assertRaises(ValueError, msg=f"Blocked command '{cmd}' did not raise ValueError"):
                validate_mcp_config(cmd)

            # Test uppercase variations
            with self.assertRaises(ValueError):
                validate_mcp_config(cmd.upper())

            # Test paths
            with self.assertRaises(ValueError):
                validate_mcp_config(f"/bin/{cmd}")

            with self.assertRaises(ValueError):
                validate_mcp_config(f"C:\\Windows\\System32\\{cmd}")

    def test_empty_command(self):
        with self.assertRaises(ValueError):
            # Type ignore because we are testing runtime behavior
            validate_mcp_config("") # type: ignore

        # Optional: check if None raises or fails type check (implementation checks 'if not command')
        try:
            validate_mcp_config(None) # type: ignore
        except ValueError:
            pass
        except Exception as e:
            self.fail(f"None raised unexpected exception: {e}")

    def test_malicious_interpreter_args(self):
        """Test blocking of dangerous flags for interpreters"""
        # Python injection
        with self.assertRaises(ValueError):
            validate_mcp_config("python", ["-c", "import os; os.system('rm -rf /')"])

        with self.assertRaises(ValueError):
            validate_mcp_config("python3", ["-c", "print('hacked')"])

        # Node injection
        with self.assertRaises(ValueError):
            validate_mcp_config("node", ["-e", "require('child_process').exec('...')"])

        with self.assertRaises(ValueError):
            validate_mcp_config("node", ["--eval", "console.log('hacked')"])

        # PHP injection
        with self.assertRaises(ValueError):
            validate_mcp_config("php", ["-r", "system('ls');"])

        # Legitimate use should pass
        try:
            validate_mcp_config("python", ["trading212_mcp_server.py"])
            validate_mcp_config("node", ["server.js"])
        except ValueError as e:
            self.fail(f"Legitimate interpreter usage blocked: {e}")

if __name__ == "__main__":
    unittest.main()
