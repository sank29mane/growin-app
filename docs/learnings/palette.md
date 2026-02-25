## 2024-10-23 - Custom SwiftUI Buttons Missing Accessibility
**Learning:** This app frequently uses `.buttonStyle(.plain)` with custom view hierarchies (GlassCard, Images, etc.) for buttons. This pattern systematically strips standard accessibility behaviors, leading to many icon-only buttons having no labels.
**Action:** When seeing `.buttonStyle(.plain)` or complex `Button` content, assume accessibility is broken and manually add `.accessibilityLabel`, `.accessibilityHint`, and `.accessibilityAddTraits`.

## 2024-05-22 - Complex List Items Accessibility
**Learning:** SwiftUI Lists with complex cards (like `PositionDeepCard`) often use `.onTapGesture` which fails to expose "button" traits to VoiceOver, making items appear static.
**Action:** Always wrap actionable list items in `Button(action: ...) { ... }` with `.buttonStyle(.plain)` instead of using `.onTapGesture`, and ensure inner content is combined via `.accessibilityElement(children: .combine)` for a clean readout.
