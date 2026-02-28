# Phase 14: Dynamic LM Studio Model Management - Plan

**Goal:** Provide a seamless UI for listing, selecting, and loading LM Studio models with real-time feedback in the Growin App.

## 1. Backend Enhancements

### LM Studio Client
- Update `backend/lm_studio_client.py` to ensure `load_model` and `unload_model` are robust.
- Add a background task or polling mechanism to check if a model has finished loading.

### API Endpoints
- **GET `/api/models/lmstudio`**: (Already exists) Fetch available models from LM Studio.
- **POST `/api/models/lmstudio/load`**: Trigger loading of a specific model.
    - Input: `{"model_id": "string"}`
- **GET `/api/models/lmstudio/status`**: Return detailed status of LM Studio (Online/Offline, Loaded Model, Memory Usage).

### Status Tracking
- Integrate with `StatusManager` to update the `lmstudio` status (loading, ready, error).
- Emit telemetry events during loading to inform the frontend in real-time.

## 2. Frontend (SwiftUI) Enhancements

### Preferences UI (`SettingsView.swift`)
- Update the "Decision Engine" picker to trigger a load request when a specific LM Studio model is selected.
- Show a progress indicator (e.g., `ProgressView`) next to the model name if it's currently loading.
- Implement an `LMStudioManager` observable class to handle the state and polling for model status.

### Activity Log
- Ensure that the `Activity Log` tab in `SettingsOverlay` shows model lifecycle events (e.g., "LM Studio: Loading Llama-3-8B...", "LM Studio: Model Ready").

### Chat UI
- Reflect the current model status in the chat view if the model is being swapped or is currently unavailable.

## 3. Implementation Steps

1.  **Backend Routes**: Implement `/api/models/lmstudio/load` and `/api/models/lmstudio/status` in `agent_routes.py`.
2.  **Telemetry**: Add `model_loading_started` and `model_loading_complete` telemetry subjects.
3.  **SwiftUI Logic**: Create `LMStudioViewModel` to manage the dynamic model list and loading state.
4.  **UI Feedback**: Update `SettingsView` to use the new view model and show live status indicators.
5.  **Integration Test**: Verify end-to-end flow: Select model -> Backend triggers load -> Status updates -> UI shows "Loaded".

## 4. Verification

- [ ] Successful listing of models from a running LM Studio instance.
- [ ] Successful trigger of `POST /api/models/lmstudio/load`.
- [ ] UI shows "Loading..." during the process.
- [ ] UI shows "Ready" once loaded.
- [ ] Activity log records the events correctly.
