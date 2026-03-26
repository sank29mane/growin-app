# Stitch UI/UX Strategy: Growin App

This document outlines the strategic approach for exploiting Stitch's capabilities to build a production-grade, high-character UI for the Growin App.

## 🎨 Creative North Star: The Sovereign Ledger
We are moving away from the generic "AI SaaS" aesthetic (Inter font, purple gradients, bento-box grids). Our direction is **Brutal Editorial**:
- **Aesthetic:** High-fashion editorial layouts meet archival financial ledgers.
- **Authority through Absence:** Luxury through negative space, sharp 0px corners, and monochromatic depth.
- **Tonal Prominence:** Using tonal layers (charcoal, off-white) instead of standard shadows or borders.

## 🛠️ Key Prompting Principles
Based on the Stitch Prompt Guide and internal Design System:
1.  **Incremental Refinement:** Focus on one screen or major component change per prompt.
2.  **Targeted Actions:** Use "Location + Action + Detail" (e.g., "On the Treasury screen [Location], change the yield percentage [Action] to vibrant electric chartreuse [Detail]").
3.  **Typography over Decoration:** Let `NOTO_SERIF` (for headers/wealth) and `SPACE_GROTESK` (for technical data) do the heavy lifting.
4.  **No-Divider Rule:** Separation via whitespace and tonal shifts, not 1px borders.

## 🚀 Integration & Workflow
- **Project Scope:** All new UI work happens within the `Growin UI` project (ID: `8180498255360292611`).
- **Antigravity Tagging:** UI-related tasks in the GSD roadmap will be explicitly tagged for implementation via this specialized design workflow.
- **Stitch-to-Code:** We will use `mcp_stitch_get_screen` to extract design specs and `designMd` to guide the actual frontend implementation in React/Vue.

## 📈 Roadmap for Growin UI
1.  **Dashboard (Conceptual):** Sovereign Ledger / Brutal Editorial style (COMPLETE).
2.  **Treasury View:** High-yield stream management with technical drill-downs.
3.  **Asset Detail:** Deep-dive into individual investments with high-character typography.
4.  **Mobile Adaptive:** Radical asymmetry adapted for vertical space.

---
*Created by GSD-assisted Gemini | Last Updated: March 2026*
