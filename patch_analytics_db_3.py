import re

with open("backend/analytics_db.py", "r") as f:
    content = f.read()

# I will refactor _execute to accept fetch options to keep the lock held during the fetch.
new_execute = """
    def _execute(self, query: str, parameters: tuple = (), fetch: str = None):
        with self.lock:
            cursor = self.conn.execute(query, parameters)
            if fetch == "one":
                return cursor.fetchone()
            elif fetch == "all":
                return cursor.fetchall()
            return cursor
"""

content = re.sub(
    r'    def _execute\(self, query: str, parameters: tuple = \(\)\):\n        with self\.lock:\n            return self\.conn\.execute\(query, parameters\)',
    new_execute.strip('\n'),
    content
)

# Now find all usages of _execute(...).fetchone() and _execute(...).fetchall() and replace them
content = re.sub(r'self\._execute\((.*?)\)\.fetchone\(\)', r'self._execute(\1, fetch="one")', content)
content = re.sub(r'self\._execute\((.*?)\)\.fetchall\(\)', r'self._execute(\1, fetch="all")', content)

with open("backend/analytics_db.py", "w") as f:
    f.write(content)
