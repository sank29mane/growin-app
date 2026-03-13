import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as optim
import numpy as np
import time
import os

# Import our MLX-native architecture
from backend.utils.jmce_model import NeuralJMCE, TimeResolution

def generate_synthetic_market_data(num_samples=1000, seq_len=78, n_assets=50):
    """
    Generates synthetic 5-minute tick data for training.
    In production, this would be replaced with the real T212/Alpaca tick database.
    """
    print(f"🧬 Generating synthetic market data ({num_samples} days, {seq_len} ticks/day, {n_assets} assets)...")
    # Simulate geometric brownian motion returns
    returns = np.random.normal(loc=0.0001, scale=0.002, size=(num_samples, seq_len, n_assets))
    
    # Introduce artificial correlation blocks to train the Cholesky Head
    # Make the first 5 assets (e.g., QQQ suite) highly correlated
    market_factor = np.random.normal(0, 0.005, size=(num_samples, seq_len, 1))
    returns[:, :, :5] += market_factor * 1.5 
    
    # Generate targets (next tick returns)
    targets = np.roll(returns, shift=-1, axis=1)
    
    return mx.array(returns.astype(np.float32)), mx.array(targets.astype(np.float32))

def jmce_loss(model, x, target_returns):
    """
    Custom Loss Function for Joint Mean-Covariance Estimation.
    Minimizes the Negative Log-Likelihood of a Multivariate Normal Distribution.
    """
    # Forward pass: predict mu, L, and Velocity
    mu, L, V = model(x, return_velocity=True)
    
    # SOTA: We only calculate loss against the last token's prediction (Sequence-to-One)
    # target_returns shape: (batch, seq_len, n_assets). Take the last one.
    y = target_returns[:, -1, :] 
    
    # --- 1. Mean Loss (MSE) ---
    mse_loss = mx.mean(mx.square(mu - y))
    
    # --- 2. Covariance Loss (NLL approximation) ---
    # Sigma = L * L^T
    # We want to maximize the likelihood of the observed returns given predicted mu & Sigma.
    # Simplified NLL loss for Cholesky: tr(Sigma^-1 * (y-mu)(y-mu)^T) + log(det(Sigma))
    # For training stability in this script, we'll use a robust proxy:
    # Force L to capture the outer product of the error
    error = y - mu # shape: (batch, n_assets)
    error_cov = error[..., None] * error[:, None, :] # outer product: (batch, n, n)
    
    # Reconstruct Sigma from L
    sigma = model.get_covariance(L)
    
    # Frobenius norm between predicted Sigma and empirical error Covariance
    cov_loss = mx.mean(mx.square(sigma - error_cov))
    
    # --- 3. Velocity Loss ---
    # Encourage the Velocity head to match the rate of change of the Cholesky factor
    # (Simplified auxiliary loss to keep gradients flowing to the velocity head)
    v_target = mx.random.normal(V.shape) * 0.01 # Placeholder for actual historical derivative
    vel_loss = mx.mean(mx.square(V - v_target))
    
    # Total Loss
    total_loss = mse_loss + (0.1 * cov_loss) + (0.05 * vel_loss)
    
    return total_loss

def train():
    print("🚀 Initializing SOTA 2026 M4 GPU Training Pipeline...")
    
    # 1. Hyperparameters
    N_ASSETS = 50
    SEQ_LEN = 78 # 1 day of 5-min candles
    BATCH_SIZE = 32
    EPOCHS = 50
    LEARNING_RATE = 1e-4
    
    # 2. Initialize Model (GPU via MLX)
    model = NeuralJMCE(n_assets=N_ASSETS, seq_len=SEQ_LEN, resolution=TimeResolution.INTRADAY_5MIN)
    mx.eval(model.parameters()) # Realize weights in memory
    
    # 3. Optimizer
    optimizer = optim.AdamW(learning_rate=LEARNING_RATE)
    
    # 4. Data
    X, Y = generate_synthetic_market_data(num_samples=1000, seq_len=SEQ_LEN, n_assets=N_ASSETS)
    
    # 5. Compilation
    # Use MLX's value_and_grad to compile the forward/backward pass efficiently on Metal
    loss_and_grad_fn = nn.value_and_grad(model, jmce_loss)
    
    print("⚡ Starting Metal GPU Training Loop...")
    start_time = time.time()
    
    for epoch in range(EPOCHS):
        epoch_loss = 0.0
        batches = 0
        
        # Mini-batch training
        for i in range(0, len(X), BATCH_SIZE):
            batch_x = X[i:i+BATCH_SIZE]
            batch_y = Y[i:i+BATCH_SIZE]
            
            # Forward + Backward
            loss, grads = loss_and_grad_fn(model, batch_x, batch_y)
            
            # Update weights
            optimizer.update(model, grads)
            
            # Force evaluation (execution) on the GPU
            mx.eval(model.parameters(), optimizer.state)
            
            epoch_loss += loss.item()
            batches += 1
            
        avg_loss = epoch_loss / batches
        if epoch % 10 == 0 or epoch == EPOCHS - 1:
            print(f"📈 Epoch {epoch:03d} | Loss: {avg_loss:.6f} | Time: {time.time() - start_time:.2f}s")
            
    print(f"✅ Training Complete. Total Time: {time.time() - start_time:.2f}s")
    
    # 6. Save Weights
    os.makedirs("models/mlx", exist_ok=True)
    weight_path = "models/mlx/jmce_50_5min.safetensors"
    model.save_weights(weight_path)
    print(f"💾 Weights saved to {weight_path}")

if __name__ == "__main__":
    train()
