import re

with open("Growin/ThemeComponents.swift", "r") as f:
    code = f.read()

print("Checking old content...")
# Ah, I replaced it all without git diffing earlier... Wait! Is that because I already modified it?
