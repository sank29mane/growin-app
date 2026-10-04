import re
from typing import Any, Dict, List, Union


def _normalize(key: Any) -> str:
    """Lower-case a key and drop every non-alphanumeric character."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


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
        # --- Breeze shapes (Phase 61). Each keeps the key or header name. ---
        # Query parameters: ?apisession=CODE&x=1
        (r"([?&](?:api_?session|session_?token|api_?key|app_?key|secret_?key|api_?secret|checksum|token)=)([^&\s#'\"]+)",
         r"\1***MASKED***"),
        # Cookie header: the value runs to the end of the line.
        (r"(\bcookie\s*:\s*)([^\r\n]+)", r"\1***MASKED***"),
        # Other headers: X-Checksum: token <hex>, X-AppKey: K, apikey: K, Authorization: ...
        (r"(\b(?:x-checksum|x-appkey|x-sessiontoken|apikey|authorization)\s*:\s*)(?:(?:token|bearer|basic)\s+)?([^\s,'\"]+)",
         r"\1***MASKED***"),
        # JSON or Python-literal pairs: "AppKey": "K", 'secret_key': 'S', SessionToken=T
        (r"""((?:app_?key|secret_?key|api_?secret|session_?token|api_?session|checksum)['"]?\s*[:=]\s*)"[^"]*\"""",
         r'\1"***MASKED***"'),
        (r"""((?:app_?key|secret_?key|api_?secret|session_?token|api_?session|checksum)['"]?\s*[:=]\s*)'[^']*'""",
         r"\1'***MASKED***'"),
        (r"""((?:app_?key|secret_?key|api_?secret|session_?token|api_?session|checksum)['"]?\s*[:=]\s*)(?!["'])[^\s,&}\]]+""",
         r"\1***MASKED***"),
        # A bare checksum value: token <64 hex>
        (r"(\btoken\s+)[0-9a-f]{64}\b", r"\1***MASKED***"),
    ]

    # Keys to mask in dictionaries. Matching is done on the normalized key
    # (lower-case, non-alphanumerics removed), so "session_token",
    # "SessionToken" and "X-SessionToken" are all caught by one entry.
    SENSITIVE_KEYS = {
        'api_key', 'apikey', 'api-key',
        'token', 'access_token', 'refresh_token',
        'password', 'passwd', 'pwd',
        'secret', 'client_secret',
        'hf_token', 'openai_api_key', 'gemini_api_key',
        't212_api_key', 'alpaca_api_key', 'alpaca_secret_key',
        'news_api_key', 'tavily_api_key', 'auth_token',
        'authorization', 'cookie',
        # Breeze shapes (Phase 61)
        'session_token', 'x_session_token', 'api_session', 'api_secret',
        'secret_key', 'app_key', 'x_app_key', 'x_checksum', 'checksum',
    }

    _NORMALIZED_KEYS = frozenset(_normalize(k) for k in SENSITIVE_KEYS)

    # A string value is masked when the normalized key merely contains one of
    # these. Numbers, booleans and None under such a key are left alone so
    # usage counters (max_tokens, total_tokens) stay readable.
    SENSITIVE_KEY_PARTS = (
        'token', 'secret', 'password', 'passwd', 'apikey', 'appkey',
        'apisession', 'sessionkey', 'checksum', 'authorization', 'cookie',
        'credential',
    )

    @classmethod
    def _normalize_key(cls, key: Any) -> str:
        return _normalize(key)

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
        """Mask a single value. Never reveals any character of it."""
        return '***MASKED***'

    @classmethod
    def mask_structure(cls, data: Any) -> Any:
        """Recursively mask secrets in dicts, lists, tuples and sets."""
        if isinstance(data, dict):
            masked = {}
            for key, value in data.items():
                norm = cls._normalize_key(key)
                if norm in cls._NORMALIZED_KEYS:
                    masked[key] = cls.mask_value(value)
                elif (
                    isinstance(value, (str, bytes))
                    and any(part in norm for part in cls.SENSITIVE_KEY_PARTS)
                ):
                    masked[key] = cls.mask_value(value)
                else:
                    masked[key] = cls.mask_structure(value)
            return masked
        elif isinstance(data, list):
            return [cls.mask_structure(item) for item in data]
        elif isinstance(data, tuple):
            return tuple(cls.mask_structure(item) for item in data)
        elif isinstance(data, (set, frozenset)):
            return type(data)(cls.mask_structure(item) for item in data)
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
