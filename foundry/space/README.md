---
title: Agent Foundry LoRA Trainer
emoji: 🛠️
colorFrom: blue
colorTo: green
sdk: docker
license: mit
---

# Agent Foundry LoRA Trainer Space

This Hugging Face Space runs the `foundry/foundry_trainer.py` script, as authorized by ORDER.md §6 and §7.

It is designed to:
1. Generate a deterministic, class-balanced secret-detector corpus using `foundry.generator`.
2. Train a small instruction-tuned base model (default `Qwen/Qwen2.5-0.5B-Instruct`) with a LoRA adapter.
3. Evaluate the trained adapter's exact-label accuracy on a withheld test split, alongside a majority-class baseline.
4. Save the LoRA adapter and a `metrics.json` file.
5. Optionally push the trained adapter to a Hugging Face Hub repository if the `HF_TOKEN_FILE` secret is properly mounted.

## Security

Per ORDER.md §6, this Space adheres to the estate's policy-clean credential model. The Hugging Face API token is **never** hardcoded or passed via environment variables directly. Instead, it is provided as a mounted secret file (e.g., `/run/secrets/hf_token.txt`) and referenced by the `HF_TOKEN_FILE` environment variable. The Space will refuse to push to the Hub if this file is declared but absent.

## Usage

This Space is configured to run on a Hugging Face zero-GPU environment. The training process is initiated by the `app.py` script.

To launch, ensure the Space's secrets are correctly set up in your Hugging Face account for this Space:
- `HF_TOKEN_FILE`: The path where the HF API token is mounted as a file (e.g., `/run/secrets/hf_token.txt`).
