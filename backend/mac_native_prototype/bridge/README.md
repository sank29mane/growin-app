# ANE IPC Bridge (Prototype)

This module provides a minimal, Unix-domain socket based IPC bridge scaffold to route on-device ANE-enabled ML tasks from the SwiftUI frontend to the Python backend. The goal is to enable ultra-low latency communication once the SwiftUI side is wired, while keeping a safe, testable REST fallback path during early iterations.
