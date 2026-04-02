import numpy as np
from backend.utils.jmce_model import NeuralJMCE
from backend.utils.mlx_loader import mx, HAS_MLX

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
    mu, L = model(x)

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

if __name__ == "__main__":
    try:
        test_jmce_forward()
    except Exception as e:
        print(f"\n❌ Test Failed: {str(e)}")
        exit(1)
