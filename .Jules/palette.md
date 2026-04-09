## 2025-02-28 - SwiftUI .buttonStyle(.plain) strips accessibility traits
**Learning:** In SwiftUI, applying `.buttonStyle(.plain)` to a button strips standard VoiceOver attributes, resulting in icon-only buttons having no inherent accessible name or traits.
**Action:** When using `.buttonStyle(.plain)`, manually add `.accessibilityLabel()`, `.accessibilityHint()`, and `.accessibilityAddTraits()` modifiers to ensure the button is recognizable and actionable by assistive technologies.

## 2025-05-18 - SwiftUI `.buttonStyle(.plain)` Accessibility Strip

**Learning:** When applying `.buttonStyle(.plain)` to buttons in SwiftUI (especially complex ones or icon-only ones), it strips out standard VoiceOver traits like `isSelected` or the default accessibility boundaries. The standard text or label might not be read correctly or might be combined improperly by VoiceOver.
**Action:** Always explicitly add `.accessibilityLabel`, `.accessibilityHint`, and `.accessibilityAddTraits` (especially for selected states like `isSelected ? [.isSelected] : []`) when using `.buttonStyle(.plain)`. If the button contains multiple text/image views, consider adding `.accessibilityElement(children: .combine)`.

## 2025-06-05 - SwiftUI `.buttonStyle(.plain)` missing `.isButton` accessibility trait
**Learning:** For icon-only `Button` views that use `.buttonStyle(.plain)` to strip visual styling, standard VoiceOver `isButton` traits are also stripped out. This causes VoiceOver to announce them as generic text or groups instead of interactive buttons.
**Action:** When creating icon-only `Button` elements with `.buttonStyle(.plain)`, explicitly append `.accessibilityAddTraits(.isButton)` to restore proper screen reader announcements as an actionable button, alongside providing an `.accessibilityLabel`.

## 2025-06-12 - Dynamic Accessibility Labels in HITL Action Cards

**Learning:** When creating paired, opposing action buttons (like Approve/Reject) in Human-in-the-Loop (HITL) cards, generic labels are insufficient. If users are navigating through a list of pending actions via screen reader, "Approve" and "Reject" alone lack context.
**Action:** Always include dynamic context from the item being actioned in the `.accessibilityLabel` (e.g., `Approve \(action.action)`) so users can distinguish exactly what they are approving or rejecting, especially when standard text labels are stripped of traits by `.buttonStyle(.plain)`.

## 2026-03-10 - SwiftUI Dynamic Accessibility Labels with `.buttonStyle(.plain)`
**Learning:** For collapsible UI components (like `ChatReasoningTraceView`), buttons using `.buttonStyle(.plain)` require dynamic `.accessibilityLabel`s that reflect the current state (e.g., 'Expand...' vs 'Collapse...') along with `.accessibilityAddTraits(.isButton)` and `.accessibilityHint` to maintain full VoiceOver support.
**Action:** Ensure dynamic state bindings (like `isExpanded`) are integrated into the `.accessibilityLabel` strings when the visual state changes.
## 2026-03-16 - Trade Proposal Accessibility Labels
**Learning:** When using `.buttonStyle(.plain)` in HITL action cards, static labels like 'Approve Trade Proposal' are insufficient for VoiceOver users who need context on *what* they are approving.
**Action:** Use dynamic accessibility labels (e.g., `Approve \(proposal.action) for \(proposal.ticker)`) and explicitly add the `.isButton` trait.
## 2025-03-19 - [Merge Conflict Resolution & Legend A11y]
**Learning:** Resolving multiple Git merge conflicts manually across SwiftUI files is critical before making UX changes. Adding explicit `.accessibilityElement(children: .combine)` to custom composite views (like LegendItem containing shapes and text) significantly improves VoiceOver coherence.
**Action:** Always verify for and resolve Git conflict markers before attempting to implement new accessibility features to prevent compilation errors and ensure valid syntax.
## 2025-03-19 - [FastAPI Exception Handling for Tests]
**Learning:** Returning un-sanitized `str(e)` in FastAPI 500 exceptions breaks security sanitization checks in `test_security_error_handling.py` and causes the test suite to silently abort with `Fatal Python error: Aborted` (due to testclient constraints or security middleware handling exceptions aggressively).
**Action:** Always replace broad exception strings with a generic `detail="Internal Server Error"` when propagating 500 errors to clients in routes.

## 2026-03-24 - Trade Proposal and Custom Button Accessibility Modifiers
**Learning:** For dynamic context buttons like `Approve` or `Reject` inside `TradeProposalCard`, static accessibility labels fail to provide context for VoiceOver users. Additionally, custom shared components like `PremiumButton` that use `.buttonStyle(.plain)` must have `.accessibilityAddTraits(.isButton)` and `.accessibilityLabel` explicitly applied.
**Action:** Use dynamic accessibility labels (e.g., `Approve \(proposal.action) for \(proposal.ticker)`) for action cards, and ensure shared custom buttons re-add the button traits when using `.plain` style.

## 2026-03-30 - Added ARIA label to "Retry" button in ChatView
**Learning:** Found an existing custom SwiftUI component ("Retry" button for failed messages) that lacked VoiceOver hints or plain button styling which standardizes presentation across the repo.
**Action:** When finding missing `.accessibilityLabel` tags on custom components with `.background(Color)` or `.cornerRadius`, consistently apply `.buttonStyle(.plain)` and `.accessibilityAddTraits(.isButton)` to integrate properly with standard VoiceOver and design parameters.

## 2026-03-31 - SwiftUI Dynamic Accessibility Labels for Metric Groups
**Learning:** For SwiftUI view components that group text elements to represent a metric (like `FinancialMetricView` grouping title, value, and change percent), VoiceOver naturally reads them as separate, disconnected elements. Adding `.accessibilityElement(children: .combine)` helps, but it still might read them in a clunky manner depending on how the views are nested.
**Action:** When making custom composite metric views accessible, combine the children (`.accessibilityElement(children: .combine)`) and provide a single, explicitly computed `.accessibilityLabel` string that naturally reads the metric as a cohesive sentence (e.g., 'TOTAL CAPITAL: £100.00, Up 5.0%').
