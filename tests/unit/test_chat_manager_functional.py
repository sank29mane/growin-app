import pytest
import os
from chat_manager import ChatManager

@pytest.fixture
def chat_manager():
    db_path = "test_functional.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    manager = ChatManager(db_path=db_path)
    yield manager

    manager.close()
    if os.path.exists(db_path):
        os.remove(db_path)

def test_get_mcp_servers_structure(chat_manager):
    # Add a test server
    chat_manager.add_mcp_server(
        name="Test Server",
        type="stdio",
        command="python",
        args=["test.py"],
        env={"TEST_VAR": "value"}
    )

    servers = chat_manager.get_mcp_servers()

    # Check if we got back what we put in (plus the default one)
    assert len(servers) >= 1

    test_server = next((s for s in servers if s["name"] == "Test Server"), None)
    assert test_server is not None
    assert test_server["name"] == "Test Server"
    assert test_server["type"] == "stdio"
    assert test_server["command"] == "python"
    assert test_server["args"] == ["test.py"]
    assert test_server["env"] == {"TEST_VAR": "value"}
    assert test_server["active"] is True

def test_get_mcp_servers_active_only(chat_manager):
    # The default one is active.
    # Add an inactive one.
    cursor = chat_manager.conn.cursor()
    cursor.execute(
        """
        INSERT INTO mcp_servers (name, type, command, args, env, active)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("Inactive Server", "stdio", "python", "[]", "{}", 0)
    )
    chat_manager.conn.commit()

    all_servers = chat_manager.get_mcp_servers(active_only=False)
    active_servers = chat_manager.get_mcp_servers(active_only=True)

    assert len(all_servers) > len(active_servers)

    inactive_in_list = any(s["name"] == "Inactive Server" for s in active_servers)
    assert not inactive_in_list

    inactive_in_all = any(s["name"] == "Inactive Server" for s in all_servers)
    assert inactive_in_all
