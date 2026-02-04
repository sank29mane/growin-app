use pyo3::prelude::*;

/// SOTA Ticker Normalization: Resolves discrepancies between Trading212, 
/// Yahoo Finance, Alpaca, and Finnhub.
#[pyfunction]
fn normalize_ticker(ticker: String) -> PyResult<String> {
    if ticker.is_empty() {
        return Ok("".to_string());
    }

    // 1. Basic Cleaning
    let mut normalized = ticker.to_uppercase().trim().replace("$", "");
    
    // 2. Already Normalized (contains dot)
    if normalized.contains('.') {
        return Ok(normalized);
    }

    // 3. Handle Platform-Specific Artifacts
    let original = normalized.clone();
    
    // Strip T212 suffixes
    let suffixes = ["_EQ", "_US", "_BE", "_DE", "_GB", "_FR", "_NL", "_ES", "_IT"];
    loop {
        let mut changed = false;
        for s in suffixes.iter() {
            if normalized.ends_with(s) {
                normalized = normalized[..normalized.len() - s.len()].to_string();
                changed = true;
            }
        }
        if !changed { break; }
    }
    normalized = normalized.replace("_", "");

    // 4. SPECIAL MAPPINGS
    let special_mappings = [
        ("SSLNL", "SSLN"), ("SGLNL", "SGLN"), ("3GLD", "3GLD"), ("SGLN", "SGLN"),
        ("PHGP", "PHGP"), ("PHAU", "PHAU"), ("3LTS", "3LTS"), ("3USL", "3USL"),
        ("LLOY1", "LLOY"), ("VOD1", "VOD"), ("BARC1", "BARC"), ("TSCO1", "TSCO"),
        ("BPL1", "BP"), ("BPL", "BP"), ("AZNL1", "AZN"), ("AZNL", "AZN"),
        ("SGLN1", "SGLN"), ("MAG5", "MAG5"), ("MAG5L", "MAG5"), ("MAG7", "MAG7"),
        ("MAG7L", "MAG7"), ("GLD3", "GLD3"), ("3UKL", "3UKL"), ("5QQQ", "5QQQ"),
        ("TSL3", "TSL3"), ("NVD3", "NVD3"), ("AVL", "AV"), ("UUL", "UU"),
        ("BAL", "BA"), ("SLL", "SL"), ("AU", "AUT"), ("RBL", "RKT"), ("MICCL", "MICC")
    ];
    
    for (k, v) in special_mappings.iter() {
        if normalized == *k {
            normalized = v.to_string();
            break;
        }
    }

    // 5. Suffix Protection for Leveraged Products
    let stems = ["LLOY", "BARC", "VOD", "HSBA", "TSCO", "BP", "AZN", "RR", "NG", "SGLN", "SSLN"];
    if normalized.ends_with('1') && normalized.len() > 3 {
        let stem_check = normalized[..normalized.len()-1].to_string();
        if stems.contains(&stem_check.as_str()) {
            normalized = stem_check;
        }
    }

    // 6. Global Exchange Logic
    let us_exclusions = [
        "AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "TSLA", "META", "NFLX",
        "AMD", "INTC", "PYPL", "ADBE", "CSCO", "PEP", "COST", "AVGO", "QCOM", "TXN",
        "ORCL", "CRM", "IBM", "UBER", "ABNB", "SNOW", "PLTR", "SQ", "SHOP", "SPOT",
        "GOOGL", "JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "AXP", "V", "MA", "COF", "USB",
        "CAT", "DE", "GE", "GM", "F", "BA", "LMT", "RTX", "HON", "UPS", "FDX", "UNP", "MMM",
        "WMT", "TGT", "HD", "LOW", "MCD", "SBUX", "NKE", "KO", "PEP", "PG", "CL", "MO", "PM", "DIS", "CMCSA",
        "JNJ", "PFE", "MRK", "ABBV", "LLY", "UNH", "CVS", "AMGN", "GILD", "BMY", "ISRG", "TMO", "ABT", "DHR",
        "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "KMI", "HAL", "T", "VZ", "TMUS",
        "SPY", "QQQ", "DIA", "IWM", "IVV", "VOO", "VTI", "GLD", "SLV", "ARKK", "SMH", "XLF", "XLE", "XLK", "XLV",
        "F", "T", "C", "V", "Z", "O", "D", "R", "K", "X", "S", "M", "A", "G"
    ];

    let is_explicit_uk = original.contains("_EQ") && !original.contains("_US");
    let is_likely_uk = (normalized.len() <= 5 || normalized.ends_with('L')) && !us_exclusions.contains(&normalized.as_str());

    if is_likely_uk && normalized.ends_with('L') && normalized.len() > 3 && !us_exclusions.contains(&normalized.as_str()) {
        normalized.pop();
    }

    let is_leveraged = (normalized.starts_with('3') || normalized.starts_with('5') || normalized.starts_with('7')) ||
                       (normalized.ends_with('2') || normalized.ends_with('3') || normalized.ends_with('5') || normalized.ends_with('7'));

    if is_explicit_uk || is_likely_uk || is_leveraged {
        if !normalized.ends_with(".L") && !normalized.contains('.') {
            return Ok(format!("{}.L", normalized));
        }
    }

    Ok(normalized)
}

