# SPEC.md - Phase 12: Stability Hardening & Crash Resolution

Status: DRAFT
Phase: 12

## Objective
Identify and resolve the root cause of the persistent crash in the Dashboard `Charts` section. Harden the application against data-induced runtime errors and concurrency issues to ensure 100% functional stability.

## Requirements

### 1. Chart Data Hardening (STAB-01)
- **Zero/Negative Filtering**: Implement strict filtering in `DashboardViewModel` to ensure `allocationData` contains only positive values before reaching the UI. `Charts` framework crashes when encountering invalid angles in `SectorMark`.
- **Identity Verification**: Ensure all collections passed to `Chart` or `ForEach` have guaranteed unique and stable IDs.
- **Null Safety**: Add defensive `if let` or `guard` checks around all data-driven chart components.

### 2. Concurrency & Thread Safety (STAB-02)
- **Actor Isolation Enforcement**: Audit all `Task` blocks in ViewModels to ensure they explicitly return to the `@MainActor` before updating `@Observable` properties.
- **Data Race Prevention**: Ensure `PortfolioSnapshot` is treated as an immutable value type throughout the processing pipeline.
- **Sync Task Management**: Improve the robustness of `startLiveSync` / `stopLiveSync` to prevent overlapping or orphaned sync tasks.

### 3. Error Recovery & Boundary Protection (STAB-03)
- **Graceful Degradation**: Implement "Safety Wrappers" around complex UI components (like Charts) that catch or prevent rendering errors.
- **User-Facing Feedback**: Enhance the `ErrorCard` to provide specific, actionable recovery steps when data sync fails.
- **Snapshot Testing**: Implement snapshot tests for the Dashboard with edge-case data (empty positions, all-zero balances, extremely large values).

### 4. macOS Native Pure-Play Verification (STAB-04)
- **Framework Audit**: Remove any remaining traces of `UIKit` or iOS-specific logic (e.g., `UIViewRepresentable` vs `NSViewRepresentable`).
- **Modifier Cleanup**: Eliminate mobile-specific view modifiers that are ignored on macOS (e.g., `navigationBarTitleDisplayMode`).
- **Platform Isolation**: Ensure all platform-specific code is properly isolated using `#if os(macOS)` to prevent accidental cross-pollination.
- **API Standardisation**: Verify usage of macOS-native APIs for system interactions (notifications, file system, window management).

## Acceptance Criteria
- [ ] Dashboard section is stable and does not crash when navigated to or refreshed.
- [ ] Charts render correctly with real-world, empty, and edge-case portfolio data.
- [ ] No threading warnings or data races detected by Xcode Sanitizers.
- [ ] Application remains responsive (120Hz) during active background synchronization.
