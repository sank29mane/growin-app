# Phase 40: Sovereign Alpha Command Center - Context

## Objective
Implement the high-end **Sovereign Ledger** aesthetic into the final 120Hz Alpha Dashboard, replacing the legacy "Liquid Glass" style with brutal editorial precision and 120Hz ProMotion optimization for the M4 Pro.

## 🏛 Strategic Design Decisions (Locked)

### 1. Asymmetry Threshold: The "Golden Grid"
*   **Decision:** A hybrid of Option 1 and 3 (Rigid Margins + Asymmetric Gutters).
*   **Implementation:** 
    *   Use a 60/40 primary-to-secondary layout split with custom `SwiftUI.Layout` protocols to create "topological betrayal" (asymmetric offsets).
    *   **Luxury through Absence:** Maintain generous "dead space" (minimum 5.5rem padding) between the "Grand Total" balance and the "Master Ledger" data rows.
    *   **Bleed Accents:** The Alpha-Stream chart lines may bleed to the right edge to signify momentum, while functional controls stay flush on the inner grid.

### 2. Information Density: The Master Ledger
*   **Decision:** Option 2 (High-Density Professional Terminal).
*   **Implementation:** 
    *   Display 12-15 asset rows visible in a single 16-inch frame without scrolling.
    *   **Typography:** All numeric data MUST use `Space Grotesk` (Monospaced/Tabular Figures) to prevent horizontal "jitter" during high-frequency updates.
    *   **Visual Signal:** Use "Cell Flashing" (sub-100ms tonal shifts to Mint/Crimson) for price changes, rendered at a locked 120Hz.

### 3. Interaction Feedback: Binary Efficiency
*   **Decision:** Option 1 (Tonal Step) + Option 4 (Inversion).
*   **Implementation:** 
    *   **No Shadows/Borders:** signified only by a hard background shift from `#131313` to `#1C1B1B`.
    *   **Active State:** Critical active elements (e.g., the selected asset) will invert to white background with black text for immediate authority.
    *   **Performance:** Background color shifts are zero-geometry operations, ensuring we stay within the **8.33ms frame budget**.

### 4. Reasoning Console Depth: Layered Trace
*   **Decision:** Option 3 (The Sovereign Journal → Raw Technical Ledger).
*   **Implementation:** 
    *   **Default View:** Natural language "Sovereign Journal" summaries written by the agent, styled as an editorial sidebar.
    *   **Deep Dive:** On-click expansion reveals the raw, timestamped technical traces in a recessed `surface_container_lowest` (#0E0E0E) data well.

## 🛠 Technical Mandates

### Performance Optimization (120Hz)
*   **Render Strategy:** Use `SwiftUI.Canvas` for the Alpha-Stream chart and any high-frequency sparklines. 
*   **Throttling:** Decouple the 100Hz ticker data stream from the 120Hz render loop using a 60Hz throttled `@Observable` state updates to preserve battery and main-thread headroom.
*   **Hardware:** Optimized for **16-inch MacBook Pro (M4 Pro)** with ProMotion enabled.

### Code Style (The "Maestro" Audit)
*   **Primitives:** Use `Rectangle()` with 0px radius exclusively. **NO `RoundedRectangle`**.
*   **Dividers:** Use background tonal shifts (`surface_container_low`) instead of 1px `Divider()` lines.
*   **Typography:** `Noto Serif` (Wealth/Headlines), `Space Grotesk` (Technical Data), `Monaco` (Intelligence Traces).
*   **Color:** Primary `#FFFFFF`, Background `#131313`, Accent Chartreuse `#DFFF00`. **NO PURPLE.**

## 📂 Key Files & Integration Points
*   `Growin/Core/SovereignTheme.swift`: The new design system backbone.
*   `Growin/Core/SovereignPrimitives.swift`: 0px Buttons, Containers, and Inputs.
*   `Growin/Views/CommandCenter/AlphaCommandDashboard.swift`: The main 120Hz 16-inch hub.
*   `Growin/Views/CommandCenter/AgentReasoningView.swift`: The Layered Trace implementation.

---
*Created by Gemini CLI | Phase 40 Context Locked | March 2026*
