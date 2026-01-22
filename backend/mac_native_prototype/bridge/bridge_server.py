from fastapi import FastAPI
from pydantic import BaseModel
import httpx

app = FastAPI(title="ANE Bridge (Prototype)")

class ForwardRequest(BaseModel):
    path: str
    method: str = "GET"
    body: dict | None = None

BACKEND_BASE = "http://127.0.0.1:8002"  # primary backend

@app.post("/ane/forward")
async def ane_forward(req: ForwardRequest):
    url = f"{BACKEND_BASE}{req.path}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        if req.body is None:
            r = await client.request(req.method, url)
        else:
            r = await client.request(req.method, url, json=req.body)
    return {
        "status": r.status_code,
        "content": r.text
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8004)
