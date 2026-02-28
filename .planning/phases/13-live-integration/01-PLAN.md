# Phase 13: Live System Integration - Plan

**Goal:** Transition the Growin App's trading backend from demo/paper trading to live production APIs for Alpaca and Trading 212, ensuring security, reliability, and minimal disruption.

**Status:** Planning

**Date:** 2026-02-26

---

## 1. Research & Requirements Gathering (Completed)

*   **Alpaca Live API:**
    *   Confirm API endpoints (`https://api.alpaca.markets`).
    *   Verify KYC requirements for live accounts.
    *   Investigate market data tiers (Free vs. SIP data subscription).
    *   Understand any differences in client initialization or scope requirements for live API keys.
*   **Trading 212 Live API:**
    *   Confirm API endpoints (`https://live.trading212.com/api/v0`).
    *   Verify Live account minimum balance (€1,000) and KYC for API access.
    *   Confirm 2FA implications for Live API key usage (should be standard API keys).
    *   Note that T212 API does not provide historical data, so Alpaca/yFinance will remain the source for this.
*   **Secrets Management:**
    *   Review current handling of API keys (environment variables, updates via MCP routes).
    *   Confirm the `sentinel.md` vulnerability related to exposing keys in `/mcp/status` has been addressed by `ChatManager` sanitization.
    *   Identify secure methods for managing live API keys in production.

## 2. Implementation - Environment Configuration

*   **Alpaca Environment Switching:**
    *   Modify `backend/data_engine.py` to dynamically set `ALPACA_BASE_URL` based on a new configuration setting (e.g., `ALPACA_ENVIRONMENT` = "live" or "paper").
    *   Ensure the `AlpacaClient` initialization correctly reflects the chosen environment (e.g., `paper="paper" in BASE_URL`).
*   **Trading 212 Environment Switching:**
    *   Ensure `TRADING212_USE_DEMO` environment variable is correctly read and applied in `backend/trading212_mcp_server.py`.
    *   Allow this setting to be configured to `false` for live trading.
*   **API Key Management:**
    *   **Secure Storage:** Advise user to set live API keys via environment variables (`.env` for local dev, system secrets for production). **DO NOT COMMIT LIVE KEYS.**
    *   **MCP Updates:** Leverage the existing `/mcp/trading212/config` endpoint for updating T212 keys if needed. For Alpaca, environment variables must be set externally.

## 3. Testing Strategy - Live Integration

*   **Isolation:** Initially, focus on API calls that do not execute trades (e.g., fetching account info, positions, historical data).
*   **Configuration Verification:** Test that the correct `BASE_URL` and `use_demo` settings are applied based on the desired environment.
*   **Read-Only Operations:** Verify that fetching data from Live Alpaca and T212 APIs works correctly (e.g., `get_account_info`, `get_portfolio_positions`).
*   **Simulated Live Trading (Optional):** If necessary, for order placement, consider:
    *   **User Confirmation:** Require explicit user confirmation for any order placed in a live environment.
    *   **Small Order Sizes:** Use minimal quantities and prices for initial live order tests.
    *   **Monitoring:** Closely monitor order execution and account balances during testing.

## 4. Phased Rollout (To be detailed in subsequent plans)

*   **Phase 13.1: Configuration & Read-Only Tests:**
    *   Set up live API keys securely.
    *   Switch environments to Live for both Alpaca and T212.
    *   Test read-only API calls (account info, positions).
*   **Phase 13.2: Order Placement & Monitoring:**
    *   Implement and test order placement (market, limit) with user confirmation and strict monitoring.
    *   Refine error handling and rate-limiting strategies for live APIs.
*   **Phase 13.3: Full Integration & Monitoring:**
    *   Deploy with live trading enabled.
    *   Implement robust monitoring and alerting for P&L, account status, and API health.

---

**Next Action:** Proceed with implementing environment switching and secure API key handling.
