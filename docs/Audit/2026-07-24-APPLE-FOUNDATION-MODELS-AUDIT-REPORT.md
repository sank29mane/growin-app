# Architectural Audit Report: Apple Foundation Models vs Growin Inference Stack

| Field | Value |
| --- | --- |
| **Document ID** | `AUDIT-2026-07-24-AFM` |
| **Date** | 2026-07-24 |
| **Status** | Complete — graph + code + public research |
| **Routing** | `ag-intelligent-routing` → architecture + mobile + AI systems audit |
| **Sources** | `.planning/graphs/graph.json`, `docs/ARCHITECTURE.md`, `docs/mac_native_architecture.md`, codebase, Apple Developer docs / WWDC 2025 public materials |
| **Primary question** | How do Apple Foundation Models compare to Growin’s current mechanism, and will they offer benefits? |

---

## 0. Executive Verdict

| Question | Answer |
| --- | --- |
| Should Growin **replace** MLX / LM Studio with Apple Foundation Models? | **No.** Capability ceiling, no custom weights/adapters, finance-domain gap, and multi-agent Python topology make replacement unsafe. |
| Should Growin **replace** CoreML / ANE numeric path with Foundation Models? | **No.** Foundation Models is a text/LLM API, not a tensor runtime. NeuralJMCE, GMM, indicators stay on CoreML/NumPy/MLX. |
| Do Foundation Models offer **net benefits** for Growin? | **Yes, as a complementary edge tier** — intent parsing, structured UI extraction, offline light chat, App Intents/Siri, and low-memory fallback when the Python backend or large MLX models are cold/unavailable. |
| Recommended strategy | **Three-tier hybrid**: Apple FM (edge NLP) + CoreML ANE (numeric) + MLX/LM Studio (sovereign financial reasoning). |

**One-line strategy:** Use Apple’s system foundation model for *cheap, private, Swift-native language plumbing*; keep Growin’s *sovereign quant brain* on MLX + CoreML + agent swarm.

---

## 1. Scope and Method

### 1.1 What “Apple Foundation frameworks” means in this audit

Ambiguity resolved for this report:

| Term | Meaning | In scope? |
| --- | --- | --- |
| **Foundation Models framework** | WWDC 2025 Swift API (`import FoundationModels`, `SystemLanguageModel`, `LanguageModelSession`, `@Generable`, tools) exposing Apple Intelligence’s ~3B on-device LLM | **Primary focus** |
| **Core ML** | Production on-device model runtime (ANE/GPU/CPU) for custom models | Already in Growin; compared as *current numeric path* |
| **MLX** | Open Apple Silicon ML framework used by Growin for open LLMs / VLM / adapters | Already in Growin; compared as *current reasoning path* |
| **Foundation (UIKit/AppKit base)** | Classic Apple `Foundation` types (`URL`, `Data`, etc.) | Out of scope — already used by SwiftUI app |

This audit is **not** about rewriting Swift standard library usage. It evaluates whether Apple’s **on-device foundation model API** should absorb or augment Growin’s AI mechanism.

### 1.2 Evidence bases

1. **Knowledge graph** (`.planning/graphs/graph.json`, report dated 2026-07-19): **4,083 nodes · 8,215 edges · 465 communities**.
2. **Code & design docs**: `llm_factory.py`, `mlx_engine.py`, `coreml_inference.py`, `model_config.py`, architecture blueprints, Phase 46/49 research.
3. **Public research**: Apple Foundation Models developer documentation, WWDC 2025 session summaries, industry comparisons of FM vs MLX/Core ML (2025–2026).

### 1.3 Graph findings (current mechanism topology)

Top structural hubs show a **Python multi-agent MAS**, not a single-model chat app:

| Hub (degree) | Community | Role |
| --- | --- | --- |
| `AgentResponse` (226), `BaseAgent` (213), `AgentConfig` (185) | 0 | Agent contract surface |
| `LLMFactory` (117), `LMStudioClient` (108), `ChatMLX` (26) | 3 | **LLM provider routing** |
| `DecisionAgent` (103), `OrchestratorAgent` (72), `CoordinatorAgent` (61) | 9 | Decision / orchestration bottleneck |
| `PortfolioAnalyzer` (112), `NeuralJMCE` (50), `QuantEngine` (26) | 2 | **Numeric / quant path** |
| `CoreMLRunner` (13) | 29 | ANE CoreML runner (small but explicit) |
| SwiftUI `View` / models | 1 | Frontend |

