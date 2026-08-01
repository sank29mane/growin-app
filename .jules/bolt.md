## 2025-02-27 - Portfolio Metrics Single Pass Optimization
**Learning:** Using multiple list comprehensions combined with `zip` to extract data from dictionaries is significantly slower in Python than a single loop that extracts fields directly, despite appearing more "functional".
**Action:** Avoid zipping multiple comprehensions; extract dictionary fields and aggregate values in a single pass when computing portfolio metrics.
