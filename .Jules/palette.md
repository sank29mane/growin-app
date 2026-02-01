## 2024-05-22 - Accessibility Patterns
**Learning:** Found multiple instances of icon-only buttons without accessibility labels in toolbar and search contexts.
**Action:** Systematically check all Image(systemName: ...) inside Button views for accompanying accessibility modifiers.

## 2024-05-23 - Complex List Item Accessibility
**Learning:** Complex data cards (like stock positions) in lists create noisy VoiceOver experiences when elements are read individually. Grouping them with `.accessibilityElement(children: .combine)` and a consolidated label significantly improves navigation speed.
**Action:** Audit other list views (like transaction history) for similar complex cards and apply grouping.