**Term hits in graph corpus:**

| Topic | Approx. node hits | Interpretation |
| --- | --- | --- |
| agent | ~709 | MAS is the product center of gravity |
| mlx | ~245 | Deep MLX footprint (inference, tests, quant math) |
| llm | ~74 | Explicit LLM plumbing |
| coreml / ane / neural | ~71 / ~40 / ~48 | ANE numeric path is real but smaller than agent graph |
| lm studio | ~18 | Production-facing local server path |
| foundation (FM) | **0** | **No Foundation Models integration today** |

**Implication:** Adopting Foundation Models is a **greenfield Swift-side edge capability**, not a drop-in for communities 0/2/3/9.

---

## 2. Growin’s Current Mechanism (As Built)

### 2.1 Three-brain hardware partition (documented target)

From `docs/mac_native_architecture.md` and `docs/ARCHITECTURE.md`:

| Brain | Hardware | Workload | Primary modules |
| --- | --- | --- | --- |
| **Orchestrator** | CPU (AMX) | FastAPI, agents, JSON, workers | `server.py`, agent package |
| **Reasoner** | GPU / Metal via MLX | LLM/VLM reasoning, QLoRA adapters | `mlx_engine.py`, `mlx_vlm_engine.py`, `mlx_langchain.py` |
| **Math engine** | ANE (NPU) via Core ML | JMCE, regime, low-latency numeric | `coreml_inference.py`, `NeuralJMCE`, Swift `JMCEInference` |

Design rule already in Phase 49 research: **Core ML uses `CPU_AND_NE`** so ANE work does not steal GPU memory from MLX.

### 2.2 Dual decision paths (critical product reality)

Prior profit audits and architecture docs show **two paths**:

```text
PATH A — Advisory MAS (main product)
  SwiftUI chat → FastAPI → Orchestrator / specialists → DecisionAgent (LLM, multi-second)
  → Risk / HITL / MCP broker tools

PATH B — Numerical live loop
  Tick → vol/spread/Welford → GMM regime → adapter hot-swap → pre-flight → execution hooks
  Latency: sub-ms features; swap 10–50ms; not full LLM consensus
```

Foundation Models can assist **Path A UX edges** and **offline intent**. It does not accelerate **Path B** numeric loops.

### 2.3 LLM provider mechanism today

`LLMFactory` (`backend/agents/llm_factory.py`) is a multi-provider router:

1. HuggingFace-style IDs → **LM Studio**
2. Explicit providers: LM Studio, OpenAI, Anthropic, Google, **MLX**, Ollama
3. Fallbacks: LM Studio auto-detect → Native MLX on Apple Silicon

`model_config.py` states production priority for **Apple Silicon native local models**, but several “native-mlx” entries are currently wired through **LM Studio** (`provider: lmstudio`), with direct MLX as a parallel/fallback path via `ChatMLX` / `MLXInferenceEngine`.

**Production reasoning stack characteristics:**

| Property | Current Growin stack |
| --- | --- |
| Model choice | Open weights (Gemma-class MoE, Nemotron, Granite, etc.) |
| Size class | Mid/large local models (tens of B params, quantized) |
| Fine-tuning | QLoRA adapters, regime hot-swap 10–50ms |
| Multimodal | `mlx_vlm` path |
| Orchestration | Multi-agent, LangChain-shaped wrappers, streaming SSE |
| Hosting | Local Python + optional LM Studio process |
| Memory budget | ~28GB inference budget on 48GB M4 Pro (60% rule) |
| Domain | Financial / portfolio / trading agent prompts + adapters |

### 2.4 Core ML / ANE mechanism today

`CoreMLRunner` loads custom `.mlmodel` / `.mlpackage` with ANE priority and is used for **numeric forecasting / indicators**, not chat:

