import pytest
import numpy as np
from datetime import date
from decimal import Decimal
from backend.data_models import DividendData
from backend.dividend_bridge import DividendBridge

@pytest.fixture
def mock_dividend_history():
    """
    Creates a mock dividend history with some noise and outliers.
    """
    history = []
    
    # Generate 20 quarterly dividends for AAPL with slight noise
    base_amount = 0.25
    for i in range(20):
        # Using a deterministic pseudo-random sequence for tests
        noise = (i % 3 - 1) * 0.01 
        # Add an outlier
        if i == 10:
            noise = 0.5 
            
        history.append(DividendData(
            ticker="AAPL",
            amount=Decimal(str(round(base_amount + noise, 4))),
            ex_date=date(2020 + (i // 4), (i % 4) * 3 + 1, 15),
            frequency="QUARTERLY"
        ))
        
    return history

def test_robust_iqr_scale():
    bridge = DividendBridge()
    data = np.array([10.0, 11.0, 12.0, 13.0, 100.0])  # 100 is an outlier
    
    scaled = bridge.robust_iqr_scale(data)
    
    median = np.median(data)
    q75, q25 = np.percentile(data, [75, 25])
    iqr = q75 - q25
    
    expected = (data - median) / iqr
    assert np.allclose(scaled, expected)
    # Robust scaling dampens outliers compared to standard scaling
    # Standard scale: (100 - 29.2) / 35.4 = 1.99
    # Robust scale: (100 - 12.0) / 2.0 = 44.0 
    # Wait, actually robust scaling gives LARGER values for outliers relative to IQR
    # because IQR is small. But it keeps the "normal" data in a tight range around 0.
    assert scaled[4] > 10.0 

def test_sempo_easd_filter():
    bridge = DividendBridge()
    # A sine wave with noise
    t = np.linspace(0, 4*np.pi, 30)
    data = np.sin(t) + (np.sin(t*10) * 0.1) # signal + "noise"
    
    filtered = bridge.sempo_easd_filter(data)
    
    assert filtered.shape == data.shape
    # Filtered signal should be smoother than noisy signal
    # (Checking variance as a proxy for smoothness)
    assert np.var(np.diff(filtered)) < np.var(np.diff(data))

def test_process_dividend_history(mock_dividend_history):
    bridge = DividendBridge()
    processed = bridge.process_dividend_history(mock_dividend_history)
    
    assert len(processed) == len(mock_dividend_history)
    for div in processed:
        assert div.iqr_scaled_amount is not None
        assert div.sempo_filtered_signal is not None
        assert isinstance(div.iqr_scaled_amount, float)
        assert isinstance(div.sempo_filtered_signal, float)

def test_prepare_for_ttm_sparsity():
    bridge = DividendBridge()
    # Only 10 points (well below 512 context)
    history = [
        DividendData(ticker="AAPL", amount=Decimal("0.25"), ex_date=date(2023, 1, 1))
        for _ in range(10)
    ]
    
    # Should not crash, handles sparsity
    result = bridge.prepare_for_ttm(history, context_points=512)
    assert len(result) == 10
    assert result.dtype == np.float64

def test_robust_iqr_scale_constant_data():
    bridge = DividendBridge()
    data = np.array([10.0, 10.0, 10.0])
    
    scaled = bridge.robust_iqr_scale(data)
    # Median is 10, IQR is 0. Expected: all zeros.
    assert np.all(scaled == 0.0)

def test_sempo_easd_filter_too_few_points():
    bridge = DividendBridge()
    data = np.array([1.0, 2.0])
    
    filtered = bridge.sempo_easd_filter(data)
    # Should return original data if too few points for filter
    assert np.array_equal(filtered, data)
