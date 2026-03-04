import torch
import torch.nn as nn
from typing import Optional
from torchdiffeq import odeint_adjoint as odeint

class ODEFunction(nn.Module):
    """
    Neural network representing the derivative dh/dt = f(h(t), t, theta)
    """
    def __init__(self, hidden_dim: int):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim)
        )

    def forward(self, t, h):
        # Time-agnostic derivative in this simple model, 
        # but t is required by torchdiffeq
        return self.net(h)

class RecoveryVelocityNODE(nn.Module):
    """
    Neural ODE model for predicting post-dividend price recovery velocity.
    Uses Adjoint Sensitivity Method for O(1) memory cost during training.
    """
    def __init__(self, input_dim: int = 16, hidden_dim: int = 32):
        super().__init__()
        self.input_proj = nn.Linear(input_dim, hidden_dim)
        self.ode_func = ODEFunction(hidden_dim)
        self.output_proj = nn.Linear(hidden_dim, 1) # Recovery Velocity output

    def forward(self, x: torch.Tensor, t: Optional[torch.Tensor] = None):
        """
        Forward pass using torchdiffeq solver.
        
        Args:
            x: Input tensor of shape (batch_size, input_dim)
            t: Time points for integration (defaults to [0, 1])
            
        Returns:
            Predicted recovery velocity at end of integration
        """
        if t is None:
            t = torch.tensor([0.0, 1.0]).to(x.device)
            
        # Map input to hidden space (h0)
        h0 = self.input_proj(x)
        
        # ODE Integration: h(t) = h0 + integral f(h(tau), tau) dtau
        # Use adjoint method for memory efficiency
        h_trajectory = odeint(self.ode_func, h0, t, rtol=1e-3, atol=1e-4)
        
        # Take the final hidden state
        h_final = h_trajectory[-1]
        
        # Map back to prediction space
        velocity = self.output_proj(h_final)
        return velocity

if __name__ == "__main__":
    # Quick test for Task 1 verification
    model = RecoveryVelocityNODE(input_dim=10, hidden_dim=20)
    dummy_input = torch.randn(5, 10)
    output = model(dummy_input)
    print(f"Output shape: {output.shape}")
    assert output.shape == (5, 1)
    print("Task 1 verification PASSED")
