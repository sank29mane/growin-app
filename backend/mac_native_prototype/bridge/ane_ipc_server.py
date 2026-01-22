import asyncio
import json

class ANEIPCServer:
    """Minimal IPC server using Unix Domain Sockets to route ANE tasks.

    This is a scaffold. It currently returns a mocked response and is intended
    to be replaced by real on-device Core ML inference calls via Core ML Tools
    once a model is wired and the SwiftUI frontend is integrated.
    """
    def __init__(self, socket_path: str = "/tmp/growin_ane.sock"):
        self.socket_path = socket_path
        self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            if not data:
                writer.close()
                await writer.wait_closed()
                return
            payload = data.decode().strip()
            try:
                req = json.loads(payload)
            except Exception:
                req = {"error": "invalid_json"}
            # Simple echo/fallback response; replace with Core ML on-device call
            response = {
                "status": "ok",
                "echo": req,
                "result": {}
            }
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        except Exception as e:
            err = {"status": "error", "message": str(e)}
            writer.write((json.dumps(err) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self):
        # Ensure old socket removed
        import os
        try:
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)
        except Exception:
            pass
        self._server = await asyncio.start_unix_server(self._handle_client, path=self.socket_path)
        async with self._server:
            await self._server.serve_forever()

    def run_in_background(self, loop=None):
        if loop is None:
            loop = asyncio.get_event_loop()
        loop.create_task(self.start())

__all__ = ["ANEIPCServer"]
