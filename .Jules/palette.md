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
