# Growin Architecture: Comprehensive AI-Powered Portfolio Intelligence Platform

## Executive Summary

**Growin** is a sophisticated financial intelligence platform that combines advanced artificial intelligence with real-time market data to provide intelligent portfolio analysis, automated trading insights, and conversational financial advice. In 2026, it adheres to SOTA best practices for **Agentic Autonomy**, **Financial Precision**, and **Local Inference**.

### System Vision
To democratize sophisticated financial analysis by providing retail investors with institutional-grade portfolio intelligence through an intuitive, AI-powered macOS application optimized for Apple Silicon hardware.

---

## 1. System Context & High-Level Architecture

### System Context Diagram
```mermaid
graph TB
    subgraph "External Environment"
        T212[Trading 212<br/>Portfolio API]
        ALP[Alpaca Markets<br/>Real-time Data]
        YF[yFinance<br/>Historical Data]
        OPENAI[OpenAI API<br/>GPT Models]
        GEMINI[Google Gemini<br/>AI Models]
        NEWS[NewsAPI<br/>Market News]
        TAVILY[TAVILY<br/>Web Search]
    end

    subgraph "Growin Platform"
        UI[macOS SwiftUI<br/>Frontend]
        API[FastAPI Backend<br/>API Gateway]
        CACHE[In-memory Cache<br/>Response Caching]
        DB[SQLite<br/>Chat History]
    end

    subgraph "AI Processing Layer (MAS)"
        COORD[Coordinator Agent<br/>Intent Routing]
        SWARM[Specialist Swarm<br/>Quant, Research, Portfolio]
        DECISION[Decision Moderator<br/>Debate & Synthesis]
    end

    UI --> API
    API --> CACHE
    API --> DB
    API --> COORD
    COORD --> SWARM
    SWARM --> DECISION
    DECISION --> UI

    SWARM --> T212
    SWARM --> ALP
    SWARM --> YF
    SWARM --> NEWS
    SWARM --> TAVILY
    DECISION --> OPENAI
    DECISION --> GEMINI

    style UI fill:#e1f5fe
    style API fill:#f3e5f5
    style COORD fill:#e8f5e8
    style DECISION fill:#fff3e0
```

---

## 2. Financial Precision Layer (2026 SOTA)
To eliminate "one-cent drift" and binary floating-point errors common in financial apps, Growin implements a dedicated Precision Layer.

- **Engine**: All monetary calculations use Python `decimal.Decimal`.
- **Initialization**: Decimals are initialized exclusively from string representations to avoid implicit float conversion.
- **Rounding**: Standardized on `ROUND_HALF_UP` (Commercial Rounding).
- **Scale**: Intermediate calculations use 4 decimal places; display outputs are quantized to 2 places (`0.01`).
- **Validation**: Every price fetch is verified across multiple sources (Alpaca, yFinance, T212) with a `0.5%` variance threshold.

---

## 3. Agentic Reasoning & Collaborative Debate
Instead of a single "Chain of Thought," Growin uses a **Multi-Agent Debate Model**.

### The Debate Phase
1.  **Specialist Analysis**: The Swarm executes in parallel (e.g., `QuantAgent` finds a bullish pattern, while `ResearchAgent` finds negative earnings news).
2.  **Contradiction Identification**: The **Decision Moderator** identifies conflicting signals.
3.  **Synthesis**: The LLM reconciles these signals (e.g., "The technical breakout is strong, but the macro sentiment suggests a trap; recommend caution").

### Structured Telemetry
Every reasoning chain is traceable via **TelemetryData**:
- **Correlation ID**: Links every specialist output to the final user response.
- **Latency Tracking**: High-precision timing for every agent hop.
- **Reasoning Trace**: The `MarketContext` accumulates full agent rationale for auditability.

---

## 4. Frontend Architecture

### Application Structure
```mermaid
graph TD
    A[GrowinApp.swift<br/>App Entry Point] --> B[ContentView.swift<br/>Main Tab Controller]

    B --> C[ChatView<br/>AI Conversation]
    B --> D[PortfolioView<br/>Live Holdings]
    B --> E[DashboardView<br/>Multi-Account Overview]
    B --> F[SettingsView<br/>Configuration]

    C --> G[ChatViewModel<br/>Message Management]
    D --> H[PortfolioViewModel<br/>Data Aggregation]
    E --> I[DashboardViewModel<br/>Cross-Account Analysis]
    F --> J[SettingsViewModel<br/>Configuration State]

    G --> K[AgentClient<br/>Backend Communication]
    H --> K
    I --> K
    J --> K

    K --> L[HTTP/REST API<br/>localhost:8002]

    style A fill:#e3f2fd
    style B fill:#f3e5f5
    style C fill:#e8f5e8
    style D fill:#fff3e0
    style G fill:#fce4ec
    style L fill:#efebe9
```

---

## 5. Security Enclave & Agent Sandboxing
As AI agents move toward autonomy, the **Sentinel Security Layer** provides robust guardrails.

### Safe Code Execution
- **Current**: `SafePythonExecutor` uses AST analysis and restricted builtins to execute model-generated "fixes".
- **Roadmap**: Migration to **Docker-based Isolation** via Docker MCP for 2026 SOTA agent safety.
  
---

## 6. Deployment & Operations

### Deployment Architecture
```mermaid
graph TD
    subgraph "Desktop Application"
        UI[macOS SwiftUI App<br/>localhost:8002]
    end

    subgraph "Local Backend Services"
        API[FastAPI Server<br/>localhost:8002]
        CACHE[(In-memory Cache<br/>Per Session)]
        DB[(SQLite<br/>Chat History)]
    end

    subgraph "External APIs"
        T212[Trading 212 API]
        ALP[Alpaca API]
        OPENAI[OpenAI/Gemini APIs]
        NEWS[NewsAPI/TAVILY]
    end

    UI --> API
    API --> CACHE
    API --> DB

    API --> T212
    API --> ALP
    API --> OPENAI
    API --> NEWS

    style UI fill:#e3f2fd
    style API fill:#f3e5f5
    style CACHE fill:#e8f5e8
    style DB fill:#fff3e0
```

---

## 7. Testing Architecture

### Test Pyramid Implementation
```mermaid
graph TD
    A[End-to-End Tests<br/>5% of tests] --> B[Integration Tests<br/>20% of tests]
    B --> C[Unit Tests<br/>75% of tests]

    A --> D[User Scenarios<br/>Full app workflows]
    B --> E[Component Integration<br/>API + Database]
    C --> F[Function Testing<br/>Individual methods]
```

*Note: Special emphasis is placed on **Financial Precision Tests** to ensure zero regression in Decimal math.*
