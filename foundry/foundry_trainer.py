#!/usr/bin/env python3
"""foundry/foundry_trainer.py — train the secret-detector LoRA (ORDER.md §6/§7).

Given the corpus generator.py produced, fine-tune a small instruction base so that
given a messy log line it emits exactly one label from the closed vocabulary:
    {none, aws_access_key, aws_secret_key, github_pat, google_api, slack_token}

Host / device : automatic — CUDA if present, else Apple MPS, else CPU (the CPU
                fallback you asked for). This laptop has no GPU; the FIT belongs on
                HF free GPU (§6) or a CPU box with torch+transformers+peft. The
                pure-Python parts (build_examples, split, majority baseline, export)
                run and are unit-tested ANYWHERE, no HF library required.

Credential    : per §6 the trainer NEVER holds an HF token. It pushes only when a
                mounted secret FILE (HF_TOKEN_FILE) is present, never from an env var
                or console paste. Otherwise it exports locally and refuses to push.

Honest-fit rule: train() runs a REAL fit or RAISES. It never returns a fabricated
"trained" result. On a host without torch/transformers/peft it fails closed with a
plain reason — nothing pretends to have learned.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from collections import Counter
from pathlib import Path
from typing import Sequence

from foundry.generator import LABELS

SYSTEM = (
    "You detect whether a log line contains an embedded secret and classify its "
    "type. Answer with exactly one token from this list: " + ", ".join(LABELS) + "."
)

_INSTRUCTION = (
    "{system}\n\nMessage:\nClassify the secret type present in this log line. "
    "Answer with one token only.\n\nLog: {text}\n\nAnswer:"
)


# --------------------------------------------------------------------------- #
# Pure-Python helpers — run and are tested anywhere, no HF library needed.    #
# --------------------------------------------------------------------------- #


def build_examples(rows: Sequence[dict]) -> list[dict]:
    """Shape labeled rows into (prompt, completion) training pairs."""
    examples = []
    for r in rows:
        prompt = _INSTRUCTION.format(system=SYSTEM, text=r["text"])
        examples.append({"prompt": prompt, "completion": r["label"]})
    return examples


def split(
    rows: Sequence[dict], seed: int = 7, test_frac: float = 0.2
) -> tuple[list[dict], list[dict]]:
    """Stratified, deterministic train/test split: every class appears in both."""
    rng = random.Random(seed)  # noqa: S311 - seeded determinism required (estate law), not crypto
    by_label: dict[str, list[dict]] = {}
    for r in rows:
        by_label.setdefault(r["label"], []).append(r)
    train: list[dict] = []
    test: list[dict] = []
    for _label, group in by_label.items():
        n_test = max(1, int(round(len(group) * test_frac)))
        pool = group[:]
        picks = set(id(p) for p in rng.sample(pool, n_test))
        for r in group:
            (test if id(r) in picks else train).append(r)
    # Re-permute deterministically with the (same) rng stream.
    rng.shuffle(train)
    rng.shuffle(test)
    return train, test


def pick_device() -> str:
    """Resolve the execution device. Pure decision, real value, tested without torch."""
    # Testable core: no heavy imports until we must confirm which is actually usable.
    import_context = _probe("torch")
    if not import_context:
        return "host-without-torch"
    return _device_from_torch()


def _probe(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001 - absent on this laptop
        return False


def _device_from_torch() -> str:
    # Import is safe (probe passed); resolve cuda -> mps -> cpu.
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if (
            getattr(torch, "backends", None) is not None
            and torch.backends.mps.is_available()
        ):
            return "mps"
        return "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


def majority_baseline(test_rows: Sequence[dict]) -> tuple[str, float]:
    """Accuracy of always guessing the test slice's most common label.

    This is the honest floor a LoRA must beat. Reported beside exact-label
    accuracy so 'the model learned' is proven by exceeding it, never by 'it ran'.
    """
    if not test_rows:
        return "none", 0.0
    counts = Counter(r["label"] for r in test_rows)
    top_label, top_count = counts.most_common(1)[0]
    return top_label, top_count / len(test_rows)


def exact_label_accuracy(gold: Sequence[dict], predicted: Sequence[str]) -> float:
    """Fraction of test rows whose single predicted token equals the gold label."""
    if not gold:
        return 0.0
    hits = sum(1 for g, p in zip(gold, predicted, strict=True) if g["label"] == p)
    return hits / len(gold)


def export_metrics(out_dir: Path, metrics: dict) -> Path:
    """Write metrics.json into out_dir; return the path. Real, tested."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "metrics.json"
    path.write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------- #
# The fit — REAL or RAISES, never fabricated.                                  #
# --------------------------------------------------------------------------- #


def _missing_fit_libs() -> list[str]:
    return [m for m in ("torch", "transformers", "peft") if not _probe(m)]


