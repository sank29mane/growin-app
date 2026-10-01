# Phase 33 Research: High-Fidelity Forecasting Optimization

## 1. Multivariate TTM-R2 Implementation
IBM Granite TTM-R2 supports multivariate forecasting primarily through a **Channel-Independent** approach, where multiple target columns can be passed to the `TimeSeriesForecastingPipeline`.

### Key Findings:
- **Pipeline Integration**: The `tsfm_public.toolkit.time_series_forecasting_pipeline.TimeSeriesForecastingPipeline` accepts a `target_columns` list.
- **Covariates**: We can inject technical indicators (RSI, ATR, Volatility) as additional target columns. TTM-R2 will process them in parallel to identify patterns.
- **Model Revision**: The `512-96-r2` revision is optimal for our 512-bar context window.

### Implementation Strategy:
1. Update `forecast_bridge.py` to calculate RSI and ATR using `pandas` before pipeline execution.
2. Pass `target_columns=["close", "rsi", "atr"]` to the pipeline.
3. Extract only the "close" forecast for the final output, while benefiting from the multi-channel context.

---

## 2. Recency-Weighted Scaling (EWMA)
Standard Z-score scaling (`StandardScaler`) treats all 512 bars equally, leading to "lagged" normalization where recent volatility is diluted by older data.

### SOTA 2026 Approach:
Use **Exponentially Weighted Moving Average (EWMA)** and **EWMSD (StdDev)** for scaling.
- **Mean**: `df['close'].ewm(span=32).mean()`
- **StdDev**: `df['close'].ewm(span=32).std()`
- **Z-Score**: `(x - ewma) / ewmsd`

### Benefit:
This ensures the "Zero Point" (Z=0) is much closer to the *current* market price, making the TTM-R2's zero-shot predictions more accurate for the immediate next steps.

---

## 3. JMCE Feedback Loop (The "Brake")
The Neural JMCE's **Shift Metric** detects correlation breakdowns.

### Integration Strategy:
- **Threshold**: If Shift Metric > 1.5 (🔥 HIGH VELOCITY).
- **Action**: Apply a **Dampening Multiplier** to the TTM forecast.
- **Logic**: `final_forecast = last_price + (model_delta * (1.0 / shift_metric))`.
- **Why**: Prevents "Trend Hallucination" during regime shifts. If the market is in high-velocity chaos, the model's confidence in a linear trend should be mathematically discounted.

---

---

## 5. Hardware-Level Pipeling (ANE -> GPU)
To optimize operational efficiency, the Neural JMCE (running on ANE) should serve as a "Pre-Processor" for the TTM-R2 (running on GPU).

### Integration Strategy:
- **State Handover**: The JMCE Shift Metric will be injected as a high-priority "Regime Metadata" tag into the TSFM pipeline.
- **Zero-Copy Transfers**: Explore using shared memory buffers between the ANE-based JMCE output and the GPU-based scaling logic to minimize CPU serialization overhead.

## 6. Batch Portfolio Vectorization
Running forecasting sequentially for 40+ tickers is inefficient.

### Efficiency Improvements:
- **Vectorized Pre-fetch**: Calculate indicators (RSI, ATR) for the *entire* portfolio in a single vectorized Pandas/NumPy block before spawning bridge scripts.
- **Parallel Inference**: Utilize `asyncio.Semaphore` to limit concurrent TTM-R2 bridge processes to match the available M4 GPU cores (e.g., 4-8 parallel sessions), preventing resource contention.

---

## 8. M4 Pro Hardware Exploitation (48GB Unified Memory)
The M4 Pro chip with 48GB of Unified Memory provides a unique opportunity for **Model Residency** and **Zero-Copy Data Pipelines**.

### Strategy for M4 Pro:
- **Model Residency**: Instead of the current "Load-Predict-Unload" cycle for the TSFM Bridge, we will implement a resident worker process. With 48GB, we can keep the TTM-R2 (1M-5M parameters) and the JMCE models permanently in memory, reducing the 200ms loading overhead to zero.
- **Unified Memory Zero-Copy**: Leverage the shared memory architecture. The indicators calculated on the CPU (using AMX matrix units) can be passed to the GPU/NPU without expensive serialization/deserialization.
- **Parallel Core Mapping**: The M4 Pro has 12-14 cores. We will optimize the concurrency semaphore to allow 10+ parallel forecasting sessions, saturating the GPU while keeping the UI responsive.
- **Precision Tuning**: Utilize `Float32` precision for the Neural JMCE on the NPU to exploit the M4's improved floating-point throughput, moving away from `Float16` where accuracy is paramount.
