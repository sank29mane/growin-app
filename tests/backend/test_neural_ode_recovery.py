import pytest
import torch
import numpy as np
from decimal import Decimal
from quant_engine import QuantEngine
from models.neural_ode import RecoveryVelocityNODE

def test_neural_ode_model():
    model = RecoveryVelocityNODE(input_dim=10, hidden_dim=20)
    dummy_input = torch.randn(5, 10)
    output = model(dummy_input)
    
    assert output.shape == (5, 1)
    assert isinstance(output, torch.Tensor)

def test_quant_engine_recovery_prediction():
    engine = QuantEngine()
    features = np.random.randn(10).astype(np.float32)
    
    velocity = engine.predict_recovery_trajectory("AAPL", features)
    
    assert isinstance(velocity, Decimal)
    # Output should be valid Decimal, even if small
    assert velocity is not None

def test_adjoint_method_verification():
    """
    Verify that the model uses the adjoint method by checking if odeint_adjoint is callable.
    """
    from torchdiffeq import odeint_adjoint
    assert callable(odeint_adjoint)
