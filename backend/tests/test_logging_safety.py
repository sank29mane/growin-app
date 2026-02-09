import unittest
import logging
from app_logging import setup_logging, get_recent_logs, log_buffer

class TestLoggingSafety(unittest.TestCase):
    def setUp(self):
        # Clear buffer
        log_buffer.clear()
        # Setup logger
        self.logger = setup_logging("test_safety_logger", level=logging.INFO)

    def test_masking_api_key_in_message(self):
        """Test that API keys in the message string are masked."""
        secret_key = "sk-1234567890abcdef"
        self.logger.info(f"Connecting with api_key='{secret_key}'")
        
        logs = get_recent_logs()
        last_log = logs[-1]
        
        self.assertIn("***MASKED***", last_log)
        self.assertNotIn(secret_key, last_log)

    def test_masking_in_args_dict(self):
        """Test that sensitive keys in argument dictionaries are masked."""
        user_data = {"username": "user1", "password": "supersecretpassword123"}
        self.logger.info("User login attempt: %s", user_data)
        
        logs = get_recent_logs()
        last_log = logs[-1]
        
        self.assertIn("username", last_log)
        self.assertIn("user1", last_log)
        self.assertIn("***MASKED***", last_log)
        self.assertNotIn("supersecretpassword123", last_log)

    def test_masking_bearer_token(self):
        """Test that Bearer tokens are masked."""
        token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        self.logger.info(f"Authorization header: {token}")
        
        logs = get_recent_logs()
        last_log = logs[-1]
        
        self.assertIn("Authorization header: Bearer ***MASKED***", last_log)
        self.assertNotIn(token.replace("Bearer ", ""), last_log)

if __name__ == '__main__':
    unittest.main()
