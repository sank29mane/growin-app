import re

with open("Growin/ThemeComponents.swift", "r") as f:
    content = f.read()

# Replace hardcoded white with Color.textPrimary where appropriate
content = content.replace(".foregroundStyle(.white)", ".foregroundStyle(Color.textPrimary)")
content = content.replace(".foregroundStyle(Color.white)", ".foregroundStyle(Color.textPrimary)")

# Standardize glass borders in borders/strokes
content = re.sub(r'LinearGradient\(\s*colors:\s*\[\s*\.white\.opacity\(0\.15\),\s*\.white\.opacity\(0\.02\),\s*\.white\.opacity\(0\.15\)\s*\],\s*startPoint:\s*\.topLeading,\s*endPoint:\s*\.bottomTrailing\s*\)',
                 r'''LinearGradient(
                        colors: [
                            Color.glassBorder,
                            Color.glassShine,
                            Color.glassBorder
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )''', content)

content = content.replace("Color.white.opacity(0.2)", "Color.glassBorder")
content = content.replace("Color.white.opacity(0.05)", "Color.glassShine")

with open("Growin/ThemeComponents.swift", "w") as f:
    f.write(content)
