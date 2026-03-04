# SPEC.md - Phase 10: Frontend Implementation: Stitch Integration & Dynamic UI Generation

Status: DRAFT
Phase: 10

## Objective
Implement the new Stitch-generated UI designs (including the dashboard, Calm Bearish state, AI Recovery Strategy, Expanded Reasoning Trace, Challenge Logic, and Revised Strategy screens) as native SwiftUI views, ensuring they are dynamically integrated, maintainable, performant, and continuously updatable.

## Requirements

### 1. UI Component Definition & Integration (FE-05)
-   Implement all Stitch-generated UI components (e.g., Liquid Glass cards, Bento grid modules, specialized buttons) as reusable SwiftUI views within the `Palette` component library.
-   **SOTA Integration**: Implement **Confidence Visualization Patterns (CVP)** using distinct visual states (solid/dashed borders, color coding) to reflect AI certainty.
-   **SOTA Integration**: Create **Reasoning Chips** and hierarchical **Logic Tree** components for progressive disclosure of AI reasoning traces.
-   Ensure components adhere to the "Liquid Glass" design language, "Calm UI" principles, and "Bento Grid" modularity, maintaining functional data density.

### 2. Data Model Mapping (FE-06)
-   Map the data requirements of the new UI components to existing (or new) backend data models and schemas (e.g., for portfolio data, AI reasoning trace, strategy details).
-   **SOTA Integration**: Update models to support granular `ReasoningStep` objects for streaming and `ConfidenceScore` metrics.
-   Define clear data flow between SwiftUI views, ViewModels, and backend responses.

### 3. API Endpoint Outline (FE-07)
-   Identify and outline any new or modified API endpoints required to support dynamic data fetching for the new UI components.
-   **SOTA Integration**: Implement the **AG-UI Streaming Protocol** using Server-Sent Events (SSE) to stream real-time agent workflow events (e.g., "Analysing Sentiment..." → "Backtesting Strategy...").
-   **SOTA Integration**: Utilize **Change Data Capture (CDC)** principles for efficient, delta-based data synchronization between backend and frontend.

### 4. State Management Specification (FE-08)
-   Specify the state management strategy for all new UI components, particularly for dynamic elements, interactive AI features (Challenge Logic), and screens with complex user interactions.
-   **SOTA Integration**: Implement **Optimistic UI patterns** for trade execution and strategy adjustments, with graceful rollback animations and plain-English error handling.
-   Utilize `Bindable` ViewModels, `@State`, `@Binding`, and `@Environment` as per SwiftUI best practices.

### 5. User Flow Detailing (FE-09)
-   Detail the user flows for all newly designed screens and interactive elements, including:
    -   Navigation to dashboard variants (main, bearish states).
    -   Interaction with AI Recovery Strategy panel.
    -   Expanding and navigating the Reasoning Trace with **Explain-Back Loops**.
    -   Interacting with the "Challenge Logic" interface.
    -   Processing and acting on the "Revised Strategy Outcome."

### 6. Testing Strategy Establishment (FE-10)
-   Establish a comprehensive testing strategy for the new frontend components.

### 7. Performance Metrics & Monitoring (FE-11)
-   Define specific performance metrics for new UI components (e.g., frame rates, render times) to ensure they meet the 120Hz smoothness target.
-   **SOTA Integration**: Leverage **Metal/WebGPU** for high-density data visualization (e.g., rendering thousands of trade points in the strategy simulator) to maintain 120FPS fluidity.
-   Integrate performance monitoring tools (e.g., Xcode Instruments) into the development workflow for continuous validation.

### 8. Continuous UI Integration Mechanism (FE-12)
-   Establish a scalable mechanism for continuously integrating future Stitch UI designs and updates into the frontend.

### 9. R-Stitch Architecture Integration (FE-13)
-   **SOTA Integration**: Implement the **R-Stitch** (Dynamic Trajectory Stitching) framework on the backend to dynamically delegate reasoning between SLMs and LLMs, reducing latency for real-time UI updates.

## Acceptance Criteria
-   [ ] All Stitch-generated screens are faithfully implemented as highly performant, native SwiftUI views.
-   [ ] UI components are reusable, modular, and strictly adhere to the defined design system ("Liquid Glass," "Bento Grid," "Calm UI").
-   [ ] Data models are efficiently mapped, and API interactions are robust and performant, supporting dynamic UI.
-   [ ] All new user flows (e.g., AI collaboration, strategy refinement) are intuitive, responsive, and provide clear feedback.
-   [ ] Comprehensive unit, integration, and UI test coverage is achieved for new frontend logic and components.
-   [ ] Performance monitoring confirms consistent 120Hz frame rates and optimal resource utilization for key UI interactions.
-   [ ] A well-defined, documented process exists for integrating subsequent Stitch UI design updates with high efficiency.
