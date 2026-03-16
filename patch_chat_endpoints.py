with open("tests/backend/test_chat_endpoints.py", "r") as f:
    content = f.read()

# See if it imports something that hangs
if "import" in content:
    print("Has imports")