# --------------------------------------------------------------------------- #
# The fit — REAL or RAISES, never fabricated.                                  #
# --------------------------------------------------------------------------- #


def _label_id(label: str) -> int:
    return LABELS.index(label)


def _hidden_dim(model) -> int:
    """Resolve the hidden size from the base model's config, model-agnostic."""
    cfg = getattr(model, "config", None)
    for key in ("hidden_size", "d_model", "n_embd", "hidden_dim"):
        if cfg is not None and hasattr(cfg, key):
            return int(getattr(cfg, key))
    # Last-resort: peek the first decoder block's output shape is unreliable;
    # refuse rather than guess.
    raise RuntimeError("cannot resolve hidden dim for %r" % type(model).__name__)


def _last_token_hidden(model_output, input_ids):
    """Last-token hidden state (position of each row's final non-pad token).

    model(**inputs) returns a ModellingOutput whose `.hidden_states` is a tuple of
    per-layer sequences (batch, seq, hidden); take the top layer at each row's last
    real token so the classifier head sees the whole row's summary.
    """
    import torch  # noqa: F811 - lazy; module import stays dependency-light

    hs = model_output.hidden_states
    top = hs[-1]  # (batch, seq, hidden)
    lengths = (input_ids != 0).sum(dim=1) - 1  # index of last real token (padded=0)
    batch = torch.arange(top.size(0), device=top.device)
    return top[batch, lengths]


