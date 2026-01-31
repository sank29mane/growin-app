import json
import sqlite3
import uuid
from datetime import datetime
from typing import Dict, List, Optional


class ChatManager:
    """Manages conversation persistence and context"""

    def __init__(self, db_path: str = "growin.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        """Initialize database schema"""
        cursor = self.conn.cursor()

        # Conversations table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                title TEXT
            )
        """)

        # Messages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tool_calls TEXT,
                agent_name TEXT,
                model_name TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
        """)

        # Add model_name column if it doesn't exist (for backward compatibility)
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN model_name TEXT")
        except sqlite3.OperationalError:
            # Column already exists
            pass

        # Portfolio snapshots table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_value REAL,
                total_pnl REAL,
                cash_balance REAL,
                positions TEXT
            )
        """)

        # Index for faster message retrieval by conversation
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_timestamp
            ON messages(conversation_id, timestamp DESC)
        """)

        # Index for faster conversation listing
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_created_at
            ON conversations(created_at DESC)
        """)

        # MCP Servers table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mcp_servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL, -- 'stdio', 'sse'
                command TEXT,
                args TEXT,
                env TEXT,
                url TEXT,
                active BOOLEAN DEFAULT 1
            )
        """)

        # Insert default Trading 212 MCP if not exists
        cursor.execute("SELECT name FROM mcp_servers WHERE name = 'Trading 212'")
        if not cursor.fetchone():
            cursor.execute(
                """
                INSERT INTO mcp_servers (name, type, command, args, env)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    "Trading 212",
                    "stdio",
                    "python",
                    json.dumps(["trading212_mcp_server.py"]),
                    json.dumps({}),
                ),
            )
        else:
            # Update to fix bad data if it exists
            cursor.execute(
                """
                UPDATE mcp_servers
                SET args = ?, env = ?
                WHERE name = 'Trading 212'
            """,
                (json.dumps(["trading212_mcp_server.py"]), json.dumps({})),
            )

        self.conn.commit()

    def add_mcp_server(
        self,
        name: str,
        type: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict] = None,
        url: Optional[str] = None,
    ):
        """Add a new MCP server configuration"""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO mcp_servers (name, type, command, args, env, url)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (name, type, command, json.dumps(args), json.dumps(env), url),
        )
        self.conn.commit()

    def get_mcp_servers(self, active_only: bool = False, sanitize: bool = False) -> List[Dict]:
        """
        Get all configured MCP servers.

        Args:
            active_only: If True, return only active servers.
            sanitize: If True, mask sensitive environment variables (keys).
        """
        cursor = self.conn.cursor()
        query = "SELECT * FROM mcp_servers"
        if active_only:
            query += " WHERE active = 1"
        cursor.execute(query)

        servers = []
        for row in cursor:
            env = json.loads(row["env"]) if row["env"] else None

            # Sanitize environment variables if requested
            if sanitize and env:
                # Mask all values in env to prevent leakage
                # If value is present (non-empty), mask it. If empty, keep empty.
                sanitized_env = {}
                for k, v in env.items():
                    if v:
                        sanitized_env[k] = "********"
                    else:
                        sanitized_env[k] = v
                env = sanitized_env

            servers.append(
                {
                    "name": row["name"],
                    "type": row["type"],
                    "command": row["command"],
                    "args": json.loads(row["args"]) if row["args"] else None,
                    "env": env,
                    "url": row["url"],
                    "active": bool(row["active"]),
                }
            )
        return servers

    def delete_mcp_server(self, name: str):
        """Remove an MCP server configuration"""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
        self.conn.commit()

    def create_conversation(self, title: Optional[str] = None) -> str:
        """Create a new conversation and return its ID"""
        conversation_id = str(uuid.uuid4())
        cursor = self.conn.cursor()

        cursor.execute(
            "INSERT INTO conversations (id, title) VALUES (?, ?)",
            (
                conversation_id,
                title or f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            ),
        )
        self.conn.commit()
        return conversation_id

    def save_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tool_calls: Optional[List[Dict]] = None,
        agent_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        """Save a message to the conversation"""
        message_id = str(uuid.uuid4())
        cursor = self.conn.cursor()

        cursor.execute(
            """
            INSERT INTO messages (id, conversation_id, role, content, tool_calls, agent_name, model_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                agent_name,
                model_name,
            ),
        )
        self.conn.commit()
        return message_id

    def load_history(self, conversation_id: str, limit: int = 50) -> List[Dict]:
        """Retrieve conversation history"""
        cursor = self.conn.cursor()

        cursor.execute(
            """
            SELECT id, role, content, strftime('%Y-%m-%dT%H:%M:%SZ', timestamp) as timestamp, tool_calls, agent_name, model_name
            FROM messages
            WHERE conversation_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        )

        messages = []
        for row in cursor:
            messages.append(
                {
                    "message_id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                    "tool_calls": json.loads(row["tool_calls"])
                    if row["tool_calls"]
                    else None,
                    "agent_name": row["agent_name"],
                    "model_name": row["model_name"],
                }
            )

        return list(reversed(messages))  # Return chronological order

    def get_conversation_history(
        self, conversation_id: str, limit: int = 50
    ) -> List[Dict]:
        """Alias for load_history"""
        return self.load_history(conversation_id, limit)

    def list_conversations(self, limit: int = 20) -> List[Dict]:
        """List recent conversations with their last message preview"""
        cursor = self.conn.cursor()

        cursor.execute(
            """
            SELECT 
                c.id, 
                strftime('%Y-%m-%dT%H:%M:%SZ', c.created_at) as created_at, 
                c.title,
                m.content as last_message
            FROM conversations c
            LEFT JOIN (
                SELECT conversation_id, content, MAX(timestamp)
                FROM messages
                GROUP BY conversation_id
            ) m ON c.id = m.conversation_id
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

        return [dict(row) for row in cursor]

    def get_conversation_title(self, conversation_id: str) -> Optional[str]:
        """Get the title of a specific conversation"""
        cursor = self.conn.cursor()

        cursor.execute(
            "SELECT title FROM conversations WHERE id = ?",
            (conversation_id,),
        )

        result = cursor.fetchone()
        return result[0] if result else None

    def update_conversation_title(self, conversation_id: str, title: str):
        """Update the title of a conversation"""
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE conversations SET title = ? WHERE id = ?",
                (title, conversation_id),
            )
            self.conn.commit()
        except Exception as e:
            print(f"Error updating conversation title: {e}")
            raise

    def delete_conversation(self, conversation_id: str):
        """Delete a conversation and all its messages"""
        try:
            cursor = self.conn.cursor()
            # Delete messages first due to foreign key
            cursor.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
            )
            cursor.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            self.conn.commit()
        except Exception as e:
            print(f"Error deleting conversation: {e}")
            raise

    def clear_conversation(self, conversation_id: str):
        """Clear all messages from a conversation"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            self.conn.commit()
        except Exception as e:
            print(f"Error clearing conversation: {e}")
            raise

    def save_portfolio_snapshot(
        self,
        total_value: float,
        total_pnl: float,
        cash_balance: float,
        positions: List[Dict],
    ):
        """Save a portfolio snapshot for historical tracking"""
        cursor = self.conn.cursor()

        cursor.execute(
            """
            INSERT INTO portfolio_snapshots (total_value, total_pnl, cash_balance, positions)
            VALUES (?, ?, ?, ?)
            """,
            (total_value, total_pnl, cash_balance, json.dumps(positions)),
        )
        self.conn.commit()

    def get_portfolio_history(self, days: int = 30) -> List[Dict]:
        """Get portfolio historical snapshots"""
        cursor = self.conn.cursor()

        cursor.execute(
            """
            SELECT strftime('%Y-%m-%dT%H:%M:%SZ', timestamp) as timestamp, total_value, total_pnl, cash_balance
            FROM portfolio_snapshots
            WHERE timestamp >= datetime('now', '-' || ? || ' days')
            ORDER BY timestamp ASC
            """,
            (days,),
        )

        return [dict(row) for row in cursor]

    def close(self):
        """Close database connection"""
        self.conn.close()
