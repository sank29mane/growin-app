"""
Text Processing Utilities
Handles text cleaning, title generation, and sanitization.
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

def clean_llm_response(text: str) -> str:
    """
    Remove common LLM artifacts like <think> tags and meta-commentary.
    """
    if not text:
        return ""
        
    # Strip think blocks
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    
    # Strip meta-patterns
    meta_patterns = [
        r'**thinking**.*?\n',
        r'^we need to', r'^let\'s', r'^based on my instructions',
        r'^i will follow', r'^i should', r'^i must',
        r'style guidelines:', r'as requested,'
    ]
    
    for pattern in meta_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
        
    return text.strip().replace('\n{3,}', '\n\n')

def extract_title_from_text(text: str, default: str = "Financial Analysis") -> str:
    """
    Extract a clean title from raw LLM output.
    """
    # 1. Clean artifacts
    text = clean_llm_response(text)
    
    # 2. Strip quotes
    text = text.strip().strip('"').strip("'").strip('`').strip('*').strip()
    
    # 3. Get first valid line
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    
    clean_lines = []
    for l in lines:
        l_lower = l.lower()
        if any(x in l_lower for x in ["output only", "3-5 word", "title:", "user:", "assistant:"]):
            continue
        if len(l) > 1:
            clean_lines.append(l)
            
    title = clean_lines[0] if clean_lines else default
    
    # 4. Final length check
    if len(title) > 40:
        title = title[:37] + "..."
    if len(title) < 2:
        title = default
        
    return title
