import numpy as np
from utils.jmce_model import NeuralJMCE
from utils.mlx_loader import mx, HAS_MLX

import pytest

@pytest.mark.skipif(not HAS_MLX, reason="MLX is not installed or available")
def test_jmce_forward():
    """
    Verifies the NeuralJMCE forward pass, output shapes, and the
    Positive Definite property of the predicted covariance matrix.
    """
    n_assets = 10
    seq_len = 180
    batch_size = 4

    print(f"Initializing NeuralJMCE with {n_assets} assets and seq_len {seq_len}...")

    # Initialize model
    model = NeuralJMCE(
        n_assets=n_assets,
        d_model=64,
        n_layers=2,
        n_heads=4,
        seq_len=seq_len
    )

    # Generate synthetic returns (Normal distribution)
    # returns: (batch, seq, assets)
    # In a real scenario, these would be daily log-returns.
    x = mx.random.normal((batch_size, seq_len, n_assets)) * 0.01

    # Forward pass
    mu, L, _ = model(x)

    # Calculate covariance Sigma = LL^T
    sigma = model.get_covariance(L)

    print(f"mu shape: {mu.shape} (Expected: ({batch_size}, {n_assets}))")
    print(f"L shape: {L.shape} (Expected: ({batch_size}, {n_assets}, {n_assets}))")
    print(f"Sigma shape: {sigma.shape} (Expected: ({batch_size}, {n_assets}, {n_assets}))")

    # 1. Check shapes
    assert mu.shape == (batch_size, n_assets), "Incorrect mu shape"
    assert L.shape == (batch_size, n_assets, n_assets), "Incorrect L shape"
    assert sigma.shape == (batch_size, n_assets, n_assets), "Incorrect sigma shape"

    # 2. Check if L is lower triangular
    L_np = np.array(L)
    for b in range(batch_size):
        if not np.allclose(L_np[b], np.tril(L_np[b])):
            raise AssertionError(f"L matrix is not lower triangular for batch {b}")

    # 3. Check if covariance is Positive Definite (PD)
    # A symmetric matrix is PD if and only if all its eigenvalues are strictly positive.
    sigma_np = np.array(sigma)
    for b in range(batch_size):
        # Use eigvalsh for symmetric matrices
        eigenvalues = np.linalg.eigvalsh(sigma_np[b])
        min_ev = eigenvalues.min()
        print(f"Batch {b} - Min Eigenvalue: {min_ev:.6f}")

        # We expect strictly positive eigenvalues because of the exp() on L's diagonal.
        # Floating point precision might lead to very small positives, but they must be > 0.
        assert min_ev > 0, f"Covariance matrix is not Positive Definite for batch {b} (min_ev={min_ev})"

        # Also check symmetry
        if not np.allclose(sigma_np[b], sigma_np[b].T, atol=1e-6):
            raise AssertionError(f"Covariance matrix is not symmetric for batch {b}")

    print("\n✅ NeuralJMCE Forward Pass Verification Successful!")
    print("Verification passed for shapes, triangularity, symmetry, and Positive Definiteness.")

@pytest.mark.skipif(not HAS_MLX, reason="MLX is not installed or available")
def test_jmce_forward_returns_three_values():
    """
    Regression test: model() must always return a 3-tuple (mu, L, V).
    Prior to this PR the call returned only (mu, L); the unpacking to
    `mu, L, _` would raise ValueError with the old signature.
    """
    model = NeuralJMCE(n_assets=4, d_model=32, n_layers=1, n_heads=2, seq_len=20)
    x = mx.random.normal((2, 20, 4)) * 0.01
    result = model(x)
    assert len(result) == 3, f"Expected 3-tuple, got {len(result)}-tuple"


@pytest.mark.skipif(not HAS_MLX, reason="MLX is not installed or available")
def test_jmce_default_call_velocity_is_none():
    """
    When return_velocity is not specified (defaults to False) the third
    return value (V) must be None.
    """
    model = NeuralJMCE(n_assets=4, d_model=32, n_layers=1, n_heads=2, seq_len=20)
    x = mx.random.normal((2, 20, 4)) * 0.01
    mu, L, V = model(x)
    assert V is None, "V should be None when return_velocity=False"


@pytest.mark.skipif(not HAS_MLX, reason="MLX is not installed or available")
def test_jmce_return_velocity_true_gives_non_none():
    """
    When return_velocity=True the third return value (V) must be a non-None
    array with the same shape as L.
    """
    n_assets = 4
    batch_size = 2
    seq_len = 20
    model = NeuralJMCE(n_assets=n_assets, d_model=32, n_layers=1, n_heads=2, seq_len=seq_len)
    x = mx.random.normal((batch_size, seq_len, n_assets)) * 0.01
    mu, L, V = model(x, return_velocity=True)

    assert V is not None, "V should be non-None when return_velocity=True"
    assert V.shape == L.shape, (
        f"Velocity V shape {V.shape} must match Cholesky L shape {L.shape}"
    )


@pytest.mark.skipif(not HAS_MLX, reason="MLX is not installed or available")
def test_jmce_velocity_shape_single_asset():
    """
    Regression for the backend/test_jmce.py scenario (n_assets=1, intraday
    5-min resolution): return_velocity must work correctly for a single asset.
    """
    from utils.jmce_model import TimeResolution
    model = NeuralJMCE(
        n_assets=1,
        d_model=32,
        n_layers=1,
        n_heads=1,
        seq_len=78,
        resolution=TimeResolution.INTRADAY_5MIN,
    )
    x = mx.random.normal((1, 78, 1)) * 0.01
    mu, L, V = model(x, return_velocity=True)

    assert V is not None
    # For n_assets=1 the Cholesky matrix is (batch, 1, 1)
    assert V.shape == (1, 1, 1), f"Expected (1,1,1), got {V.shape}"
    # The scalar value must be finite
    v_scalar = float(V[0, 0, 0])
    assert np.isfinite(v_scalar)


if __name__ == "__main__":
    try:
        test_jmce_forward()
    except Exception as e:
        print(f"\n❌ Test Failed: {str(e)}")
        exit(1)
