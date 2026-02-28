# Research: LM Studio 0.4.x (V1) Management API

**Researched:** 2026-02-15
**Domain:** Local LLM Management & Inference
**Confidence:** HIGH

## Summary
LM Studio 0.4.x (V1) introduced a robust management API (`/api/v1/*`) that complements its existing OpenAI-compatible endpoints (`/v1/*`). This API provides granular control over model loading, unloading, and configuration, supporting advanced features like **Continuous Batching (Parallel Inference)** and **Stateful Chat Sessions**.

**Primary recommendation:** Use `/api/v1/models` to manage model lifecycle (load/unload) and determine current VRAM state, but continue using `/v1/chat/completions` for standard inference to maintain compatibility with most LLM client libraries.

## Standard Stack

### Core
| Library/Tool | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| LM Studio | 0.4.x (V1) | Local Inference Server | Industry standard for local GGUF/MLX inference on Mac/Windows/Linux. |
| OpenAI Python SDK | Latest | Inference Client | Widely compatible with LM Studio's `/v1` endpoints. |
| httpx / aiohttp | Latest | Management Client | Used for calling the native `/api/v1` management endpoints. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| @lmstudio/lms-client-node | 0.4.x | Official SDK | For Node.js-based management. |

## Management API Specification (V1)

### 1. Listing Models: `GET /api/v1/models`
Returns all models known to LM Studio (downloaded), regardless of whether they are loaded.

- **Endpoint:** `http://localhost:1234/api/v1/models`
- **Top-level key:** `data` (within a response object where `object: "list"`).
- **'Loaded' Detection:** 
  - Does **NOT** contain a `loaded` boolean.
  - Instead, contains a **`loaded_instances`** array.
  - **Logic:** If `loaded_instances.length > 0`, the model is loaded.
  - Legacy `state` string (`"loaded"`, `"not-loaded"`) is available in the `/api/v0/` compatibility layer but replaced by `loaded_instances` in `v1`.

**Response Example:**
```json
{
  "object": "list",
  "data": [
    {
      "id": "lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF",
      "object": "model",
      "type": "llm",
      "publisher": "lmstudio-community",
      "arch": "llama",
      "compatibility_type": "gguf",
      "quantization": "Q4_K_M",
      "max_context_length": 131072,
      "loaded_instances": [
        {
          "instance_id": "my-llama-instance",
          "status": "loaded",
          "gpu": "max",
          "context_length": 8192
        }
      ]
    }
  ]
}
```

### 2. Loading Models: `POST /api/v1/models/load`
Programmatically loads a model into memory with specific parameters.

- **Endpoint:** `http://localhost:1234/api/v1/models/load`
- **Identifier Key:** **`model`** (the full model path/id).
- **Configuration Keys (snake_case):**
  - **`gpu`**: (string/number) Values: `"max"`, `"off"`, or `0.0` to `1.0` (decimal percentage of layers).
  - **`context_length`**: (number) Token window size.
  - **`identifier`**: (string, optional) Custom name for the instance.
  - **`ttl`**: (number, optional) Auto-unload after $N$ seconds of inactivity.
  - **`flash_attention`**: (boolean, optional) Enable optimized attention.

**Request Payload:**
```json
{
  "model": "lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF",
  "gpu": "max",
  "context_length": 8192,
  "identifier": "main-llm",
  "ttl": 3600
}
```

### 3. Unloading Models: `POST /api/v1/models/unload`
Frees memory by unloading a specific instance.

- **Endpoint:** `http://localhost:1234/api/v1/models/unload`
- **Identifier Key:** **`instance_id`**.
- **Note:** You must use the `instance_id` (either the `identifier` you provided or the auto-generated UUID from `load`).

**Request Payload:**
```json
{
  "instance_id": "main-llm"
}
```

### 4. Stateful Chat: `POST /api/v1/chat`
LM Studio 0.4.x introduced a native stateful chat API that manages conversation context on the server side, eliminating the need to resend full history.

- **Endpoint:** `http://localhost:1234/api/v1/chat`
- **State Management:**
    - **`store`**: (boolean, default: `true`) If true, LM Studio stores the thread and returns a `response_id`.
    - **`previous_response_id`**: (string, optional) Reference a previous `response_id` (starts with `resp_`) to continue or branch a conversation.
