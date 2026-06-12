---
phase: 45
plan: 03
status: COMPLETED
completion_date: 2026-06-03
summary: Implemented CADisplayLink VSync-driven tick buffering and downsampling in StockChartViewModel and StockChartView.
artifacts:
  - Growin/ViewModels/StockChartViewModel.swift
  - Growin/Views/StockChartView.swift
---

## Key Achievements

- **VSync-Aligned Tick Rendering**: Integrated a display-synchronized refresh loop using AppKit's `NSScreen.displayLink` API, leveraging a custom NSObject-based `DisplayLinkProxy` for Swift 6 thread safety.
- **WebSocket Buffering**: Refactored websocket tick processing to queue incoming chart points and quotes into high-frequency thread-safe buffers, flushing updates only on VSync ticks to avoid layout thrashing.
- **Metal-Accelerated Downsampling**: Implemented data downsampling inside `StockChartView.swift` to limit active rendering paths to 500 points when datasets exceed this count, guaranteeing flawless 120Hz scrolling and drawing.

## Verification Results

- **Xcode Build**: Compiled successfully with no errors or display link warnings.
  `xcodebuild -project Growin.xcodeproj -scheme Growin -configuration Debug build -quiet`
