import torch
import torch.nn as nn

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
        return self.net(h)

class RecoveryODE(nn.Module):
    """
    Neural ODE model for predicting post-dividend price recovery velocity.
    Models the continuous-time evolution of price features.
    """
    def __init__(self, input_dim: int = 16, hidden_dim: int = 32):
        super().__init__()
        self.input_proj = nn.Linear(input_dim, hidden_dim)
        self.ode_func = ODEFunction(hidden_dim)
        self.output_proj = nn.Linear(hidden_dim, 1) # Recovery Velocity output

    def forward(self, x: torch.Tensor, steps: int = 10, dt: float = 0.1):
        """
        Forward pass using Euler integration for the ODE.
        
        Args:
            x: Input tensor of shape (batch_size, input_dim)
            steps: Number of integration steps
            dt: Time step size
            
        Returns:
            Predicted recovery velocity (scalar per sample)
        """
        # Map input to hidden space (h0)
        h = self.input_proj(x)
        
        # ODE Integration: h(t+dt) = h(t) + dt * f(h(t), t)
        for i in range(steps):
            t = i * dt
            h = h + dt * self.ode_func(t, h)
            
        # Map back to prediction space
        velocity = self.output_proj(h)
        return velocity

if __name__ == "__main__":
    # Quick test for Task 1 verification
    model = RecoveryODE(input_dim=10, hidden_dim=20)
    dummy_input = torch.randn(5, 10)
    output = model(dummy_input)
    print(f"Output shape: {output.shape}")
    assert output.shape == (5, 1)
    print("Task 1 verification PASSED")