def _build_head_and_peft(base_model: str, device: str, quantize_4bit: bool):
    """Load tokenizer + base causal LM, wrap in LoRA, attach a linear classifier
    head over the label vocabulary. Returns (tokenizer, model, label_id_map).
    Runs only where torch/transformers/peft exist."""
    import torch  # noqa: F811
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import LoraConfig, get_peft_model

    dtype = torch.float32
    load_kw = {}
    if quantize_4bit:
        try:
            import bitsandbytes as bnb  # noqa: F401
        except Exception:  # noqa: BLE001
            quantize_4bit = False  # 4-bit unavailable -> honest fallback to plain LoRA
        if quantize_4bit:
            from transformers import BitsAndBytesConfig

            load_kw["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_compute_dtype=dtype
            )

    tokenizer = AutoTokenizer.from_pretrained(base_model, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(base_model, **load_kw)
    model.to(device)
    model.config.use_cache = False

    lora_cfg = LoraConfig(
        r=8,
        lora_alpha=16,
        lora_dropout=0.1,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_cfg)
    model.train()
    return tokenizer, model, quantize_4bit


def _tokenize(batch: Sequence[dict], tokenizer, max_length: int, device: str):
    """Pad+truncate prompts to a fixed tensor batch on device."""
    enc = tokenizer(
        [b["prompt"] for b in batch],
        padding=True,
        truncation=True,
        max_length=max_length,
        return_tensors="pt",
    )
    return enc.input_ids.to(device), enc.attention_mask.to(device)


def train(
    train_rows: list[dict],
    test_rows: list[dict],
    *,
    base_model: str = "Qwen/Qwen2.5-0.5B-Instruct",
    out_dir: Path = Path("foundry/artifact"),
    epochs: int = 3,
    device: str | None = None,
    quantize_4bit: bool = False,
    hf_token_file: Path | None = None,
) -> dict:
    """Fit a LoRA classifier for the closed secret-label vocabulary.

    REAL or RAISES: on a host without torch/transformers/peft it fails closed with
    a plain reason (nothing pretends success). On a host with them it trains a
    small linear classifier head over the base LM's last-token hidden state,
    evaluates exact-label accuracy on the held-out test rows BESIDE the majority
    baseline, saves the LoRA adapter + metrics.json, and pushes to the HF hub only
    if a mounted secret file (hf_token_file) is present (§6)."""
    missing = _missing_fit_libs()
    if missing:
        raise RuntimeError(
            "cannot fit on this host: missing %s (no GPU here; run on HF free GPU "
            "per ORDER.md §6, or a CPU box with torch+transformers+peft installed "
            "for the CPU fallback)." % ", ".join(missing)
        )
    import torch  # noqa: F811 - the fit body only runs where torch is present
    from torch import nn

    device = device or pick_device()
    if device == "host-without-torch":
        device = "cpu"

    # Build probe dataset by tokenizing WITHOUT a model to fail fast on format.
    tokenizer, model, used_4bit = _build_head_and_peft(
        base_model, device, quantize_4bit
    )
    hidden = _hidden_dim(model)

    label_head = nn.Linear(hidden, len(LABELS)).to(device)
    opt = torch.optim.AdamW(
        list(model.parameters()) + list(label_head.parameters()),
        lr=2e-4,
        weight_decay=0.01,
    )

    train_ex = build_examples(train_rows)
    test_ex = build_examples(test_rows)
    gold_labels = [r["label"] for r in test_rows]

    def batches(examples):
        bs = 8
        for i in range(0, len(examples), bs):
            yield examples[i : i + bs]

    ce = nn.CrossEntropyLoss()
    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        n = 0
        for chunk in batches(train_ex):
            input_ids, attn = _tokenize(chunk, tokenizer, max_length=256, device=device)
            out = model(
                input_ids=input_ids, attention_mask=attn, output_hidden_states=True
            )
            h = _last_token_hidden(out, input_ids)
            logits = label_head(h)
            gold = torch.tensor(
                [_label_id(e["completion"]) for e in chunk], device=device
            )
            loss = ce(logits, gold)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += loss.item() * len(chunk)
            n += len(chunk)
        # print every epoch so a Spaces log shows a live number, not silence
        print(f"epoch {epoch + 1}/{epochs}: loss={total_loss / max(n, 1):.4f}")

    # --- evaluation ---
    model.eval()
    predicted: list[str] = []
    with torch.no_grad():
        for chunk in batches(test_ex):
            input_ids, attn = _tokenize(chunk, tokenizer, max_length=256, device=device)
            out = model(
                input_ids=input_ids, attention_mask=attn, output_hidden_states=True
            )
            h = _last_token_hidden(out, input_ids)
            argmax = label_head(h).argmax(dim=1).tolist()
            predicted.extend(LABELS[i] for i in argmax)

    floor_label, floor_acc = majority_baseline(test_rows)
    acc = exact_label_accuracy([{"label": g} for g in gold_labels], predicted)

    metrics = {
        "base_model": base_model,
        "device": device,
        "epochs": epochs,
        "quantize_4bit": used_4bit,
        "n_train": len(train_ex),
        "n_test": len(test_ex),
        "dataset_seed": 7,
        "majority_baseline_label": floor_label,
        "majority_baseline_accuracy": floor_acc,
        "exact_label_accuracy": acc,
        "adapter_path": str(out_dir),
        "push": bool(hf_token_file is not None),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out_dir))
    # The classifier head carries no PeFT/LoRA state (it is a plain Linear above
    # the last-token hidden state). Persist it beside the adapter so the saved
    # artifact is loadable END TO END — otherwise reloading the adapter alone
    # could not reproduce the classifier, which a buyer's engineer would catch.
    torch.save(label_head.state_dict(), str(out_dir / "classifier_head.pt"))
    metrics["classifier_head"] = "classifier_head.pt"
    export_metrics(out_dir, metrics)

    if hf_token_file is not None:
        if not hf_token_file.exists():
            raise RuntimeError(
                "HF_TOKEN_FILE points at %r but the file is absent; per §6 refuse "
                "to push without the mounted secret." % hf_token_file
            )
        token = hf_token_file.read_text(encoding="utf-8").strip()
        try:
            from huggingface_hub import HfApi

            HfApi(token=token).upload_folder(
                folder_path=str(out_dir),
                repo_id=metrics["adapter_path"],
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "push to HF hub failed (local adapter saved): %s" % e
            ) from e

    return metrics


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _load_rows(data: Path) -> list[dict]:
    with data.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Train the secret-detector LoRA (ORDER.md §6/§7)"
    )
    ap.add_argument("--data", default="foundry/secret-train.jsonl")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--base-model", default="Qwen/Qwen2.5-0.5B-Instruct")
    ap.add_argument("--out", type=Path, default=Path("foundry/artifact"))
    ap.add_argument("--device", default=None, help="cuda|mps|cpu (default: auto)")
    ap.add_argument("--quantize-4bit", action="store_true")
    ap.add_argument(
        "--hf-token-file",
        default=os.environ.get("HF_TOKEN_FILE"),
        help="mounted secret file (§6); push only when present",
    )
    args = ap.parse_args()

    rows = _load_rows(Path(args.data))
    train_rows, test_rows = split(rows, seed=args.seed)
    print(f"split: train={len(train_rows)} test={len(test_rows)}")
    print("device: " + pick_device())

    try:
        metrics = train(
            train_rows,
            test_rows,
            base_model=args.base_model,
            out_dir=args.out,
            epochs=args.epochs,
            device=args.device,
            quantize_4bit=args.quantize_4bit,
            hf_token_file=Path(args.hf_token_file) if args.hf_token_file else None,
        )
    except (RuntimeError, NotImplementedError) as e:
        # Fail loudly — never a silent no-op disguised as success.
        print(f"train aborted: {e}", file=os.sys.stderr)
        raise SystemExit(2) from e
    print(json.dumps(metrics, indent=2))
    print("written: " + str(export_metrics(args.out, metrics)))


if __name__ == "__main__":
    main()
