# Phase 40: Sovereign Alpha Command Center - Research

**Researched:** 2026-03-21
**Domain:** SwiftUI, Brutal Editorial Design, 120Hz ProMotion Performance
**Confidence:** HIGH

## Summary

This phase focuses on transitioning the Growin App from its legacy "Liquid Glass" aesthetic to the **Sovereign Ledger** style—a "Brutal Editorial" approach characterized by 0px corners, tonal depth, and radical negative space. The goal is to create a high-authority, financial command center that feels like a bespoke archival ledger rather than a generic AI SaaS dashboard.

**Primary recommendation:** Enforce a strict "0px corner" rule and replace all shadows/gradients with monochromatic tonal layering and 1pt technical borders to establish "Authority through Absence."

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **Aesthetic:** Brutal Editorial (high-fashion meeting archival financial ledgers).
- **Corner Radius:** 0px (no exceptions for Sovereign Foundation primitives).
- **Palette:** Monochromatic depth + Electric Chartreuse (#DFFF00) as the primary "Acid" accent.
- **Performance:** Mandatory 120Hz smooth performance on M4 Pro displays.

### the agent's Discretion
- **Component Specifics:** Designing the technical control set and reasoning console layout.
- **Asymmetry Levels:** Determining the degree of layout "topological betrayal" to maintain usability.
- **Technical Ledger Implementation:** Specific grid-based alignments for agent thoughts.

### Deferred Ideas (OUT OF SCOPE)
- **Multi-User Migration:** (Moved to Post-Weekend Backlog).
- **Options Greeks Agent:** (Moved to Post-Weekend Backlog).
- **Order Book Heatmaps:** (Moved to Post-Weekend Backlog).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SOV-01 | Wave 1: Sovereign Foundation (Core Primitives) | 0px corner research and Tonal Layering patterns. |
| SOV-02 | Wave 2: Alpha Command Dashboard (120Hz) | ProMotion 120Hz optimization and `Canvas` for high-fidelity charts. |
| SOV-03 | Wave 3: Narrative Depth & Strategy Calibration | "Authority through Absence" and "Agent Reasoning Console" financial ledger patterns. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| SwiftUI | 5.x | Native Frontend | macOS native performance, 120Hz support. |
| Accelerate | System | Vector Math | Performance-critical data processing for charts. |
| Metal | System | Rendering | Offloading complex graphics via `.drawingGroup()`. |

### Supporting
| Font | Purpose | When to Use |
|------|---------|-------------|
| **Noto Serif** | Wealth & Authority | Large headers, display text, "Premium" labels. |
| **Space Grotesk** | Technical Data | Numeric values, metrics, strategy labels. |
| **Monaco** | Code/Trace | Intelligence trace, agent thoughts, technical ledgers. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| RoundedRectangle | Rectangle() | Brutalism requires 0px; RoundedRectangle breaks the aesthetic. |
| Shadow Effects | Tonal Layering | Shadows look "soft/AI"; Tonal layers (charcoal/off-white) feel "archival/heavy". |
| Bento Grids | Asymmetric Layouts | Bento is generic; Asymmetry feels bespoke and "High Fashion". |

**Verified Versions:**
- SwiftUI 5.x (macOS 14+) - Stable.
- MLX 0.20.x (Current for Backend communication).

## Architecture Patterns

### Recommended Project Structure
```
Growin/
├── Core/
│   ├── SovereignTheme.swift      # 0px Primitives, Tonal Scale, Technical Borders
│   └── Performance.swift       # 120Hz Render Utilities
├── Views/
│   ├── CommandCenter/
│   │   ├── AlphaLedgerView.swift # The main financial ledger dashboard
│   │   └── AgentReasoning.swift  # Financial ledger-style trace
│   └── Calibration/
│       └── StrategyLabView.swift # Hyperparameter calibration UI
```

### Pattern 1: Tonal Layering (Depth via Value, not Shadow)
**What:** Creating hierarchy using specific grayscale steps instead of shadows.
**When to use:** All card-like containers or sections.
**Example:**
```swift
// Source: docs/STITCH_STRATEGY.md
Rectangle()
    .fill(Color.brutalCharcoal)
    .overlay(
        Rectangle()
            .fill(Color.brutalRecessed)
            .padding(1) // 1pt technical border effect
            .border(Color.white.opacity(0.1), width: 0.5)
    )
```

### Pattern 2: 120Hz ProMotion Optimization
**What:** Maintaining an 8.33ms frame budget for ProMotion displays.
**When to use:** Real-time dashboards with streaming data.
**Optimization:**
- Use `.equatable()` on complex child views to skip unnecessary diffing.
- Use `.drawingGroup()` for static background layers with complex shapes/gradients.
- Use `Canvas` for the "Alpha Chart" to avoid individual view allocation for every data point.

### Anti-Patterns to Avoid
- **Implicit Corner Radius:** Using `.cornerRadius(20)` (The "Liquid Glass" legacy).
- **Bento Overuse:** Standard symmetrical grids that look like Apple's marketing site.
- **Shadow Blur:** Soft shadows (use hard offsets or tonal shifts instead).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Financial Charts | Custom Path Logic | `Swift Charts` or `Canvas` | Swift Charts is optimized for 120Hz; Canvas for huge data. |
| Layout Management | Hardcoded Offsets | SwiftUI `Layout` Protocol | For complex asymmetric editorial layouts. |
| Markdown Parsing | Custom Regex | `ParsedMessage` (Existing) | Already hardened in `ThemeComponents.swift`. |

**Key insight:** SwiftUI's `Canvas` is an immediate-mode rendering context. For the 120Hz Alpha Dashboard, drawing hundreds of data points in a `Canvas` is 10x more efficient than creating individual `Shape` views.

## Runtime State Inventory

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None | Verified: UI change only. |
| Live service config | None | Verified: No backend config changes. |
| OS-registered state | None | Verified: No OS registrations involved. |
| Secrets/env vars | None | Verified: Colors are not secrets. |
| Build artifacts | None | Normal build cycle. |

## Common Pitfalls

### Pitfall 1: Frame Drops at 120Hz
**What goes wrong:** UI stutters during high-speed data updates.
**Why it happens:** Main thread work exceeds 8ms; excessive `GeometryReader` usage.
**How to avoid:** Move all data calculation to ViewModels; use `.equatable()` to isolate updates.
**Warning signs:** Xcode Instruments showing "Hitch" markers.

### Pitfall 2: Aesthetic Drift
**What goes wrong:** The UI starts feeling like "Liquid Glass" again due to accidental rounding or soft shadows.
**Why it happens:** Default SwiftUI behaviors (e.g., `RoundedRectangle`).
**How to avoid:** Create a "Sovereign UI Kit" wrapper that defaults to `Rectangle()` and hard borders.

## Code Examples

### The Sovereign "Technical Ledger" Card
```swift
struct SovereignCard<Content: View>: View {
    let content: Content
    
    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }
    
    var body: some View {
        ZStack {
            // Layer 0: Recessed Base
            Rectangle()
                .fill(Color.brutalRecessed)
            
            // Layer 1: Tonal Depth with Technical Border
            content
                .padding()
                .background(Color.brutalCharcoal)
                .border(Color.white.opacity(0.15), width: 0.5)
                .offset(x: -2, y: -2) // Asymmetric depth
        }
    }
}
```

### High-Authority Technical Typography
```swift
extension View {
    func technicalData() -> some View {
        self.font(.custom("Space Grotesk", size: 14))
            .kerning(0.5)
            .foregroundStyle(Color.brutalOffWhite)
    }
    
    func acidAccent() -> some View {
        self.foregroundStyle(Color.brutalChartreuse)
    }
}
```

## State of the Art

| Old Approach | Current Approach | Impact |
|--------------|------------------|--------|
| Liquid Glass (SF Rounded) | Brutal Editorial (0px Sharp) | Authority & Precision |
| Shadows/Gradients | Tonal Layering / Borders | Archival Financial feel |
| Symmetrical Grids | Radical Asymmetry | High-Fashion / Bespoke |
| 60Hz Target | 120Hz ProMotion | Instantaneous Response |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | XCTest + SwiftUI View Inspector |
| Config file | `pytest.ini` (Backend) / macOS Unit Tests |
| Quick run command | `xcodebuild test -scheme Growin` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command |
|--------|----------|-----------|-------------------|
| SOV-01 | 0px Corner Enforcement | Snapshot Test | `fastlane test` |
| SOV-02 | 120Hz Smoothness | Performance Test | `XCTMeasure` (fps) |
| SOV-03 | Agent Logic Trace | Unit Test | `pytest backend/tests/test_telemetry.py` |

## Sources

### Primary (HIGH confidence)
- `docs/STITCH_STRATEGY.md` - Core aesthetic definition.
- Apple Developer Docs - ProMotion optimization (120Hz).
- Project Codebase (`ThemeComponents.swift`) - Existing brutalist fragments.

### Secondary (MEDIUM confidence)
- "Brutal Editorial" design trends 2024-2026.
- "Authority through Absence" - Minimalist design theory.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Native SwiftUI is well-understood.
- Architecture: HIGH - Tonal layering is a proven editorial pattern.
- Pitfalls: MEDIUM - 120Hz on complex layouts requires careful profiling.

**Research date:** 2026-03-21
**Valid until:** 2026-04-20
