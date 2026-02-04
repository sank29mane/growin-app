## 2024-05-22 - Accessibility Patterns
**Learning:** Found multiple instances of icon-only buttons without accessibility labels in toolbar and search contexts.
**Action:** Systematically check all Image(systemName: ...) inside Button views for accompanying accessibility modifiers.

## 2026-01-14 - Consolidating Complex Data Cards
**Learning:** Complex UI cards with multiple data points (like `PositionDeepCard` with ticker, name, price, pnl) create a noisy and fragmented experience for screen reader users when each element is read separately.
**Action:** Use `.accessibilityElement(children: .combine)` and a custom `.accessibilityLabel` to summarize the card's content into a single, coherent sentence.
