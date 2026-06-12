# Phase 45: Sovereign UX & macOS 2026 Redesign - Research

**Researched:** 2026-06-03
**Domain:** macOS Native SwiftUI & 120Hz ProMotion Engine
**Confidence:** HIGH

## Summary
To transform Growin into a SOTA macOS desktop experience, we are executing a complete visual and performance overhaul:
1. **0px Ledger DNA + Tahoe Liquid Glass**: Replacing the rounded premium cards with sharp, brutalist 0px borders layered with macOS native translucent materials (`.containerBackground(.thinMaterial, for: .window)`).
2. **Stage Manager 2.0 Window Sets**: Defining discrete Window scenes in `GrowinApp.swift` for trading views (Main, Charts, Console) and providing a quick actions workflow to launch them as a side-by-side trading workspace.
3. **120Hz ProMotion & <16ms Price Sync**: Utilizing macOS 14+ `CADisplayLink` to drive the charts and price ticking at screen refresh rates without thread blocking, ensuring rendering takes <8ms to prevent dropped frames.

## 🏛️ UI/UX Design System Spec
* **0px Borders**: Strict rule of `0` corner radius on all card views and containers.
* **Liquid Glass**: Use SwiftUI's native materials overlayed with fine border strokes:
  ```swift
  .background(.thinMaterial)
  .border(Color.glassBorder, width: 0.5)
  ```
* **Typography**: Spaces Grotesk (Sans) and Space Mono (Monospaced) with strict uppercase alignments for metrics.

## 💻 Technical Details

### 1. Stage Manager 2.0 Integration (Window Groups)
SwiftUI `Environment(\.openWindow)` is the key API. In `GrowinApp.swift`, we register secondary window sets:
```swift
WindowGroup(id: "live-charts") {
    StockChartView()
}
WindowGroup(id: "agent-console") {
    IntelligentConsoleView()
}
```
In the main interface, a unified button triggers:
```swift
openWindow(id: "live-charts")
openWindow(id: "agent-console")
```

### 2. DisplayLink-Aligned Render Loop (CADisplayLink)
Using `CADisplayLink` ensures ProMotion-aware execution:
```swift
class RenderLoop: ObservableObject {
    private var displayLink: CADisplayLink?
    
    func start() {
        displayLink = CADisplayLink(target: self, selector: #selector(tick))
        displayLink?.preferredFrameRateRange = CAFrameRateRange(minimum: 60, maximum: 120, preferred: 120)
        displayLink?.add(to: .main, forMode: .common)
    }
    
    @objc private func tick() {
        // Send frame update notification or publish timestamp
    }
}
```

## 🚫 Avoid
* Do not use `CVDisplayLink` which requires complex C callbacks and lacks native ProMotion frame-rate constraints.
* Avoid heavy calculations on the main thread during high-frequency display updates.
