# Palette's UX Journal

This journal tracks critical UX and accessibility learnings for the Growin project.

## 2024-10-18 - Custom Selection Controls & Accessibility
**Learning:** Custom SwiftUI selection controls (like `AccountPicker`) don't automatically communicate their "selected" state to screen readers, unlike native `Picker`.
**Action:** Always manually apply `.accessibilityAddTraits([.isSelected])` to the active element in custom segmented controls or tab bars.

## 2026-01-28 - Accessibility Traits on Custom Controls
**Learning:** Custom selection controls like "chips" or "pills" implemented with `Button` do not convey their selected state to VoiceOver users by default. They act like stateless buttons.
**Action:** Always add `.accessibilityAddTraits(isSelected ? [.isSelected] : [])` to custom radio-like buttons to ensure screen readers announce "Selected" state.
