# Phase 34: Hybrid Magentic Architecture (Agent Structured Outputs)

## Objective
Integrate the `magentic` framework into the Growin App's multi-agent architecture as a tactical "Structured Parser Utility." The goal is to completely replace brittle, boilerplate-heavy string parsing (`json.loads()`, regex) across all LLM agents with natively enforced `Pydantic` and `@prompt` structured outputs, without breaking the existing highly-optimized local hardware routing (`LLMFactory` with MLX/LM Studio fallback).

## Current Status
-   **Status**: PLANNED
-   **Assigned**: AI Assistant (Antigravity Engine)
-   **Start Date**: 2026-03-14

## Context & Rationale
Currently, agents like `ResearchAgent` rely on complex LangChain prompts that instruct the LLM to output JSON, which is then manually extracted (e.g., `content.split("\`\`\`json")[1]`) and parsed. This is error-prone and verbose. By integrating the [Magentic](https://magentic.dev/) framework alongside LangChain, we will define native Python functions returning strictly typed Pydantic models. We will keep LangChain for broad Swarm/Orchestrator tasks while delegating the specific "Extract Data to Struct" sub-tasks to Magentic. 

## Technical Requirements

1.  **MAG-01: Framework Integration**
    *   Add `magentic` and `pydantic` to the project's dependency matrix (`uv add magentic pydantic`).
    *   Initialize Magentic to securely consume environment secrets (`.env`) for OpenAI/Anthropic APIs out-of-the-box.
    
2.  **MAG-02: `ResearchAgent` Refactor**
    *   Target the `_generate_smart_query` method in `ResearchAgent.py`.
    *   Replace the raw string-based LangChain prompt and `json.loads` manual extraction logic with a sleek `@prompt` decorator returning a `NewsQueryParams` Pydantic class.
    *   Ensure the fallback `try/except` gracefully handles API failures and retains its integration with NewsData.io.

3.  **MAG-03: Backward Compatibility**
    *   The `LLMFactory` must remain untouched to ensure all other Agents (e.g., QuantAgent, MathGenerator) retain access to Apple Native `mlx.core` and `lmstudio` APIs. Magentic should be utilized only where schema-enforced returns are strictly necessary.

## Execution Steps

### Step 1: Install Dependencies
-   Run `uv pip install magentic`.

### Step 2: Implement Magentic in `ResearchAgent`
-   Create a Pydantic model:
    ```python
    from pydantic import BaseModel
    class NewsQueryParams(BaseModel):
        q: str
        country: str
    ```
-   Implement the `@prompt` decorator:
    ```python
    from magentic import prompt
    @prompt("Generate optimal NewsData.io API query parameters...")
    def generate_news_query(company_name: str, ticker: str) -> NewsQueryParams:
        ...
    ```
-   Integrate this directly into `ResearchAgent._fetch_newsdata()`.

### Step 3: Test and Validation
-   Run the `ResearchAgent` in isolation for a test ticker (`MAG5.L` or `AAPL`).
-   Verify that valid API queries are still successfully hitting `NewsData.io` and articles are returned correctly.
-   Ensure no side-effects break the Swarm context.

## Definition of Done (DoD)
-   [ ] `magentic` is installed and functional.
-   [ ] `ResearchAgent` successfully utilizes Magentic to generate robust Pydantic schemas.
-   [ ] 0 instances of manual `split("```json")` left in the target method.
-   [ ] The system executes flawlessly without degrading latency.