- **Request Body Highlights:**
    - **`model`**: Identifier for the model instance.
    - **`input`**: Can be a string or an array of objects (supporting text and base64 images).
    - **`system_prompt`**: Sets model behavior.
    - **`integrations`**: List of enabled plugins or ephemeral MCP servers.
    - **`reasoning`**: Controls reasoning effort (`"off"`, `"low"`, `"medium"`, `"high"`, `"on"`).
- **Response Body Highlights:**
    - **`response_id`**: Unique identifier for the turn (starts with `resp_`).
    - **`output`**: Array of items generated:
        - `type: "message"`: Text response.
        - `type: "tool_call"`: Model-generated tool call.
        - `type: "reasoning"`: Internal reasoning chain (CoT).
    - **`stats`**: Detailed performance metrics including `reasoning_output_tokens` and `model_load_time_seconds`.

**Request Example (Continue Conversation):**
```json
{
  "model": "ibm/granite-4-micro",
  "input": "What color did I just mention?",
  "previous_response_id": "resp_abc123xyz...",
  "temperature": 0
}
```

## Architecture Patterns

### Recommended Lifecycle Pattern
1. **Discover**: Call `GET /api/v1/models` to find the desired model.
2. **Ensure Loaded**: Check if `loaded_instances` is empty.
3. **Load if Needed**: Call `POST /api/v1/models/load` if not loaded.
4. **Predict**: Call `/v1/chat/completions` using the `model` id or `instance_id`.
5. **Clean up**: Call `POST /api/v1/models/unload` when done, or rely on `ttl` for auto-cleanup.

### Differences: `/api/v1` vs `/v1`

| Feature | `/v1` (OpenAI Compatible) | `/api/v1` (Native V1) |
|---------|----------------------------|------------------------|
| **Scope** | Inference focus | Management focus |
| **Model List** | Only shows loaded models | Shows all downloaded models |
| **Loading** | N/A (Manual or Auto-load) | Programmatic explicit load |
| **State** | Stateless | Stateful (supports session IDs) |
| **Parallelism** | Sequential (Legacy) | Continuous Batching (Native) |

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Parallel Queuing | Custom Semaphore | LM Studio Slots | 0.4.x supports continuous batching. Loading a model with multiple slots handles concurrency natively. |
| Model Caching | Memory Manager | `ttl` parameter | LM Studio handles auto-unload based on inactivity via the `ttl` field in the load request. |
| Quant Discovery | Metadata Parser | `/api/v1/models` | The native API provides `quantization` and `arch` fields directly. |

## Common Pitfalls

### Pitfall 1: Case Sensitivity and Key Naming
**What goes wrong:** Using `gpuOffload` or `contextLength` (camelCase) from older unofficial docs.
**Why it happens:** 0.4.x V1 API standardized on snake_case for management keys.
**How to avoid:** Always use `gpu` and `context_length`.

### Pitfall 2: Unloading by Model ID
**What goes wrong:** Sending the model path (e.g., `publisher/repo`) to `/unload`.
**Why it happens:** In 0.4.x, you can have multiple instances of the same model.
**How to avoid:** Use the `instance_id` returned by the load command or found in `loaded_instances`.

### Pitfall 3: Flash Attention Compatibility
**What goes wrong:** Enabling `flash_attention: true` on incompatible hardware/engines.
**How to avoid:** Check `compatibility_type` (e.g., `gguf`) before enabling.

## Code Examples (httpx/Python)

### List Loaded Model IDs
```python
async def get_loaded_ids(base_url):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/api/v1/models")
        data = resp.json().get("data", [])
        return [m["id"] for m in data if len(m.get("loaded_instances", [])) > 0]
```

### Programmatic Load
```python
async def load_model(base_url, model_id):
    payload = {
        "model": model_id,
        "gpu": "max",
        "context_length": 8192,
        "echo_load_config": True
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/api/v1/models/load", json=payload)
        return resp.json().get("instance_id")
```

## Sources

### Primary (HIGH confidence)
- **LM Studio 0.4.0 Release Docs** - Official V1 API specification.
- **@lmstudio/lms-client-node Source** - Verification of `instance_id` and snake_case keys.

### Secondary (MEDIUM confidence)
- **LM Studio Desktop API Reference Tab** - Real-time endpoint verification in current builds.

## Metadata
**Confidence breakdown:**
- Standard stack: HIGH - LM Studio is the definitive local provider.
- Architecture: HIGH - V1 API patterns are stable in 0.4.x.
- Pitfalls: HIGH - Common migration issues from v0/beta noted.

**Research date:** 2026-02-15
**Valid until:** 2026-05-15 (Stable branch release cycle)
