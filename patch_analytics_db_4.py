import re

with open("backend/analytics_db.py", "r") as f:
    content = f.read()

# I also need to make sure the fetch='one' arguments are placed correctly.
# The regex replacement `self._execute(\1, fetch="one")` might result in `self._execute("query", parameters, fetch="one")`.
# Let's verify what the output of regex was.
content = content.replace('self._execute(ticker_query, params, fetch="all")', 'self._execute(ticker_query, params, fetch="all")')
content = content.replace('self._execute(specialist_query, [ticker], fetch="all")', 'self._execute(specialist_query, [ticker], fetch="all")')
content = content.replace('self._execute(specialist_query, fetch="all")', 'self._execute(specialist_query, fetch="all")')

# wait, there's another usage `self._execute("SELECT payload FROM pending_actions WHERE id = ?", (action_id,), fetch="one")`
content = re.sub(r'self\._execute\((.*?)\)\.fetchone\(\)', r'self._execute(\1, fetch="one")', content)
content = re.sub(r'self\._execute\((.*?)\)\.fetchall\(\)', r'self._execute(\1, fetch="all")', content)

with open("backend/analytics_db.py", "w") as f:
    f.write(content)
