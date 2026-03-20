with open("backend/analytics_db.py", "r") as f:
    content = f.read()

# Remove the incorrect inner with self.lock: we might have added manually with the first patch
content = content.replace("            with self.lock:\n                self._execute", "            self._execute")
content = content.replace("        with self.lock:\n            self._execute", "        self._execute")

with open("backend/analytics_db.py", "w") as f:
    f.write(content)
