import re
from typing import Any, Dict, List, Union

class SecretMasker:
    """Mask sensitive data in logs and error messages."""
    
    # Patterns for common secrets in strings
    # Captures key followed by potential separator and value
    PATTERNS = [
        (r"(api[_-]?key['\"]?\s*[:=]\s*['\"]?)([^'\"\s,]+)", r"\1***MASKED***"),
        (r"(token['\"]?\s*[:=]\s*['\"]?)([^'\"\s,]+)", r"\1***MASKED***"),
        (r"(password['\"]?\s*[:=]\s*['\"]?)([^'\"\s,]+)", r"\1***MASKED***"),
        (r"(secret['\"]?\s*[:=]\s*['\"]?)([^'\"\s,]+)", r"\1***MASKED***"),
        (r"(bearer\s+)([a-zA-Z0-9_-]+)", r"\1***MASKED***"),
    ]
    
    # Keys to mask in dictionaries (lowercase)
    SENSITIVE_KEYS = {
        'api_key', 'apikey', 'api-key',
        'token', 'access_token', 'refresh_token',
        'password', 'passwd', 'pwd',
        'secret', 'client_secret',
        'hf_token', 'openai_api_key', 'gemini_api_key',
        't212_api_key', 'alpaca_api_key', 'alpaca_secret_key',
        'news_api_key', 'tavily_api_key', 'auth_token',
        'authorization', 'cookie'
    }
    
    @classmethod
    def mask_string(cls, text: str) -> str:
        """Mask secrets in a string using regex."""
        if not text:
            return text
        
        result = text
        for pattern, replacement in cls.PATTERNS:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
        return result
    
    @classmethod
    def mask_value(cls, value: Any) -> Any:
        """Mask a single value based on its type."""
        if isinstance(value, str):
            # If it looks like a long token (JWT, etc), mask all but last 4
            if len(value) > 20: 
                return '***' + value[-4:]
            return '***MASKED***'
        return '***MASKED***'

    @classmethod
    def mask_structure(cls, data: Any) -> Any:
        """Recursively mask secrets in a dictionary or list."""
        if isinstance(data, dict):
            masked = {}
            for key, value in data.items():
                if key.lower() in cls.SENSITIVE_KEYS:
                    masked[key] = cls.mask_value(value)
                else:
                    masked[key] = cls.mask_structure(value)
            return masked
        elif isinstance(data, list):
            return [cls.mask_structure(item) for item in data]
        elif isinstance(data, str):
            # Try to determine if the string itself contains secrets (e.g. JSON string)
            # This is expensive, so we only apply basic regex masking on raw strings
            return cls.mask_string(data)
        else:
            return data

    @classmethod
    def mask_args(cls, *args, **kwargs):
        """Mask secrets in function arguments."""
        masked_args = tuple(cls.mask_structure(arg) for arg in args)
        masked_kwargs = cls.mask_structure(kwargs)
        return masked_args, masked_kwargs