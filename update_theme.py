import re

with open("Growin/ThemeComponents.swift", "r") as f:
    code = f.read()

# Replace hard-coded whitespaces and colors with semantic equivalents
code = code.replace(".foregroundStyle(.white)", ".foregroundStyle(Color.textPrimary)")
code = code.replace(".foregroundStyle(Color.white)", ".foregroundStyle(Color.textPrimary)")
code = code.replace(".fill(.white)", ".fill(Color.textPrimary)")

# Replace `.black` with semantic background
code = code.replace("Color.black", "Color.growinDarkBg")
code = code.replace(".shadow(color: .black", ".shadow(color: Color.growinDarkBg")

# Standardize glass stroke gradients (using standard properties)
old_glass_gradient = r'''LinearGradient\(
\s*colors: \[
\s*\.white\.opacity\(0\.15\),
\s*\.white\.opacity\(0\.02\),
\s*\.white\.opacity\(0\.15\)
\s*\],
\s*startPoint: \.topLeading,
\s*endPoint: \.bottomTrailing
\s*\)'''

new_glass_gradient = r'''LinearGradient(
                        colors: [
                            Color.glassBorder,
                            Color.glassShine,
                            Color.glassBorder
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )'''
code = re.sub(old_glass_gradient, new_glass_gradient, code)

# Fix up opacity strings
code = code.replace("Color.white.opacity(0.2)", "Color.glassBorder")
code = code.replace("Color.white.opacity(0.05)", "Color.glassShine")
code = code.replace(".white.opacity(0.1)", "Color.glassBorder")
code = code.replace(".white.opacity(0.05)", "Color.glassShine")
code = code.replace("Color.white.opacity(0.02)", "Color.glassShine")
code = code.replace(".white.opacity(0.02)", "Color.glassShine")

# Make buttons strictly conform to Liquid Glass
code = code.replace("Color.white.opacity(0.5)", "Color.textSecondary")
code = code.replace(".white.opacity(0.5)", "Color.textSecondary")
code = code.replace(".white.opacity(0.3)", "Color.textTertiary")
code = code.replace(".white.opacity(0.8)", "Color.textPrimary.opacity(0.8)")

# More semantic mappings
code = code.replace("Color.orange", "Color.growinOrange")
code = code.replace("Color.gray", "Color.textTertiary")

with open("Growin/ThemeComponents.swift", "w") as f:
    f.write(code)
