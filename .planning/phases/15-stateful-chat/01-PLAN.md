# PHASE 15: SOTA REFINEMENT & UI STABILITY

## Objective
Elevate the Growin macOS app to 2026 SOTA standards by implementing "Calm UI" principles, progressive disclosure for reasoning, and robust stateful session management.

## 1. UI/UX: The "Calm & Intelligent" Interface
- **Progressive Disclosure**: Move all verbose reasoning (CoT) into a collapsible "Intelligence Trace" component.
- **Micro-Feedback**: Add subtle visual pulses (haptic-lite) during model transitions and trade analysis.
- **Liquid Glass Alignment**: Ensure all chat bubbles and cards utilize frosted background materials with WCAG-safe contrast.
- **Flicker-Free Loading**: Implement an "Optimistic Switcher" that maintains the previous UI state until the new model is 100% ready.

## 2. Backend: SOTA Stateful Session Management
- **Checkpointing**: Use LM Studio's `response_id` as a state checkpoint in the `messages` table.
- **Aggressive Cleaning**: Refine `DecisionAgent._clean_response` to strip all meta-instructions, leaving only the "Strategic Synthesis".
- **Dynamic Context Eviction**: Implement a logic to discard unneeded historical turns once a new `response_id` is established.

## 3. Implementation Tasks

### Wave 1: UI Hardening (Frontend)
- [x] Update `LMStudioViewModel` with 15s Flicker Shield.
- [ ] Implement `IntelligenceTraceView` in `RichMessageComponents.swift`.
- [ ] Add Active Status "Green Light" with glow effect to the Preferences UI.

### Wave 2: Stateful Logic (Backend)
- [x] Refactor `LMStudioClient` for Native V1 support.
- [x] Update `ChatManager` schema for `lm_studio_response_id`.
- [x] Wire `DecisionAgent` to pass and store session IDs.

### Wave 3: Quality of Life & SOTA Verification
- [ ] Aggressive response cleaning (strip thinking artifacts).
- [ ] Verification of stateful continuity (Amnesia check).
- [ ] UI build and crash audit on M4 hardware.

## Execution Instruction
Proceed with Wave 1 (Frontend Refinements) and Wave 3 (Response Cleaning).
Refer to `Growin Research` notebook for Liquid Glass and Calm UI patterns.
