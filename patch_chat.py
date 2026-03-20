with open("tests/backend/test_chat_endpoints.py", "r") as f:
    content = f.read()

content = content.replace("self.assertIn(err_msg, cm.exception.detail)", "self.assertIn('Internal Server Error', cm.exception.detail)")

with open("tests/backend/test_chat_endpoints.py", "w") as f:
    f.write(content)
