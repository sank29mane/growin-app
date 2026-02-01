"""
Skill Loader - Injects specialist skills into LLM contexts
"""
import os
import glob
import logging
from typing import Dict, List, Optional
import re

logger = logging.getLogger(__name__)

class SkillLoader:
    def __init__(self, skills_dir: str = ".agents/skills", global_skills_dir: str = "~/.agents/skills"):
        # Resolve paths
        self.project_skills_path = os.path.abspath(skills_dir)
        self.global_skills_path = os.path.expanduser(global_skills_dir)
        self.skills_cache: Dict[str, str] = {}
        
        # Load skills on init
        self.refresh_skills()
        
    def refresh_skills(self):
        """Load all SKILL.md files from both global and project paths"""
        self.skills_cache = {}
        
        # 1. Load Global Skills first
        self._load_from_path(self.global_skills_path)
            
        # 2. Load Project Skills (overwrite global if same name)
        self._load_from_path(self.project_skills_path)
        
        logger.info(f"SkillLoader: Loaded {len(self.skills_cache)} skills: {list(self.skills_cache.keys())}")

    def _load_from_path(self, path: str):
        if not os.path.exists(path):
            return
            
        # Find all SKILL.md files
        skill_files = glob.glob(os.path.join(path, "**", "SKILL.md"), recursive=True)
        
        for file_path in skill_files:
            try:
                folder_name = os.path.basename(os.path.dirname(file_path))
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    
                # Store full content
                self.skills_cache[folder_name] = content
            except Exception as e:
                logger.warning(f"Failed to load skill {file_path}: {e}")

    def get_relevant_skills(self, query: str) -> str:
        """
        Return content of skills relevant to the query.
        Simple keyword matching for high speed.
        """
        query_lower = query.lower()
        relevant_content = []
        
        # Keyword mapping (expand this as needed)
        triggers = {
            "financial-operations-expert": ["profit", "loss", "tax", "accounting", "margin", "p&l", "expense", "revenue", "bookkeeping"],
            "sql-optimization-patterns": ["sql", "database", "query", "slow", "performance", "optimize db", "postgres", "sqlite"],
            "python-performance-optimization": ["python", "slow", "async", "performance", "optimize code", "loop", "threading"],
            "stripe-best-practices": ["stripe", "payment", "subscription", "webhook", "checkout"],
            "docker-expert": ["docker", "container", "compose", "deploy"],
            "frontend-design": ["ui", "design", "css", "component", "look", "style", "theme", "color", "layout"],
            "cto-advisor": ["architecture", "scaling", "tech debt", "team", "cto", "strategy"],
            "expert-architect": ["positioning", "brand", "story", "marketing"],
            "trading-plan-generator": ["trading plan", "strategy", "risk management", "swing trade", "day trade", "position size", "stop loss", "portfolio plan", "goal plan"]
        }
        
        for skill_name, content in self.skills_cache.items():
            # Check keywords
            keywords = triggers.get(skill_name, [])
            if any(k in query_lower for k in keywords):
                # Extract the "Instructions" part (usually after frontmatter)
                # Removing YAML frontmatter for cleaner context
                clean_content = re.sub(r'^---.*?---', '', content, flags=re.DOTALL).strip()
                relevant_content.append(f"--- SKILL: {skill_name} ---\n{clean_content}\n")
                
        return "\n".join(relevant_content)

# Global singleton
_loader = None
def get_skill_loader():
    global _loader
    if not _loader:
        _loader = SkillLoader()
    return _loader
