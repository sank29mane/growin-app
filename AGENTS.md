# AGENTS.md

## Project Overview
**Growin App** is a high-performance financial analytics platform for macOS, built with a specific "Neo-Fintech" aesthetic. It consists of a Swift/SwiftUI frontend and a reliable backend.

## Architecture
- **Frontend**: macOS Native App (SwiftUI).
- **Backend**: Python (FastAPI/Agents) with Rust extensions (`growin_core`).
- **Communication**: REST API.

## Workflow Rules for Agents
1.  **Branching**: Always create a new branch for every task. Pattern: `agent/task-description`.
2.  **Pull Requests**: 
    - Title must be descriptive.
    - Description must include a summary of changes and a "Test Plan".
    - You must verify that tests pass before requesting a review.
3.  **Testing**:
    - **Frontend**: Run XCTest suites via `xcodebuild`.
    - **Backend**: Run `pytest` in the `backend/` directory.
    - **Rust**: Run `cargo test` in `backend/growin_core/`.
4.  **Code Style**:
    - **Swift**: Follow strict concurrency (sendable types), functional patterns, and MVVM.
    - **Python**: Strict typing (`mypy`), Pydantic models for data structures.
    - **Rust**: idiomatic Rust, safe memory management.

## Build Instructions
### Backend
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
maturin develop --release --manifest-path growin_core/Cargo.toml
python start_backend.py
```

### Frontend
Assume a standard Xcode project structure. Key scheme is `Growin`.

## Design System (Neo-Fintech)
- **Colors**: Dark mode first. Use high-contrast accents (Neon Green/Blue) for financial data.
- **Typography**: Clean, sans-serif (Inter/SF Pro).
- **Components**: Glassmorphism backgrounds, subtle borders, interactive hover states.

## Do Not
- Do not remove the `AGENTS.md` file.
- Do not commit secrets or API keys.
- Do not force push to `main`.
