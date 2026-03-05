# Project Decisions

## Phase 24 Decisions

**Date:** 2026-03-04

### Scope
- **Trace Detail Level & Streaming:** Hybrid (Collapsible). We will show a clean top-level status with a chevron to expand the raw streaming trace.

### Approach
- Chose: **Advanced Native SwiftUI** (using pure CoreAnimation/SwiftUI).
- Reason: Guarantees 120Hz performance during token generation without the complexity and GPU draw overhead of a custom Metal shader.

### Constraints
- **Interaction:** Collapse to Indicator (a small chip that can be tapped to re-open the reasoning log for that specific message).
- **Accessibility:** Must play nicely with VoiceOver dynamically without overwhelming the user (`.accessibilityLiveRegion(.polite)`).
