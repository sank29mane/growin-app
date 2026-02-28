# Phase 14: Dynamic LM Studio Model Management - SPEC

**Objective:** Enable users to dynamically select and load local LLMs from LM Studio through the Preferences UI, providing real-time visual feedback on the loading status and integrating model lifecycle events into the system's activity logs.

## 1. Functional Requirements

### 1.1 Model Selection & Loading
- **LMS-01: Dynamic Listing**: The "Decision Engine" dropdown in the Preferences UI must fetch and display the current list of downloaded models from the LM Studio V1 API.
- **LMS-02: Triggered Loading**: Selecting a model from the dropdown (that is not already loaded) must send a `POST` request to the backend to trigger LM Studio's `/api/models/load` endpoint.
- **LMS-03: Auto-Detection**: Support an "Auto-Detect" mode that identifies the currently loaded model in LM Studio if no specific model is selected.

### 1.2 Status & Feedback
- **LMS-04: Real-time Indicators**: The UI must display a `ProgressView` (spinner) and "Loading..." text while a model is being initialized.
- **LMS-05: Status Polling/Push**: The backend must track the loading progress and expose it via a status endpoint or telemetry events.
- **LMS-06: Activity Logging**: All model lifecycle events (listing, loading, ready, error) must be recorded in the `StatusManager` and visible in the app's Activity Log.

### 1.4 Advanced Inference
- **LMS-09: Stateful Chat Support**: Leverage the `POST /api/v1/chat` endpoint to maintain conversation state on the server side using `response_id`.
- **LMS-10: Reasoning Trace Integration**: Extract and display the `reasoning` output from LM Studio (CoT) in the app's Reasoning Trace UI.

## 2. Technical Requirements

### 2.1 Backend (Python/FastAPI)
- **Endpoints**:
    - `GET /api/models/lmstudio`: List models.
    - `POST /api/models/lmstudio/load`: Load model.
    - `GET /api/models/lmstudio/status`: Get detailed health/load status.
- **Client**: Update `LMStudioClient` to support:
    - Native V1 model management.
    - Stateful chat via `previous_response_id`.
    - Reasoning extraction from structured output.
- **Persistence**: Store `lm_studio_response_id` in `ChatManager` (SQLite) per message to allow context-aware branching.

### 2.2 Frontend (SwiftUI)
- **ViewModel**: Create `LMStudioViewModel` to encapsulate model management logic.
- **UI Components**: 
    - Integrate the new Stitch-generated design patterns for the Preferences tabs and Activity Log rows.
    - Add reasoning chain expansion in the chat bubble for local models.

## 3. Acceptance Criteria
- [ ] User can see a list of LM Studio models in the dropdown.
- [ ] Selecting a model initiates a load process visible in the UI.
- [ ] UI updates to "Ready" once the model is fully loaded.
- [ ] Activity Log tab correctly displays the sequence of loading events.
- [ ] System remains stable if LM Studio is offline.
- [ ] Local model conversations maintain context via server-side session management (`response_id`).
- [ ] Thought/Reasoning blocks from local models are visible in the reasoning trace.
