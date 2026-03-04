import re

with open("Growin/ThemeComponents.swift", "r") as f:
    content = f.read()

# Make all instances of ".white" opacity use textSecondary or equivalent
content = content.replace(".foregroundStyle(.white.opacity(0.5))", ".foregroundStyle(Color.textSecondary.opacity(0.8))")
content = content.replace(".foregroundStyle(.white.opacity(0.3))", ".foregroundStyle(Color.textTertiary)")
content = content.replace("Color.white.opacity(0.02)", "Color.glassShine")
content = content.replace(".foregroundStyle(.white.opacity(0.1))", ".foregroundStyle(Color.glassBorder)")
content = content.replace(".foregroundStyle(.white.opacity(0.8))", ".foregroundStyle(Color.textPrimary.opacity(0.8))")

# Re-check for any explicit hardcoded colors not using constants
content = content.replace("Color.white", "Color.textPrimary")
content = content.replace("Color.black", "Color.growinDarkBg")
content = content.replace("Color.gray", "Color.textTertiary")
content = content.replace("Color.orange", "Color.growinOrange")

# Standardize cornerRadiuses
content = content.replace("cornerRadius: 14", "cornerRadius: 16") # premium button usually 16 or 20
content = content.replace("cornerRadius: 12", "cornerRadius: 16")

with open("Growin/ThemeComponents.swift", "w") as f:
    f.write(content)
