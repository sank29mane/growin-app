
import duckdb
import threading
import time
import json
from typing import Dict, Any

def worker(conn, thread_id):
    for i in range(100):
        try:
            message_data = {
                'id': f"msg-{thread_id}-{i}",
                'correlation_id': f"corr-{thread_id}",
                'sender': f"agent-{thread_id}",
                'subject': "test",
                'payload': {"val": i},
                'timestamp': time.time()
            }
            conn.execute("""
                INSERT INTO agent_telemetry (id, correlation_id, agent_name, subject, payload, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                message_data.get('id'),
                message_data.get('correlation_id'),
                message_data.get('sender'),
                message_data.get('subject'),
                json.dumps(message_data.get('payload')),
                message_data.get('timestamp')
            ))
        except Exception as e:
            print(f"Thread {thread_id} error: {e}")

def main():
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE agent_telemetry (
            id VARCHAR PRIMARY KEY,
            correlation_id VARCHAR,
            agent_name VARCHAR,
            subject VARCHAR,
            payload JSON,
            timestamp TIMESTAMP
        )
    """)
    
    threads = []
    for i in range(10):
        t = threading.Thread(target=worker, args=(conn, i))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
    
    print("Finished without crash")

if __name__ == "__main__":
    main()
