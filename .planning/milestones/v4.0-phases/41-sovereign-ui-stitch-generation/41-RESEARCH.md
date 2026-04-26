# Phase 41: Sovereign UI - Stitch Generation & UX Refinement - Research

**Researched:** 2026-03-22
**Domain:** Stitch MCP, SwiftUI, Brutal Editorial Design, Trading 212 UI Patterns
**Confidence:** HIGH

## Summary

This phase focuses on leveraging **Stitch MCP tools** to generate and refine UI components that adhere to the **Sovereign Ledger** (Brutal Editorial) aesthetic established in Phase 40. The goal is to move beyond static dashboarding into a high-density, professional trading environment inspired by the efficiency of **Trading 212** while maintaining the authoritative, minimalist character of the Growin App's new design system.

**Primary recommendation:** Use a two-step "DNA Extraction" workflow with Stitch—first extracting design context from an existing Sovereign primitive, then generating new screens using highly specific "Location + Action + Detail" prompts to ensure 0px corner enforcement and tonal layering consistency.

<user_constraints>
## User Constraints (from CONTEXT.md)

*Note: No 41-CONTEXT.md found. Carrying forward locked decisions from Phase 40.*

### Locked Decisions
- **Aesthetic:** Sovereign Ledger / Brutal Editorial (high-fashion meeting archival financial ledgers).
- **Corner Radius:** Strict 0px (no exceptions for Sovereign Foundation primitives).
- **Palette:** Monochromatic depth (#131313, #1C1B1B) + Electric Chartreuse (#DFFF00) accent.
- **Typography:** Noto Serif (Wealth/Authority), Space Grotesk (Technical Data), Monaco (Intelligence Traces).
- **Performance:** Mandatory 120Hz smooth performance on M4 Pro displays.

### the agent's Discretion
- **Component Specifics:** Designing the technical control set and reasoning console layout.
- **Asymmetry Levels:** Determining the degree of layout "topological betrayal" to maintain usability.
- **Trading Layouts:** Specific arrangement of order books, watchlists, and P&L widgets.

### Deferred Ideas (OUT OF SCOPE)
- **Multi-User Migration:** (Moved to Post-Weekend Backlog).
- **Options Greeks Agent:** (Moved to Post-Weekend Backlog).
- **Order Book Heatmaps:** (Moved to Post-Weekend Backlog).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SOV-UI-01 | Stitch-Driven Screen Generation | Best practices for `generate_screen_from_text` and `extract_design_context`. |
| SOV-UI-02 | Trading 212 Pattern Integration | Research on T212 high-density portfolio and watchlist layouts. |
| SOV-UI-03 | Sovereign Ledger UX Refinement | "Authority through Absence" and "Topological Betrayal" layout patterns. |
| SOV-UI-04 | Professional Trading Efficiency | Above-the-fold execution and modular grid research. |
</phase_requirements>

## Standard Stack

### Stitch MCP Tools
| Tool | Purpose | Best Practice |
|------|---------|---------------|
| `extract_design_context` | Capture "Design DNA" | Run this on `SovereignTheme.swift` or an existing UI screen before generating new ones. |
| `generate_screen_from_text` | Create NEW UI components | Use "Location + Action + Detail" prompting (e.g., "In the Portfolio view, add a 0px technical ledger..."). |
| `fetch_screen_code` | Download implementation specs | Use to guide SwiftUI implementation in `Growin/Views/`. |
| `create_project` | Workspace management | Ensure all work is in `Growin UI` (ID: `8180498255360292611`). |

### Trading UI Framework
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| SwiftUI | 5.x | Frontend | 120Hz ProMotion support and native macOS performance. |
| Swift Charts | 1.x | Financial Viz | Optimized for high-frequency data streaming. |

**Installation:**
```bash
# Verify Stitch MCP configuration (hypothetical command)
mcp-stitch status
```

## Architecture Patterns

### Recommended Project Structure
```
Growin/
├── Core/
│   ├── SovereignTheme.swift      # 0px Primitives, Tonal Scale
│   └── SovereignPrimitives.swift # Refined Button, Container, Input stubs
├── Views/
│   ├── Trading/
│   │   ├── MasterLedgerView.swift # High-density portfolio (T212 inspired)
│   │   ├── WatchlistView.swift    # Compact ticker list with sparklines
│   │   └── ExecutionPanel.swift   # Slide-to-trade / Order entry
```

### Pattern 1: Topological Betrayal (Radical Asymmetry)
**What:** Challenging standard sidebar/top-nav layouts with asymmetric offsets.
**When to use:** Main command dashboards to create a "bespoke" editorial feel.
**Detail:** Use extreme margins (0px for technical blocks, 80px+ for editorial titles).

### Pattern 2: High-Density Tonal Layering
**What:** Using grayscale shifts (#131313 -> #1C1B1B) instead of borders/shadows to separate data.
**When to use:** All portfolio and watchlist rows to maintain the "archival ledger" look.

### Anti-Patterns to Avoid
- **Aesthetic Drift:** Accidental use of `RoundedRectangle` or soft shadows.
- **Low-Density "Bento":** Generic symmetrical grids that waste space on a 16-inch M4 Pro display.
- **Visual Noise:** Using too many colors (stick to Monochrome + Chartreuse).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| UI Design Sync | Manual Copy-Paste | `stitch_integration_tool.py` | Automated mapping of tokens to SwiftUI code. |
| Numeric Formatting | Custom String Logic | `tabular-nums` / Space Grotesk | Prevents horizontal "shimmer" in high-frequency tickers. |
| Order Confirmation | Standard Button | Slide-to-Confirm | T212/Professional standard for preventing execution errors. |

## Common Pitfalls

### Pitfall 1: Corner Radius Leakage
**What goes wrong:** Library components (e.g., `TextField`, `Menu`) often have implicit rounding.
**How to avoid:** Explicitly overlay with `Rectangle().stroke()` and use `.buttonStyle(.plain)` to strip default styling.

### Pitfall 2: Density vs. Legibility
**What goes wrong:** Cramming too much data makes the UI unreadable.
**How to avoid:** Use **Zebra Striping** with subtle 1px dotted underlines and maintain vertical rhythm using "Lining Figures."

## Code Examples

### Stitch Prompting: Location + Action + Detail
```markdown
Location: On the "Master Ledger" portfolio screen, 
Action: Replace the standard asset rows with a 0px high-density technical list,
Detail: Use "Space Grotesk" for numbers and "Electric Chartreuse" for 24h change values > 2%. 
        Ensure a 1px solid border between headers and data.
```

### The T212-Inspired Compact Watchlist Row
```swift
struct WatchlistRow: View {
    let ticker: String
    let price: Double
    let change: Double
    
    var body: some View {
        HStack(spacing: 0) {
            // Ticker (Left-aligned)
            Text(ticker)
                .font(.custom("Space Grotesk Bold", size: 14))
                .frame(width: 60, alignment: .leading)
            
            // Sparkline (Center)
            SovereignSparkline(data: mockData)
                .frame(width: 80, height: 20)
                .padding(.horizontal)
            
            Spacer()
            
            // Price & Change (Right-aligned, Tabular Numbers)
            VStack(alignment: .trailing, spacing: 2) {
                Text(price.formatted(.currency(code: "GBP")))
                    .font(.custom("Space Grotesk", size: 14))
                    .monospacedDigit()
                
                Text(change > 0 ? "▲ \(change)%" : "▼ \(abs(change))%")
                    .font(.custom("Space Grotesk", size: 10))
                    .foregroundStyle(change > 0 ? Color.brutalChartreuse : Color.red)
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 12)
        .background(Color.brutalRecessed)
        .border(Color.white.opacity(0.05), width: 0.5) // Subtle technical border
    }
}
```

## State of the Art

| Old Approach | Current Approach | Impact |
|--------------|------------------|--------|
| Liquid Glass (Soft/Transparent) | Sovereign Ledger (Sharp/Opaque) | Authority & Precision |
| Mobile-First (Large Taps) | M4 Pro-First (High Density) | Professional Terminal Feel |
| Manual Design Implementation | Stitch MCP Generation | Workflow Acceleration |
| Generic Minus Sign (-) | Unicode Minus Sign (−) | Typographic Excellence |

## Open Questions

1. **Stitch Tool Availability:** Are the `mcp__stitch` tools pre-authenticated in the environment?
   - *Recommendation:* If not, use `stitch_integration_tool.py` as a bridge for token-based updates.
2. **Chart Performance at 120Hz:** Does `Swift Charts` maintain 8ms frame budget with 50+ simultaneous sparklines?
   - *Recommendation:* Fall back to `SwiftUI.Canvas` for the main dashboard if hitches occur.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | XCTest + ViewInspector |
| Config file | `pytest.ini` (Backend) |
| Quick run command | `xcodebuild test -scheme Growin` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command |
|--------|----------|-----------|-------------------|
| SOV-UI-01 | 0px Corner Compliance | Snapshot | `fastlane snapshot` |
| SOV-UI-02 | T212 Density Verification | UI Test | Check row count in viewport |
| SOV-UI-03 | Tabular Number Alignment | Layout Test | Verify x-offset of numeric characters |

## Sources

### Primary (HIGH confidence)
- `docs/STITCH_STRATEGY.md` - Core aesthetic definition.
- Trading 212 App Analysis - Recent design reversion to "Density-First."
- "Brutal Editorial" design theory - Radical asymmetry and negative space.

### Secondary (MEDIUM confidence)
- Bloomberg Terminal UX patterns - High-density information hierarchy.
- Stitch MCP Documentation - Toolset and prompting workflow.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - SwiftUI and T212 patterns are well-documented.
- Architecture: HIGH - Sovereign Ledger primitives are already in the codebase.
- Pitfalls: MEDIUM - Stitch-to-SwiftUI automation may require manual tuning.

**Research date:** 2026-03-22
**Valid until:** 2026-04-21
