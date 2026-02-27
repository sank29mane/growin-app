# Palette's UX/Accessibility Journal

## 2026-02-27 - Accessibility traits missing on plain button styles
**Learning:** Found multiple instances where interactive UI elements in SwiftUI, specifically buttons (e.g. `ShareLink`, close notification button) combined with `.buttonStyle(.plain)`, act as icon-only actions but are missing essential VoiceOver attributes.
**Action:** Always add an explicit `.accessibilityLabel` when using icon-only interactive elements and custom `.buttonStyle(.plain)` so screen readers know what they do.
