import torch
import torch.nn as nn
import coremltools as ct
import numpy as np
import os

class SimpleSelfAttention(nn.Module):
    def __init__(self, d_model, n_heads):
        super().__init__()
        self.d_model = d_model
        self.n_heads = n_heads
        self.head_dim = d_model // n_heads
        self.q_proj = nn.Linear(d_model, d_model)
        self.k_proj = nn.Linear(d_model, d_model)
        self.v_proj = nn.Linear(d_model, d_model)
        self.out_proj = nn.Linear(d_model, d_model)

    def forward(self, x):
        batch_size, seq_len, _ = x.shape
        q = self.q_proj(x).view(batch_size, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        k = self.k_proj(x).view(batch_size, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        v = self.v_proj(x).view(batch_size, seq_len, self.n_heads, self.head_dim).transpose(1, 2)

        # Scale dot product
        scores = torch.matmul(q, k.transpose(-2, -1)) / (self.head_dim ** 0.5)
        attn = torch.softmax(scores, dim=-1)
        
        # Weighted sum of values
        context = torch.matmul(attn, v).transpose(1, 2).contiguous()
        context = context.view(batch_size, seq_len, self.d_model)
        return self.out_proj(context)

class SimpleTransformerBlock(nn.Module):
    def __init__(self, d_model, n_heads):
        super().__init__()
        self.attn = SimpleSelfAttention(d_model, n_heads)
        self.ln1 = nn.LayerNorm(d_model)
        self.ln2 = nn.LayerNorm(d_model)
        self.mlp = nn.Sequential(
            nn.Linear(d_model, d_model * 4),
            nn.GELU(),
            nn.Linear(d_model * 4, d_model)
        )

    def forward(self, x):
        h = x + self.attn(self.ln1(x))
        h = h + self.mlp(self.ln2(h))
        return h

class NeuralJMCE(nn.Module):
    """
    SOTA 2026: PyTorch implementation of JMCE for CoreML export.
    Matches the MLX architecture exactly for NPU parity.
    Uses trace-friendly attention blocks to bypass CoreML conversion bugs.
    """
    def __init__(self, n_assets=50, d_model=128, n_layers=3, n_heads=4, seq_len=180):
        super().__init__()
        self.n_assets = n_assets
        self.cholesky_size = (n_assets * (n_assets + 1)) // 2
        
        # 1. Input Projection
        self.input_proj = nn.Linear(n_assets, d_model)
        
        # 2. Transformer Encoder (Trace-friendly custom layers)
        self.layers = nn.ModuleList([
            SimpleTransformerBlock(d_model, n_heads) for _ in range(n_layers)
        ])
        
        # 3. Task Heads
        self.mu_head = nn.Linear(d_model, n_assets)
        self.cholesky_head = nn.Linear(d_model, self.cholesky_size)
        self.velocity_head = nn.Linear(d_model, self.cholesky_size)

    def forward(self, x):
        # x shape: (1, seq_len, n_assets)
        h = self.input_proj(x)
        
        # Transformer layers
        for layer in self.layers:
            h = layer(h)
            
        # Take the last token's representation for the final estimate
        h_final = h[:, -1]
        
        mu = self.mu_head(h_final)
        cholesky = self.cholesky_head(h_final)
        velocity = self.velocity_head(h_final)
        
        return mu, cholesky, velocity

def export():
    n_assets = 50
    seq_len = 78 # 5-min intervals for 1 day
    
    # Force deterministic behavior for tracing
    torch.manual_seed(42)
    model = NeuralJMCE(n_assets=n_assets, seq_len=seq_len)
    model.eval()
    
    # Create example input
    example_input = torch.randn(1, seq_len, n_assets)
    
    # Trace the model
    traced_model = torch.jit.trace(model, example_input)
    
    # CoreML Export
    # Target ANE by specifying Tensor types and fixed shapes
    mlmodel = ct.convert(
        traced_model,
        inputs=[ct.TensorType(shape=example_input.shape, name="returns")],
        outputs=[
            ct.TensorType(name="mu"),
            ct.TensorType(name="cholesky"),
            ct.TensorType(name="velocity")
        ],
        convert_to="mlprogram", # Modern format for ANE
        compute_precision=ct.precision.FLOAT16,
        compute_units=ct.ComputeUnit.CPU_AND_NE,
        minimum_deployment_target=ct.target.macOS14 
    )
    
    # Metadata for Xcode
    mlmodel.author = "Growin AI"
    mlmodel.license = "Proprietary SOTA 2026"
    mlmodel.short_description = "High-Velocity Joint Mean-Covariance Estimator for M4 Pro ANE"
    
    # Save
    output_path = "models/coreml/NeuralJMCE.mlpackage"
    os.makedirs("models/coreml", exist_ok=True)
    mlmodel.save(output_path)
    
    print(f"✅ SOTA: JMCE exported to {output_path}")
    print(f"📍 Targets: ANE, CPU (MLComputeUnits.cpuAndNeuralEngine)")

if __name__ == "__main__":
    export()