- Predict on feature dicts
- Memory cleanup of multiarray wrappers
- Explicit GPU isolation from MLX

This is **orthogonal** to Foundation Models.

### 2.5 Frontend mechanism

SwiftUI Sovereign UI talks to backend over REST/SSE. Local Core ML exists for some JMCE-style paths. **No** `FoundationModels` import appears in the product graph or source search (aside from this audit).

---

## 3. What Apple Foundation Models Actually Are

### 3.1 Product definition (public 2025–2026 materials)

Apple’s **Foundation Models** framework gives third-party apps access to the **system on-device foundation model** that powers Apple Intelligence features:

| Capability | Detail |
| --- | --- |
| API surface | Swift: `SystemLanguageModel`, `LanguageModelSession`, streaming, tools |
| Structured output | `@Generable` / guided generation → typed Swift structs |
| Tool calling | Developer-defined tools; model can invoke app functions |
| Privacy | On-device inference by default; no app-shipped multi-GB weights |
| Model control | Apple-managed weights; OS-updated; **not** arbitrary open models |
| Approximate scale | Public discourse: **~3B-class** on-device model (highly optimized) |
| Context | Practical guidance: **~4k–8k tokens** class (verify per OS; design for lower end) |
| Availability | Apple Intelligence–eligible hardware, OS, region, language, user settings |
| Private Cloud Compute | Used by **Apple’s own** Apple Intelligence features for harder tasks; **not** a general third-party trading backend |

### 3.2 What Foundation Models are *not*

- Not a replacement for **MLX** open-model serving
- Not a **tensor/Core ML** replacement for JMCE/GMM
- Not a **multi-agent Python swarm** runtime
- Not a path to load **Gemma / Nemotron / custom QLoRA adapters**
- Not an SLA’d finance engine; safety stack may refuse or hedge financial advice

### 3.3 Fit map for Growin feature classes

| Growin capability | FM fit | Keep on current stack |
| --- | --- | --- |
| Intent classification / NL filters | **Strong** | Optional hybrid |
| Structured extraction to Swift models | **Strong (`@Generable`)** | Backend Pydantic remains source of truth for trades |
| Offline light assistant | **Strong** | Backend when deep reasoning needed |
| Multi-agent debate / DecisionAgent | **Weak** | MLX / LM Studio |
| Regime QLoRA adapters | **None** | MLX |
| NeuralJMCE / covariance / GMM | **None** | CoreML / NumPy / MLX |
| Chart/vision VLM | **Limited / different product surface** | `mlx_vlm` |
| Broker MCP / execution | **None (tools only if you wire them carefully)** | Python execution integrity layer |
| HITL Secure Enclave approval | **Orthogonal** | Keep as-is |

---

## 4. Head-to-Head Comparison

### 4.1 Master comparison matrix

| Dimension | Apple Foundation Models | Growin MLX / LM Studio | Growin Core ML ANE |
| --- | --- | --- | --- |
| **Role** | System LLM for app NLP | Sovereign reasoning LLM/VLM | Custom numeric models |
| **Model ownership** | Apple | Growin / open community | Growin-exported packages |
| **Typical size** | ~3B system model | 8–30B+ quantized open models | Small specialized nets |
| **Custom weights** | No | Yes | Yes (`.mlpackage`) |
| **QLoRA / adapters** | No (prompt/tools only) | Yes, 10–50ms hot-swap | N/A / separate export |
| **Primary language** | Swift | Python | Python + Swift |
| **Hardware** | System-scheduled ANE/CPU/GPU | Metal GPU (MLX) | ANE preferred (`CPU_AND_NE`) |
| **App RAM for model** | Shared system model (minimal app cost) | Multi-GB unified memory | Tens of MB typical |
| **Latency class** | Hundreds of ms text gen | Streaming tokens; multi-s agent chains | **&lt;10 ms** numeric targets |
| **Context** | Small (low-k tokens) | Model-dependent; much larger possible | N/A (fixed tensors) |
| **Multimodal** | Apple-defined surface | Explicit `mlx_vlm` | Custom vision models if exported |
| **Offline** | Yes if Apple Intelligence available | Yes if local weights loaded | Yes |
| **Agent swarm** | Single session + tools | Full MAS (graph communities 0/9) | No |
| **Finance specialization** | General; policy friction risk | Prompt + adapter + tools | Quant math only |
| **Vendor lock / gates** | High (OS, region, eligibility) | Medium (Apple Silicon preferred) | Medium |
| **Ops burden** | Very low | High (memory, processes, model mgmt) | Medium (export/validate) |
| **Graph presence today** | **0 nodes** | Dominant in comm 3 + MLX tests | Comm 2 + 29 |

