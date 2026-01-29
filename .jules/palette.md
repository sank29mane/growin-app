# Palette's Journal

This journal records critical UX and accessibility learnings from the Growin project.

## 2024-05-22 - Initial Setup
**Learning:** Accessibility state exposure is critical for custom selection controls.
**Action:** Always verify that custom segmented controls or pickers use `.accessibilityAddTraits([.isSelected])` to communicate state to screen readers.
