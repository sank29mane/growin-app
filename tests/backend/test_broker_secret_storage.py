import json

from chat_manager import ChatManager


def test_chat_database_scrubs_legacy_trading212_secrets(tmp_path):
    db_path = tmp_path / "growin.db"
    manager = ChatManager(str(db_path))
    manager.conn.execute(
        "UPDATE mcp_servers SET env = ? WHERE name = 'Trading 212'",
        (
            json.dumps(
                {
                    "TRADING212_API_KEY": "key-sentinel",
                    "TRADING212_API_SECRET": "secret-sentinel",
                    "TRADING212_API_KEY_ISA": "isa-key-sentinel",
                    "TRADING212_API_SECRET_ISA": "isa-secret-sentinel",
                    "TRADING212_USE_DEMO": "true",
                }
            ),
        ),
    )
    manager.conn.commit()
    manager.conn.close()

    reopened = ChatManager(str(db_path))
    row = reopened.conn.execute(
        "SELECT env FROM mcp_servers WHERE name = 'Trading 212'"
    ).fetchone()
    reopened.conn.close()

    assert json.loads(row[0]) == {"TRADING212_USE_DEMO": "true"}
    raw_database = db_path.read_bytes()
    assert b"key-sentinel" not in raw_database
    assert b"secret-sentinel" not in raw_database


def test_database_schema_does_not_add_secret_columns(tmp_path):
    db_path = tmp_path / "growin.db"
    manager = ChatManager(str(db_path))
    columns = {
        row[1]
        for row in manager.conn.execute("PRAGMA table_info(mcp_servers)").fetchall()
    }
    manager.conn.close()

    assert not any("secret" in column.lower() or "api_key" in column.lower() for column in columns)