### 4.2 Capability ceiling for trading intelligence

```text
Capability (qualitative)

Frontier cloud LLMs  ████████████████████  (optional Growin fallbacks)
Growin large local   ████████████████░░░░  (Gemma/Nemotron-class + adapters)
Apple FM on-device    ████████░░░░░░░░░░░░  (~3B system, great for structure)
Core ML numeric      ████████████████████  (for its domain: tensors only)
```

Growin’s product thesis is **sovereign, domain-tuned, multi-agent portfolio intelligence**. Apple FM optimizes for **private, efficient, OS-integrated general language tasks**. Overlap is real but partial.

### 4.3 Resource & process model

| Concern | Foundation Models | Current Growin |
| --- | --- | --- |
| Process footprint | In-app Swift; system model shared | Python backend + often LM Studio + Redis/DuckDB |
| Cold start | Fast if system model ready | Heavy (Python, model load, Metal) |
| Concurrent agents | Poor fit for 6+ parallel specialist LLMs | Designed for swarm (memory is the limit) |
| Battery / thermals | System-optimized | Sustained dual large models can heat M-series |
| Determinism | OS model updates can change behavior | You pin open weights / adapters |

---

## 5. Will Foundation Models Offer Benefits?

### 5.1 Benefits that are real for Growin

1. **Zero-weight UX intelligence**  
   Parse NL into typed filters, watchlist queries, and UI navigation without loading Gemma-class weights.

2. **`@Generable` reliability at the UI boundary**  
   Map “show high-vol bullish LSE ETFs last week” → Swift struct without brittle regex; backend still validates.

3. **Backend-independent degraded mode**  
   When Python is down, models unloading, or memory pressure high, users still get light assistance and explanations of *cached* portfolio state (if you feed local data as context carefully).

4. **App Intents / Siri / Shortcuts surface**  
   Natural fit for “what’s my NAV?” / “open risk panel” style actions without inventing a second NL stack.

5. **Privacy marketing alignment**  
   Reinforces local-first story for *personal* text processing; still not a substitute for audit logs on trades.

6. **Thermal / memory relief**  
   Offload trivial NLP from the 28GB MLX budget so GPU stays free for DecisionAgent / VLM / dual-model paths.

7. **Lower ops for edge features**  
   No HF download, no quant recipe, no adapter matrix for simple extraction tasks.

### 5.2 Benefits that are *overstated* or false for Growin

| Claim | Reality for Growin |
| --- | --- |
| “Replace MLX and save all that complexity” | Loses adapters, model choice, VLM depth, multi-agent quality |
| “On-device = better trading decisions” | FM is not finance-tuned; numeric edge remains CoreML/quant |
| “Private Cloud Compute for our agents” | PCC is not a general third-party agent backend |
| “One session can be the whole swarm” | Context + capability too small for current Orchestrator topology |
| “No more safety work” | Finance refusals + App Review still apply; you still need HITL |

### 5.3 Benefit scorecard (Growin-weighted)

| Use case | Benefit score (0–5) | Notes |
| --- | --- | --- |
| NL → structured UI filters | **5** | Best ROI |
| Intent routing before backend | **4** | Complements R-Stitch / SLM routing |
| Offline light chat | **4** | Degraded mode |
| App Intents / Siri | **4** | Platform leverage |
| DecisionAgent replacement | **1** | Capability / adapter gap |
| RiskAgent critic | **1** | Policy + quality risk |
| NeuralJMCE / GMM | **0** | Wrong abstraction |
| Adapter regime learning | **0** | Unsupported |
| Multi-agent synthesis | **1–2** | Tool loop only for simple cases |

