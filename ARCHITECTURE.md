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

    subgraph "AI Processing Layer"
        COORD[Coordinator Agent<br/>Intent Classification]
        PORT[Portfolio Agent<br/>Analysis Engine]
        QUANT[Quant Agent<br/>Technical Analysis]
        FORECAST[Forecasting Agent<br/>Price Prediction]
        RESEARCH[Research Agent<br/>News Analysis]
        DECISION[Decision Agent<br/>LLM Integration]
    end

    UI --> API
    API --> CACHE
    API --> DB
    API --> COORD
    COORD --> PORT
    COORD --> QUANT
    COORD --> FORECAST
    COORD --> RESEARCH
    PORT & QUANT & FORECAST & RESEARCH --> DECISION

    PORT --> T212
    QUANT --> ALP
    QUANT --> YF
    FORECAST --> ALP
    RESEARCH --> NEWS
    RESEARCH --> TAVILY
    DECISION --> OPENAI
    DECISION --> GEMINI

    style UI fill:#e1f5fe
    style API fill:#f3e5f5
    style COORD fill:#e8f5e8
    style DECISION fill:#fff3e0
```

### Core System Components

| Component | Technology | Purpose | Scalability |
|-----------|------------|---------|-------------|
| **Frontend** | SwiftUI + Combine | User interface and interaction | Single-user desktop app |
| **API Gateway** | FastAPI + Uvicorn | Request routing and authentication | Horizontal scaling via load balancer |
| **AI Orchestrator** | Python Asyncio | Agent coordination and workflow | Vertical scaling on powerful hardware |
| **Data Cache** | In-memory | Response caching and session storage | Redis planned for scaling |
| **Local Storage** | SQLite | Chat history and user preferences | Single-user, file-based |
| **External APIs** | REST/GraphQL | Market data and AI model access | Rate-limited, with circuit breakers |

---

## 2. Financial Precision Layer (2026 SOTA)
To eliminate "one-cent drift" and binary floating-point errors common in financial apps, Growin implements a dedicated Precision Layer.

- **Engine**: All monetary calculations use Python `decimal.Decimal`.
- **Initialization**: Decimals are initialized exclusively from string representations.
- **Rounding**: Standardized on `ROUND_HALF_UP` (Commercial Rounding).
- **Scale**: Intermediate calculations use 4 decimal places; display outputs are quantized to 2 places (`0.01`).

---

## 3. Frontend Architecture

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

### View Hierarchy & Data Flow

#### MVVM Architecture Implementation
- **Models**: Pure data structures with Codable conformance
- **ViewModels**: ObservableObjects managing business logic and API calls
- **Views**: SwiftUI components focused on presentation and user interaction

#### State Management Strategy
```swift
// Observable Object Pattern
class PortfolioViewModel: ObservableObject {
    @Published var snapshot: PortfolioSnapshot?
    @Published var isLoading = false
    @Published var errorMessage: String?

    // Business logic methods
    func fetchPortfolio() async { /* ... */ }
    func calculateMetrics() -> [Metric] { /* ... */ }
}
```

---

## 4. Backend Architecture

### API Gateway & Service Layer
```mermaid
graph LR
    subgraph "API Gateway"
        A[FastAPI App] --> B[CORS Middleware]
        A --> C[Authentication]
        A --> D[Rate Limiting]
        A --> E[Request Logging]
    end

    subgraph "Route Handlers"
        F[Chat Routes] --> G[Message Processing]
        H[Market Routes] --> I[Data Aggregation]
        J[Health Routes] --> K[System Monitoring]
    end

    subgraph "Service Layer"
        L[ChatManager] --> M[SQLite Storage]
        N[CacheManager] --> O[In-memory Backend]
        P[StatusManager] --> Q[System Metrics]
    end

    B --> F
    C --> F
    D --> F
    E --> F

    F --> L
    H --> N
    J --> P

    L --> M
    N --> O
    P --> Q

    style A fill:#e3f2fd
    style F fill:#f3e5f5
    style L fill:#e8f5e8
```

### High-Performance Core & Optimization
Growin utilizes a hybrid processing model where performance-critical operations are offloaded to low-level implementations.

| Domain | Optimization Technique | Benefit |
|--------|------------------------|---------|
| **Ticker Resolution** | Rust `growin_core` Extension | Sub-microsecond symbol mapping |
| **Quant Analysis** | Bolt Optimized Vectorization | 10x-100x speedup in EMA/RSI math |
| **UI Rendering** | SwiftUI Metal-backed Views | 120Hz smooth scrolling for charts |
| **Memory** | 8-bit AFFINE Quantization | Reduced VRAM footprint for local LLMs |

---

## 5. Security Enclave & Agent Sandboxing
As AI agents move toward autonomy, the **Sentinel Security Layer** provides robust guardrails.

### Safe Code Execution
- **Current**: `SafePythonExecutor` uses AST analysis and restricted builtins to execute model-generated "fixes".
- **Roadmap**: Migration to **Docker-based Isolation** for 2026 SOTA agent safety.
  
#### Sandbox Technology Comparison (2026 Strategy)
| Technology | Category | Isolation Level | Fit for Growin |
|------------|----------|-----------------|----------------|
| **Docker / OCI** | Container | OS-level (Cgroups) | **Primary (Implementation Ready)** |
| **libkrun** | MicroVM | Hardware-level | High (Mac-native optimization) |
| **e2b / Firecracker**| MicroVM | Hardware-level | High (Production Scaling) |
| **Wasmtime / WASM** | Language VM | Runtime-level | Medium (Safe but complex interop) |

- **Strategy**: Leverage the **Docker MCP** to execute agent-generated scripts in temporary, isolated containers. This provides a production-ready balance between performance and the hardware-level isolation required for autonomous financial agents.

---

## 6. Specialist Agents Architecture

#### Data Processing Pipeline
```mermaid
graph TD
    A[Raw API Data] --> B[DataFabricator<br/>Centralized IO]
    B --> C[Data Validation]
    C --> D[Business Logic Processing]
    D --> E[Response Formatting]
    E --> F[Cache Storage]
    F --> G[API Response]

    B --> H[Error Handling]
    C --> H
    D --> H

    H --> I[Fallback Data]
    I --> G

    style A fill:#e8f5e8
    style G fill:#fce4ec
    style H fill:#ffebee
