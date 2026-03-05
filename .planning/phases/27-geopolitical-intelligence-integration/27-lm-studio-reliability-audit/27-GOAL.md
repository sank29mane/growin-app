# Phase 27: LM Studio Reliability & Functional Audit

## Goal
Conduct a comprehensive reliability audit of the `LMStudioClient` to ensure local inference is robust, failsafe, and correctly integrated with the MAS. This phase focuses on model lifecycle management (load/unload), memory guardrails, and adversarial edge-case handling.

## Scope
1. **Model Lifecycle Verification**: Ensure `load_model`, `unload_model`, and `ensure_model_loaded` work correctly with LM Studio v1 API (0.4.x).
2. **Memory Guard Audit**: Validate the "60% RAM Rule" and concurrency semaphore under heavy load.
3. **Error Resilience**: Test "Channel Error" and "Model Crashed" recovery logic (V1 Recovery path).
4. **Stateful Chat Integrity**: Verify `stateful_chat` correctly maintains context via `response_id`.
5. **Edge Case Prompting**: Test "drying out" prompts with empty inputs, excessive tokens, and malformed tool calls.

## Acceptance Criteria
- [ ] Automated test suite `tests/backend/test_lm_studio_audit.py` passes all reliability checks.
- [ ] Model auto-recovery (reload on crash) is empirically verified.
- [ ] Memory guard successfully prevents OS swapping during 4+ parallel agent requests.
- [ ] Stateful chat maintains context across at least 3 turns without manual history management.
- [ ] Malformed tool calls from the model are gracefully handled (PriceValidator/MCP bridge).

## Technical Constraints
- **Environment**: LM Studio 0.4.x Stable (v1 API).
- **Hardware**: Optimized for M4 Pro/Max (60% RAM Rule).
- **Latency**: TTFT for local models should be < 500ms with prefix caching enabled.