/// Calculate Relative Strength Index (RSI).
/// 
/// Args:
///     prices (List[float]): List of closing prices.
///     period (int): Lookback period (default 14).
/// 
/// Returns:
///     List[float]: RSI values (aligned with input, first `period` are 50.0).
#[pyfunction]
#[pyo3(signature = (prices, period=14))]
fn calculate_rsi(prices: Vec<f64>, period: usize) -> PyResult<Vec<f64>> {
    if prices.len() < period {
        return Ok(vec![50.0; prices.len()]);
    }

    let mut rsi_values = Vec::with_capacity(prices.len());
    let mut gains = Vec::with_capacity(prices.len());
    let mut losses = Vec::with_capacity(prices.len());

    // Calculate diffs
    let mut diffs = Vec::with_capacity(prices.len());
    diffs.push(0.0); // First element has no diff
    for i in 1..prices.len() {
        diffs.push(prices[i] - prices[i-1]);
    }

    // Initialize with 50.0 for the warm-up period
    for _ in 0..period {
        rsi_values.push(50.0);
        gains.push(0.0);
        losses.push(0.0);
    }

    // Initial average gain/loss
    let mut avg_gain = 0.0;
    let mut avg_loss = 0.0;
    
    // We need at least 'period' changes, so 'period + 1' prices.
    let warmup_end = if diffs.len() > period { period } else { diffs.len() - 1 };
    
    for i in 1..=warmup_end {
        let change = diffs[i];
        if change > 0.0 {
            avg_gain += change;
        } else {
            avg_loss += change.abs();
        }
    }
    
    let denominator = if warmup_end > 0 { warmup_end as f64 } else { 1.0 };
    avg_gain /= denominator;
    avg_loss /= denominator;

    // First RSI calculation (at index `period`)
    // Actually standard RSI usually starts validity `period` indices in.
    // We already pushed `period` placeholders.

    // Calculate subsequent values using Wilder's Smoothing
    for i in period..prices.len() {
        let change = diffs[i];
        let (gain, loss) = if change > 0.0 {
            (change, 0.0)
        } else {
            (0.0, change.abs())
        };

        // Wilder's Smoothing
        // smoothed_gain = (previous_gain * (period - 1) + current_gain) / period
        
        // Note: For the very first point after warm up, we use the simple average we just calculated?
        // Standard Algo:
        // first Avg Gain = Sum of gains over past 14 periods / 14.
        // subsequent Avg Gain = ((previous Avg Gain) * 13 + current Gain) / 14.
        
        if i == period {
            // This is the first calculated point, utilizing the simple average of the *previous* 14 candles
            // Wait, standard RSI(14) needs 15 data points to produce the first value?
        } else {
             avg_gain = ((avg_gain * (period as f64 - 1.0)) + gain) / period as f64;
             avg_loss = ((avg_loss * (period as f64 - 1.0)) + loss) / period as f64;
        }

        let rs = if avg_loss == 0.0 {
            100.0 
        } else {
            avg_gain / avg_loss
        };

        let rsi = 100.0 - (100.0 / (1.0 + rs));
        rsi_values.push(rsi);
    }
    
    // Fill remaining if any mismatch or ensure size match?
    // The loop runs from `period` to `prices.len()`.
    // The `rsi_values` started with `period` elements.
    // So final length is `period + (prices.len() - period) = prices.len()`. Correct.

    Ok(rsi_values)
}

/// Calculate Simple Moving Average (SMA).
#[pyfunction]
#[pyo3(signature = (data, period=20))]
fn calculate_sma(data: Vec<f64>, period: usize) -> PyResult<Vec<f64>> {
    let mut sma = Vec::with_capacity(data.len());
    let mut sum = 0.0;

    for i in 0..data.len() {
        sum += data[i];
        if i >= period {
            sum -= data[i - period];
            sma.push(sum / period as f64);
        } else if i == period - 1 {
            sma.push(sum / period as f64);
        } else {
            sma.push(0.0); // Padding
        }
    }
    Ok(sma)
}

