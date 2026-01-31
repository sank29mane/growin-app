# Palette's UX Journal

This journal tracks critical UX and accessibility learnings for the Growin project.

## 2024-10-18 - Custom Selection Controls & Accessibility
**Learning:** Custom SwiftUI selection controls (like `AccountPicker`) don't automatically communicate their "selected" state to screen readers, unlike native `Picker`.
**Action:** Always manually apply `.accessibilityAddTraits([.isSelected])` to the active element in custom segmented controls or tab bars.

## 2026-01-30 - Enhanced VoiceOver Labels & Contextual Hints
**Learning:** For interactive AI components (like Suggestion Chips), generic labels aren't enough. Users need to know *what* the action will do (e.g., "Ask about Portfolio Overview" instead of just "Portfolio Overview").
**Action:** Implemented `accessibilityLabel` with dynamic prompts and `accessibilityHint` on `AccountPicker`. Also integrated the PR #31/28/35 changes which added message time bubbles for better visual hierarchy and chronological clarity in chat.
