# PLAN: Phase 2 — Financial Precision Refactor

## Objective
Eliminate floating-point precision risks by migrating all currency-related calculations and data structures to `decimal.Decimal`.

## Wave 1: Foundation (Type Definitions)
<task type="auto" effort="medium">
  <name>Update MarketContext Models</name>
  <files>backend/market_context.py</files>
  <action>
    Change types for price, pnl, value, and balance fields from `float` to `Decimal`.
    Ensure Pydantic models correctly handle Decimal serialization.
  </action>
  <verify>uv run pytest backend/tests/test_api_schemas.py</verify>
  <done>Models use Decimal types.</done>
</task>

## Wave 2: Engine Refactor (Logic Migration)
<task type="auto" effort="high">
  <name>Refactor QuantEngine to Decimal</name>
  <files>backend/quant_engine.py</files>
  <action>
    Replace all `float` casts and type hints for currency fields with `Decimal`.
    Use `create_decimal` and `quantize_currency` from `financial_math.py`.
    Update `PortfolioMetrics` and `TechnicalIndicators` TypedDicts.
  </action>
  <verify>export PYTHONPATH=$PYTHONPATH:. && uv run pytest backend/tests/test_financial_precision.py</verify>
  <done>QuantEngine logic is float-free for currency.</done>
</task>

## Wave 3: Integration & Verification
<task type="auto" effort="medium">
  <name>Verify End-to-End Precision</name>
  <files>backend/tests/test_financial_precision.py</files>
  <action>
    Add more aggressive edge cases (very small fractions, large numbers) to precision tests.
    Verify that `QuantAgent` outputs Decimals.
  </action>
  <verify>uv run pytest backend/tests/test_financial_precision.py backend/tests/test_agents.py</verify>
  <done>All tests pass with 100% Decimal accuracy.</done>
</task>
