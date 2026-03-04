import re

with open("Growin/ThemeComponents.swift", "r") as f:
    code = f.read()

print(code.find(".foregroundStyle(.white)"))
print(code.find("LinearGradient(\n                        colors: [\n                            .white.opacity(0.15),\n                            .white.opacity(0.02),\n                            .white.opacity(0.15)\n                        ]"))