**Net:** Material benefits on the **UX/edge layer**; negligible-to-negative if forced onto the **alpha/decision core**.

---

## 6. Pros and Cons (Deep Dive)

### 6.1 Pros of using Apple Foundation Models

| # | Pro | Why it matters to Growin |
| --- | --- | --- |
| P1 | **Minimal memory tax** | Protects 60% unified-memory budget for real models |
| P2 | **Swift-native DX** | `@Generable`, async sequences, fewer JSON glue bugs |
| P3 | **System-updated quality** | Apple improves model with OS releases without Growin re-export |
| P4 | **Strong privacy posture** | On-device by default for supported tasks |
| P5 | **Tool calling for app actions** | Can call non-money-critical app functions (navigate, filter, summarize) |
| P6 | **Power efficiency** | Better for always-on UI helpers than keeping 26B warm |
| P7 | **Competitive polish** | Feels “Mac-native AI” next to Writing Tools / Intelligence ecosystem |
| P8 | **Faster time-to-feature for NLP chrome** | Avoid standing up another small open model just for parsing |

### 6.2 Cons of using Apple Foundation Models

| # | Con | Growin impact |
| --- | --- | --- |
| C1 | **No custom / open weights** | Breaks sovereignty of model choice and eval harness |
| C2 | **No QLoRA regime adapters** | Direct conflict with Phase 46 adaptive learning story |
| C3 | **Smaller capability ceiling** | Weaker multi-step financial reasoning vs Gemma/Nemotron-class |
| C4 | **Small context window** | Bad fit for long RAG + multi-agent traces |
| C5 | **Eligibility gates** | Region, language, hardware, user toggle → mandatory fallbacks |
| C6 | **Finance safety / refusals** | May hedge or refuse investment-like content |
| C7 | **Non-deterministic across OS updates** | Harder to pin behavior for regression tests vs open weights |
| C8 | **Python MAS impedance mismatch** | Swarm, governance, MCP, DuckDB live in Python; FM is Swift-first |
| C9 | **Not a numeric engine** | Cannot replace CoreML JMCE path |
| C10 | **App Review & advice liability** | Still on Growin; FM does not reduce regulatory duty |
| C11 | **Tool-use ≠ execution integrity** | Must not bypass HITL, pre-flight, risk gates (project non-negotiables) |
| C12 | **Testing complexity** | Simulator/device matrix; availability flaky in CI |

### 6.3 Risk matrix (adoption)

| Risk | Likelihood | Severity | Mitigation |
| --- | --- | --- | --- |
| Accidental use for trade decisions | Medium | **Critical** | Hard policy: FM never places orders; only UI/intent |
| Over-trust of structured output | Medium | High | Backend re-validate all financial structs |
| Availability false dependency | High | Medium | Feature-detect + fallback to keyword / backend SLM |
| Policy refusal in finance copy | Medium | Medium | Template responses; don’t rely on FM for compliance text |
| Memory contention with system Intelligence | Low–Med | Medium | Profile on M4 Pro under dual MLX load |
| Scope creep “rewrite agents in Swift” | Medium | High | Cap scope to edge NLP phases |

---

## 7. Architectural Recommendation

### 7.1 Target hybrid topology

```text
                         User utterance / gesture
                                   │
                 ┌─────────────────┴─────────────────┐
                 ▼                                   ▼
     ┌───────────────────────┐           ┌──────────────────────────┐
     │ Apple Foundation Models│           │  Direct UI / hotkeys     │
     │ (optional edge tier)   │           └──────────────────────────┘
     │ - Intent + @Generable  │
     │ - Offline light chat   │
     │ - App Intents          │
     └───────────┬───────────┘
                 │ structured IntentDTO (non-authoritative)
                 ▼
     ┌───────────────────────┐
     │ FastAPI + Governance  │◄── always authoritative for money paths
     └───────────┬───────────┘
         ┌───────┼────────┬────────────────┐
         ▼       ▼        ▼                ▼
      Agents   MLX/LM   CoreML ANE      Execution
      (MAS)   Studio    NeuralJMCE      HITL/MCP
```

