with open("Growin/ThemeComponents.swift", "r") as f:
    code = f.read()

print("Initial:")
print(code.find(".foregroundStyle(.white)"))
print(code.find("Color.white.opacity(0.2)"))

import re

old_glass_gradient = r'''LinearGradient\(
\s*colors: \[
\s*\.white\.opacity\(0\.15\),
\s*\.white\.opacity\(0\.02\),
\s*\.white\.opacity\(0\.15\)
\s*\],
\s*startPoint: \.topLeading,
\s*endPoint: \.bottomTrailing
\s*\)'''
print("Regex find:", re.search(old_glass_gradient, code))

# Apply transformations
code = code.replace(".foregroundStyle(.white)", ".foregroundStyle(Color.textPrimary)")
code = code.replace(".foregroundStyle(Color.white)", ".foregroundStyle(Color.textPrimary)")
code = code.replace(".foregroundStyle(.white.opacity(0.5))", ".foregroundStyle(Color.textSecondary)")
code = code.replace(".foregroundStyle(.white.opacity(0.3))", ".foregroundStyle(Color.textTertiary)")
code = code.replace(".foregroundStyle(.white.opacity(0.1))", ".foregroundStyle(Color.glassBorder)")
code = code.replace(".foregroundStyle(.white.opacity(0.8))", ".foregroundStyle(Color.textPrimary.opacity(0.8))")
code = code.replace(".fill(.white)", ".fill(Color.textPrimary)")

code = code.replace("Color.black", "Color.growinDarkBg")
code = code.replace(".shadow(color: .black", ".shadow(color: Color.growinDarkBg")

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

code = code.replace("Color.white.opacity(0.2)", "Color.glassBorder")
code = code.replace("Color.white.opacity(0.05)", "Color.glassShine")
code = code.replace("Color.white.opacity(0.02)", "Color.glassShine")

code = code.replace("Color.orange", "Color.growinOrange")
code = code.replace("Color.gray", "Color.textTertiary")

with open("Growin/ThemeComponents.swift", "w") as f:
    f.write(code)

print("Final:")
print(code.find(".foregroundStyle(.white)"))
print(code.find("Color.white.opacity(0.2)"))
