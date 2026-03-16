import os

replacements = {
    "from backend.agents": "from agents",
    "from backend.utils": "from utils",
    "from backend.models": "from models",
    "from backend.routes": "from routes",
    "from backend.quant_engine": "from quant_engine",
    "from backend.data_models": "from data_models",
    "from backend.market_context": "from market_context",
    "from backend.forecast_bridge": "from forecast_bridge",
    "from backend.app_context": "from app_context"
}

for root, dirs, files in os.walk("tests/backend"):
    for file in files:
        if file.endswith(".py"):
            path = os.path.join(root, file)
            with open(path, "r") as f:
                content = f.read()
            changed = False
            for k, v in replacements.items():
                if k in content:
                    content = content.replace(k, v)
                    changed = True
            if changed:
                with open(path, "w") as f:
                    f.write(content)
