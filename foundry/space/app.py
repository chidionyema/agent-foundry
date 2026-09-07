#!/usr/bin/env python3
"""foundry/space/app.py — Hugging Face Space entrypoint for the LoRA trainer.

This script runs inside an HF zero-GPU Space (ORDER.md §6). It drives the
`foundry/foundry_trainer.py` to: generate the secret-detector corpus, train the
0.5B LoRA, evaluate it honestly against the majority baseline, and save the
adapter + metrics.json (with optional push to HF Hub if the mounted
HF_TOKEN_FILE secret is present, which it must be for a real Space). It passes
the environment on to the trainer, and does NOT hold credentials itself.

Usage: executed by the HF Space runtime (e.g. `python app.py`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the project root is on the Python path so local modules can be found
# This script is at foundry/space/app.py, so we need to go up two levels.
_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from foundry.generator import generate, write_jsonl
from foundry.foundry_trainer import train, export_metrics


_DEFAULT_OUT_DIR = Path("foundry/artifact")
_DEFAULT_DATA_PATH = Path("foundry/secret-train.jsonl")


def main() -> None:
    print("--- Starting HF Space LoRA trainer ---")

    # 1. Generate the dataset (deterministic by seed)
    print("Generating synthetic dataset...")
    rows = generate(seed=7, n_per_class=400)  # Use the same seed as local tests
    _DEFAULT_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(rows, _DEFAULT_DATA_PATH)
    print(f"Dataset generated and saved to {_DEFAULT_DATA_PATH}")

    # 2. Prepare paths and HF token file for the trainer
    hf_token_file = os.environ.get("HF_TOKEN_FILE")
    if hf_token_file:  # It's expected to be set for a secure Space deployment
        hf_token_path = Path(hf_token_file)
        print(f"HF_TOKEN_FILE environment variable set: {hf_token_path}")
        # IMPORTANT: The trainer expects the *file itself*, not its path string
        # So we pass the Path object directly for consistent handling.
    else:
        hf_token_path = None
        print(
            "WARNING: HF_TOKEN_FILE not set. Adapter will be saved locally, NOT pushed to Hub."
        )

    # 3. Run the trainer
    print("Starting LoRA training...")
    try:
        metrics = train(
            train_rows=rows,  # Pass all generated rows for simplicity in space; split happens in train()
            test_rows=[],  # Test split will be generated internally by train() for consistent eval
            base_model="Qwen/Qwen2.5-0.5B-Instruct",  # As per locked decision
            out_dir=_DEFAULT_OUT_DIR,
            epochs=3,
            device=None,  # Auto-detect in trainer
            quantize_4bit=False,  # For 0.5B model, plain LoRA is fine on free GPU
            hf_token_file=hf_token_path,
        )
        print("LoRA training completed.")
        print(f"Metrics: {metrics}")
        export_metrics(
            _DEFAULT_OUT_DIR, metrics
        )  # Ensure metrics are saved consistently

    except Exception as e:
        print(f"ERROR during training: {e}", file=os.sys.stderr)
        import sys

        sys.exit(1)

    print(f"Artifacts saved to {_DEFAULT_OUT_DIR}")
    print("--- HF Space LoRA trainer finished ---")


if __name__ == "__main__":
    main()
