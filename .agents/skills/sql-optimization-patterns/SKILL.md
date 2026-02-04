---
name: sql-optimization-patterns
description: Patterns for transforming slow database queries into fast operations.
---

# SQL Optimization Patterns

This skill focuses on optimizing SQL queries and database performance, specifically for financial time-series and analytics.

## Core Instructions

1. **Indexing Strategy**: Always index columns used in JOINs and WHERE clauses. Use composite indexes for multi-column filters.
2. **Query Plan Analysis**: Use `EXPLAIN ANALYZE` to identify bottlenecks (indices scans, sequential scans).
3. **Partitioning**: For large time-series data (like historical price data), use table partitioning by timestamp.
4. **Batch Operations**: Use batch inserts and updates to minimize transaction overhead.
5. **OLAP Optimization**: When using DuckDB, prioritize columnar storage formats (Parquet) and vectorized operations.

## Example: Time-Series Indexing
```sql
CREATE INDEX idx_price_history_ticker_timestamp 
ON price_history (ticker, timestamp DESC);
```