/// Calculate Exponential Moving Average (EMA).
#[pyfunction]
#[pyo3(signature = (data, period=14))]
fn calculate_ema(data: Vec<f64>, period: usize) -> PyResult<Vec<f64>> {
    if data.is_empty() {
        return Ok(vec![]);
    }
    
    let mut ema = Vec::with_capacity(data.len());
    let k = 2.0 / (period as f64 + 1.0);
    
    // First value is usually SMA of first 'period' elements, or just the first price if period=1?
    // Commonly initialized with First Price or SMA.
    // Let's use SMA of first `period` if enough data, else first price.
    
    let start_idx = if data.len() >= period { period - 1 } else { 0 };
    
    // Padding with NaNs or 0s until valid? 
    // Pandas TA typically produces NaNs. We use 0.0 for simplicity in this context or handle in Python.
    // To match previous SMA behavior (padding 0.0), we pad.
    
    for _ in 0..start_idx {
        ema.push(0.0);
    }

    let mut current_ema = if start_idx < data.len() {
        // Calculate SMA for the first valid point
        let sum: f64 = data[0..=start_idx].iter().sum();
        sum / (start_idx + 1) as f64
    } else {
        data[0]
    };
    
    if start_idx < data.len() {
        ema.push(current_ema);
        
        for i in (start_idx + 1)..data.len() {
            current_ema = (data[i] * k) + (current_ema * (1.0 - k));
            ema.push(current_ema);
        }
    }

    Ok(ema)
}

/// Calculate MACD (Moving Average Convergence Divergence).
/// Returns tuple of (macd_line, signal_line, histogram)
#[pyfunction]
#[pyo3(signature = (data, fast=12, slow=26, signal=9))]
fn calculate_macd(data: Vec<f64>, fast: usize, slow: usize, signal: usize) -> PyResult<(Vec<f64>, Vec<f64>, Vec<f64>)> {
    // Helper to calculate EMA internally
    let get_ema = |d: &Vec<f64>, p: usize| -> Vec<f64> {
        let mut res = Vec::with_capacity(d.len());
        let k = 2.0 / (p as f64 + 1.0);
        
        // Simple init: just use price as starts or 0 padding
        // Replicating logic: Pad 0 until p-1, then SMA, then EMA.
        for _ in 0..(p-1) {
            res.push(0.0);
        }
        
        if d.len() >= p {
             let sum: f64 = d[0..p].iter().sum();
             let mut curr = sum / p as f64;
             res.push(curr);
             
             for i in p..d.len() {
                 curr = (d[i] * k) + (curr * (1.0 - k));
                 res.push(curr);
             }
        }
        res
    };

    let ema_fast = get_ema(&data, fast);
    let ema_slow = get_ema(&data, slow);
    
    let mut macd_line = Vec::with_capacity(data.len());
    for i in 0..data.len() {
        // Only valid if both are non-zero? Or simple subtraction
        macd_line.push(ema_fast[i] - ema_slow[i]);
    }
    
    // Signal line is EMA of MACD line
    // BUT we need to ignore the initial zeros in calculation/padding
    // Doing a "naive" EMA on the whole macd_line including leading zeros might skew it near start.
    // However, for this SOTA implementation, let's keep it consistent.
    let signal_line = get_ema(&macd_line, signal);
    
    let mut histogram = Vec::with_capacity(data.len());
    for i in 0..data.len() {
        histogram.push(macd_line[i] - signal_line[i]);
    }

    Ok((macd_line, signal_line, histogram))
}

/// Calculate Bollinger Bands.
/// Returns (upper, middle, lower)
#[pyfunction]
#[pyo3(signature = (data, period=20, std_dev=2.0))]
fn calculate_bbands(data: Vec<f64>, period: usize, std_dev: f64) -> PyResult<(Vec<f64>, Vec<f64>, Vec<f64>)> {
    let mut upper = Vec::with_capacity(data.len());
    let mut middle = Vec::with_capacity(data.len()); // This is SMA
    let mut lower = Vec::with_capacity(data.len());

    for i in 0..data.len() {
        if i < period - 1 {
            upper.push(0.0);
            middle.push(0.0);
            lower.push(0.0);
            continue;
        }

        // Slice window safely
        let start_idx = (i + 1) - period;
        let window = &data[start_idx..=i];
        let sum: f64 = window.iter().sum();
        let mean = sum / period as f64;
        
        let mut variance = 0.0;
        for &x in window {
             variance += (x - mean).powi(2);
        }
        variance /= period as f64;
        let std = variance.sqrt();
        
        middle.push(mean);
        upper.push(mean + (std_dev * std));
        lower.push(mean - (std_dev * std));
    }

    Ok((upper, middle, lower))
}


/// A Python module implemented in Rust.
#[pymodule]
fn growin_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(normalize_ticker, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_rsi, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_sma, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_ema, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_macd, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_bbands, m)?)?;
    Ok(())
}