### 7.2 Decision rules

| If the task is… | Use |
| --- | --- |
| Parsing UI language, summarizing a short note, generating a typed filter | **Foundation Models** (preferred) |
| Multi-agent research, decision, risk narrative, strategy synthesis | **MLX / LM Studio** |
| Covariance, regime, indicators, forecasts as tensors | **Core ML ANE / numeric stack** |
| Order intent, sizing, broker calls | **Execution integrity stack only** (never FM-direct) |

### 7.3 Phased adoption (suggested)

| Phase | Work | Exit criteria |
| --- | --- | --- |
| **A — Spike** | `#if canImport(FoundationModels)` availability probe; hello `@Generable` | Works on target M4 macOS with AI enabled; graceful fail otherwise |
| **B — Intent edge** | Map chat composer prefix / command bar to `IntentDTO` | ≥95% parse success on curated NL suite; zero order side effects |
| **C — Offline mode** | Light Q&A over *locally cached* portfolio snapshot | Backend down still yields safe read-only UX |
| **D — App Intents** | “Open ledger”, “summarize day” (non-trading) | Siri/Shortcuts demos without trade authority |
| **E — Explicit non-goals** | Document forever: no DecisionAgent replacement, no adapter path | Enforced in `PROJECT_RULES` / architecture ADRs |

**Do not** schedule a phase that “migrates the swarm to Foundation Models.”

### 7.4 Integration sketch (non-normative)

```swift
// Conceptual only — verify against current SDK
import FoundationModels

@Generable
struct GrowinUIIntent {
    var action: String          // e.g. "filter_watchlist"
    var tickers: [String]
    var regimeHint: String?
    var timeRangeDays: Int?
}

func parseIntent(_ text: String) async -> GrowinUIIntent? {
    guard SystemLanguageModel.default.isAvailable else { return nil }
    let session = LanguageModelSession()
    return try? await session.respond(to: text, generating: GrowinUIIntent.self)
}
```

Backend must re-validate any ticker lists, ranges, and never treat this as a trade ticket.

---

## 8. Comparison to “Current Mechanism” by Layer

### 8.1 Language reasoning layer

| | Current | With FM added |
| --- | --- | --- |
| Heavy reasoning | MLX / LM Studio open models | Unchanged |
| Light reasoning | Often still hits backend | **Shift to FM** |
| Structured JSON | Prompt + parsers / Pydantic | **FM `@Generable` at edge** + Pydantic at core |

### 8.2 Numeric layer

**No change.** Phase 49 ANE isolation remains correct. FM does not participate.

### 8.3 Agent orchestration layer

Graph communities 0 and 9 remain Python-centric. FM tools could *trigger* agent runs (e.g. “run research on XYZ”) but orchestration, governance, ACE, risk gates stay backend.

### 8.4 Learning / adaptation layer

Phase 46 QLoRA + DuckDB feature pipeline is a **core differentiator**. FM cannot host this. Attempting to abandon adapters for FM would **regress** Growin’s adaptive regime story.

---

## 9. Security, Compliance, and Project Non-Negotiables

Aligned with `AGENTS.md` / cookbook:

1. **No bypass of risk, simulation, approval, auth, or security** for integration convenience.
2. **No real trades** from experimental FM tools without explicit authorization + pre-flight.
3. FM outputs are **untrusted input** — same as user text or web content.
4. Prefer FM for **read-only** and **UI** actions initially.
5. Audit log any FM→backend action that could influence trading workflows.

Finance-specific public research consensus: Apple Intelligence / Foundation Models are **poor primary engines** for automated trading signals, but **reasonable** for private NLP chrome around a specialized system.

---

## 10. Alternatives Considered

| Alternative | Verdict |
| --- | --- |
| **Full replacement of MLX with FM** | Reject — capability, adapters, VLM, swarm |
| **Full replacement of CoreML with FM** | Reject — wrong abstraction |
| **Status quo only (no FM)** | Acceptable short-term; leaves UX efficiency on table |
| **Hybrid edge FM (recommended)** | Best ROI / risk balance |
| **Core ML LLM conversion of open models** | Possible for small SLMs on ANE; still not Apple FM; evaluate separately if MLX memory pressure forces it |
| **Cloud-only for light NLP** | Conflicts with local-first privacy thesis |