```

---

## 7. AI/ML Architecture

### Model Selection & Routing Logic
```mermaid
flowchart TD
    A[User Query] --> B{Privacy Priority?}

    B -->|High| C[Local Models First]
    B -->|Low| D[Remote Models First]

    C --> E{MLX Available?}
    E -->|Yes| F[Use MLX Model]
    E -->|No| G{Ollama Available?}
    G -->|Yes| H[Use Ollama Model]
    G -->|No| I{LM Studio Available?}
    I -->|Yes| J[Use LM Studio Model]
    I -->|No| K[Error: No Local Models]

    D --> L{API Keys Available?}
    L -->|Yes| M[Use Preferred Remote Model]
    L -->|No| N{Fallback to Local}
    N --> E

    F --> O[Process Response]
    H --> O
    J --> O
    M --> O
    K --> P[Graceful Degradation]
    P --> Q[Rule-Based Response]

    style C fill:#e8f5e8
    style D fill:#fff3e0
    style P fill:#ffebee
```

### Coordinator Model Guardrails
The Coordinator Agent uses IBM Granite 4.0 Tiny handling routing to ensure low latency (<100ms classification) and deterministic output.

---

## 8. Data Architecture

### Data Model Hierarchy
```mermaid
classDiagram
    class PortfolioSnapshot {
        +PortfolioSummary summary
        +[Position] positions
    }

    class PortfolioSummary {
        +Int totalPositions
        +Double totalInvested
        +Double currentValue
        +Double totalPnl
        +Double totalPnlPercent
        +CashBalance cashBalance
        +[String: AccountSummary] accounts
    }

    PortfolioSnapshot --> PortfolioSummary
    PortfolioSummary --> AccountSummary
    PortfolioSnapshot --> Position
```

---

## 9. Security Architecture

### Authentication & Authorization
```mermaid
graph TD
    A[API Request] --> B{Valid API Key?}
    B -->|No| C[401 Unauthorized]
    B -->|Yes| D{User Permissions?}

    D -->|No| E[403 Forbidden]
    D -->|Yes| F{Rate Limit Check}

    F -->|Exceeded| G[429 Too Many Requests]
    F -->|OK| H{Request Validation}

    H -->|Invalid| I[400 Bad Request]
    H -->|Valid| J[Process Request]

    style C fill:#ffebee
    style E fill:#ffebee
    style G fill:#fff3e0
    style I fill:#ffebee
    style J fill:#e8f5e8
```

---

## 10. Performance & Scalability Architecture

### Caching Architecture
```mermaid
graph TD
    A[API Request] --> B{Cache Hit?}
    B -->|Yes| C[Return Cached Response]
    B -->|No| D[Process Request]

    D --> E[Generate Response]
    E --> F{Should Cache?}
    F -->|Yes| G[Store in Cache]
    F -->|No| H[Skip Caching]

    G --> I[Return Response]
    H --> I
    C --> I

    style B fill:#e8f5e8
    style G fill:#e8f5e8
    style I fill:#4caf50
```

---

## 11. Error Handling & Resilience Architecture

### Fallback Strategy Implementation
```mermaid
flowchart TD
    A[Primary Service] --> B{Available?}
    B -->|Yes| C[Use Primary]
    B -->|No| D{Secondary Available?}

    D -->|Yes| E[Use Secondary]
    D -->|No| F{Tertiary Available?}

    F -->|Yes| G[Use Tertiary]
    F -->|No| H[Graceful Degradation]

    H --> I{Cached Data?}
    I -->|Yes| J[Return Cached]
    I -->|No| K{Static Fallback?}
    K -->|Yes| L[Return Static]
    K -->|No| M[Error Response]

    style C fill:#e8f5e8
    style E fill:#fff3e0
    style G fill:#ffebee
    style J fill:#e3f2fd
    style L fill:#f3e5f5
    style M fill:#ffebee
```

---

## 12. Deployment & Operations

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

## 13. Testing Architecture

### Test Pyramid Implementation
```mermaid
graph TD
    A[End-to-End Tests<br/>5% of tests] --> B[Integration Tests<br/>20% of tests]
    B --> C[Unit Tests<br/>75% of tests]

    A --> D[User Scenarios<br/>Full app workflows]
    B --> E[Component Integration<br/>API + Database]
    C --> F[Function Testing<br/>Individual methods]
```
