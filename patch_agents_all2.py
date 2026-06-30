import os
import glob
import re

def process_file(filepath):
    if "coordinator_agent.py.orig" in filepath: return
    with open(filepath, "r") as f:
        content = f.read()

    modified = False

    # Regex to match generic exception handlers followed by a logger error/warning call
    # except Exception as var:
    #     logger.error/warning(f"...: {var}")
    # Replace with: handle_error(var, "...", logger, raise_error=False)

    pattern = r'(\s*)except Exception as ([a-zA-Z0-9_]+):\s+((?:self\.)?)logger\.(error|warning)\(f?["\'](.*?)["\'](?:,\s*exc_info=True)?\)'

    def replacer(match):
        indent = match.group(1)
        var = match.group(2)
        self_prefix = match.group(3)
        msg = match.group(5)

        # clean msg
        clean_msg = re.sub(r'[:\-\s]*\{' + var + r'\}', '', msg)
        clean_msg = re.sub(r'[:\-\s]*%s', '', clean_msg)

        logger_name = f"{self_prefix}logger"

        return f'{indent}except Exception as {var}:\n{indent}    handle_error({var}, "{clean_msg}", {logger_name}, raise_error=False)'

    new_content, count = re.subn(pattern, replacer, content)

    if count > 0:
        modified = True

        # Add import if missing
        if "from utils.error_handler import handle_error" not in new_content:
            if "from typing import" in new_content:
                new_content = new_content.replace("from typing import", "from utils.error_handler import handle_error\nfrom typing import", 1)
            else:
                new_content = "from utils.error_handler import handle_error\n" + new_content

        with open(filepath, "w") as f:
            f.write(new_content)

agent_files = glob.glob("backend/agents/*.py") + glob.glob("backend/agents/social_swarm/*.py")
agent_files.append("backend/data_engine.py")
agent_files.append("backend/lm_studio_client.py")

for f in agent_files:
    process_file(f)
