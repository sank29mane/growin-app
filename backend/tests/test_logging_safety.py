import unittest
import logging
import sys
import os

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from app_logging import SecretMaskingFormatter

class TestLoggingSafety(unittest.TestCase):
    def setUp(self):
        # Create a dedicated logger for this test to avoid interference
        self.logger = logging.getLogger("test_safety_logger_isolated")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False # Don't send to root logger
        
        # Clear existing handlers
        self.logger.handlers = []
        
        # Local buffer to capture logs
        self.log_output = []
        
        class TestHandler(logging.Handler):
            def __init__(self, output_list):
                super().__init__()
                self.output_list = output_list
            def emit(self, record):
                self.output_list.append(self.format(record))
        
        self.handler = TestHandler(self.log_output)
        self.handler.setFormatter(SecretMaskingFormatter('%(message)s'))
        self.logger.addHandler(self.handler)

    def test_masking_api_key_in_message(self):
        """Test that API keys in the message string are masked."""
        secret_key = "sk-1234567890abcdef"
        self.logger.info(f"Connecting with api_key='{secret_key}'")
        
        assert len(self.log_output) > 0
        last_log = self.log_output[-1]
        
        self.assertIn("***MASKED***", last_log)
        self.assertNotIn(secret_key, last_log)

    def test_masking_in_args_dict(self):
        """Test that sensitive keys in argument dictionaries are masked."""
        user_data = {"username": "user1", "password": "supersecretpassword123"}
        self.logger.info("User login attempt: %s", user_data)
        
        assert len(self.log_output) > 0
        last_log = self.log_output[-1]
        
        self.assertIn("username", last_log)
        self.assertIn("user1", last_log)
        # SecretMasker masks all but last 4 chars for strings > 20 chars
        # "supersecretpassword123" ends in "d123"
        self.assertTrue("***MASKED***" in last_log or "***d123" in last_log)
        self.assertNotIn("supersecretpassword123", last_log)

    def test_masking_bearer_token(self):
        """Test that Bearer tokens are masked."""
        token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        self.logger.info(f"Authorization header: {token}")
        
        assert len(self.log_output) > 0
        last_log = self.log_output[-1]
        
        self.assertIn("Authorization header: Bearer ***MASKED***", last_log)
        self.assertNotIn(token.replace("Bearer ", ""), last_log)

if __name__ == '__main__':
    unittest.main()
