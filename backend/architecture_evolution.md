# Architecture Evolution Plan

## Phase 4: Production Readiness & Scale

### 1. Containerization (Docker)
Goal: Reproducible deployments and isolated environments.

#### Dockerfile Strategy
Create a multi-stage build to keep image size small.
- **Base Image**: `python:3.10-slim`
- **Builder Stage**: Install build dependencies (gcc, etc.) and compile requirements.
- **Runtime Stage**: Copy virtualenv/site-packages and app code.

#### Docker Compose
Define services for:
- `backend`: The FastAPI application.
- `chromadb`: (Optional) Run Chroma as a standalone server mode if scaling beyond local file.
- `redis`: For caching (replace simple dict cache).

**Example Structure:**
```yaml
version: '3.8'
services:
  backend:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
```

### 2. Monitoring & Observability
Goal: Real-time insights into agent performance and latency.

#### OpenTelemetry (OTEL)
- **Tracing**: Instrument `GlobalTracer` to trace requests through FastAPI -> DecisionAgent -> LLM.
- **Spans**: Create custom spans for each "Thinking" step and "Tool Call".
- **Exporter**: Send traces to Jaeger or Honeycomb.

#### Prometheus Metrics
- **Latency**: Histogram of `agent_execution_time`.
- **Success Rate**: Counter for `agent_success` vs `agent_failure`.
- **Token Usage**: Counter for input/output tokens (cost tracking).

#### Dashboard (Grafana)
- Visualize `DecisionAgent` latency vs. complexity.
- Alert on `primary_llm_failure` (fallback activation rate).

### 3. CI/CD Pipeline
- **Linting**: Pylint/Ruff.
- **Testing**: Pytest with 80% coverage.
- **Build**: Auto-build Docker image on push to main.

### 4. Scalability
- **Task Queue**: Move long-running analysis (ResearchAgent) to Celery/Redis.
- **Vector DB**: Migrate local Chroma to managed Chroma/Pinecone if data grows > 1GB.
- **Load Balancing**: Nginx in front of multiple backend replicas.
