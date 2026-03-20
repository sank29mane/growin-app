with open("backend/analytics_db.py", "r") as f:
    content = f.read()

if "import threading" not in content:
    content = content.replace("import asyncio", "import asyncio\nimport threading")

wrapper = """
    def _execute(self, query: str, parameters: tuple = ()):
        with self.lock:
            return self.conn.execute(query, parameters)

    def _init_schema(self):"""

if "def _execute(self" not in content:
    content = content.replace("    def _init_schema(self):", wrapper)
    content = content.replace("self.conn.execute", "self._execute")
    # Fix the wrapper's inner self.conn.execute
    content = content.replace("return self._execute(query, parameters)", "return self.conn.execute(query, parameters)")

if "self.lock = threading.Lock()" not in content:
    content = content.replace("self._init_schema()", "self.lock = threading.Lock()\n        self._init_schema()")

with open("backend/analytics_db.py", "w") as f:
    f.write(content)
