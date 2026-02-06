## 2024-10-23 - Custom SwiftUI Buttons Missing Accessibility
**Learning:** This app frequently uses `.buttonStyle(.plain)` with custom view hierarchies (GlassCard, Images, etc.) for buttons. This pattern systematically strips standard accessibility behaviors, leading to many icon-only buttons having no labels.
**Action:** When seeing `.buttonStyle(.plain)` or complex `Button` content, assume accessibility is broken and manually add `.accessibilityLabel`, `.accessibilityHint`, and `.accessibilityAddTraits`.