---

## 11. Open Questions / Verification Gaps

1. Exact **context window**, **rate limits**, and **availability APIs** on the project’s target macOS SDK — re-check `developer.apple.com/documentation/foundationmodels` at implementation time.
2. Behavior of system guardrails on **portfolio / ETF / leverage** prompts (refusal rates).
3. Concurrent load: FM + dual MLX models + ANE JMCE under memory pressure on **M4 Pro 48GB**.
4. Whether future Apple APIs expose **adapters** (rumored/evolving) — if so, re-open Phase E; today treat as unavailable.
5. CI strategy: how to test `#if canImport` paths without Apple Intelligence hardware in all runners.

---

## 12. Final Recommendations

1. **Do not replace** the current MLX / LM Studio / multi-agent mechanism with Apple Foundation Models.
2. **Do not replace** Core ML ANE numeric forecasting with Foundation Models.
3. **Do adopt** Foundation Models as an **optional Swift edge tier** for intent parsing, structured UI generation, offline light assistance, and App Intents.
4. **Encode hard boundaries**: FM never authorizes money movement; backend governance remains source of truth.
5. **Preserve** QLoRA adapters, NeuralJMCE, and swarm orchestration as competitive moat.
6. **Update architecture docs** when Phase A spike lands; keep this audit as the decision record.

### Verdict table

| Layer | Action |
| --- | --- |
| Apple Foundation Models | **Adopt selectively (edge)** |
| MLX / LM Studio reasoning | **Keep primary** |
| Core ML ANE numeric | **Keep primary** |
| Multi-agent Python MAS | **Keep primary** |
| Full migration to Apple FM | **Reject** |

---

## 13. Evidence Appendix

### 13.1 Graph snapshot (2026-07-19)

- Corpus: 348 files · ~440k words · 4083 nodes · 8215 edges
- LLM hub community **3**: `LLMFactory`, `LMStudioClient`, `ChatMLX`, `MathGeneratorAgent`
- Quant community **2**: `PortfolioAnalyzer`, `NeuralJMCE`, `QuantEngine`
- UI community **1**: SwiftUI models/views
- CoreML community **29**: `CoreMLRunner` + phase 46 tests
- Foundation Models nodes: **0**

### 13.2 Key code / docs

- `backend/agents/llm_factory.py` — multi-provider LLM creation
- `backend/mlx_engine.py` — MLX load/generate/memory
- `backend/coreml_inference.py` — ANE `CPU_AND_NE` runner
- `backend/model_config.py` — local-first model registry
- `docs/mac_native_architecture.md` — three-brain model
- `docs/model-selection-playbook.md` — dual-model + adapter policy
- `docs/ARCHITECTURE.md` — MAS + MLX + ANE system diagram
- Phase 49 research — ANE isolation from MLX GPU

### 13.3 External research (public)

- Apple Developer Documentation: Foundation Models framework  
  https://developer.apple.com/documentation/foundationmodels
- WWDC 2025 sessions (public summaries): Meet Foundation Models; Generable/structured output; tool calling
- Industry comparisons (2025–2026): Apple FM vs MLX vs Core ML — system model efficiency vs open-model flexibility vs production custom inference
- Finance-use caveats: on-device models suitable for private NLP; unsuitable as sole engines for automated trading decisions

---

## 14. Sign-off

| Check | Status |
| --- | --- |
| Graph topology reviewed | Yes |
| Current code paths reviewed | Yes |
| Public FM research synthesized | Yes |
| Pros/cons enumerated | Yes |
| Benefits scored by use case | Yes |
| Hybrid recommendation with hard non-goals | Yes |
| Security / execution integrity respected | Yes |

**Storage path:** `docs/Audit/2026-07-24-APPLE-FOUNDATION-MODELS-AUDIT-REPORT.md`

---

*End of audit report.*
