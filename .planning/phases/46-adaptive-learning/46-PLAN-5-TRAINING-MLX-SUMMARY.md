# Phase 46: Adaptive Learning & Alpha Engineering (Plan 5: Training MLX) — SUMMARY

## 1. Objective Completed
Fine-tuned a 4-bit Quantized Small Language Model (SLM) using native MLX/VLM for dynamic regime prediction.

## 2. Work Done
- **Task 5.1:** Configured `mlx_vlm.lora` parameters to load the 4-bit quantized VLM and initialize LoRA adapters.
- **Task 5.2:** Modified the dataset generation in `scripts/prepare_training_data.py` to export in the `question`/`answer` format expected by `mlx_vlm` for prompt masking (`--train-on-completions`).
- **Task 5.3:** Executed QLoRA fine-tuning on the native target model `mlx-community/gemma-4-E4B-it-qat-4bit` via `mlx_vlm.lora` after upgrading the `mlx-vlm` package.
- **Task 5.4:** Ran the training script successfully on Metal GPU for 20 iterations and saved the adapter weights to `adapters/high_vol_bull/adapters.safetensors`.

## 3. Verification Results
- Fine-tuning completed successfully.
- Peak memory usage during training was strictly throttled at **12.62 GB**, well below the 60% memory limit (~28GB limit).
- Training loss decreased from **3.382** (Iteration 10) down to **0.874** (Iteration 20).
- Adapter weights successfully saved at `adapters/high_vol_bull/adapters.safetensors`.
