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
    """Fit the LoRA on the exec host, export the adapter + an honest metric.

    Fails closed (RuntimeError) with a plain reason when this host cannot fit —
    absent torch/transformers/peft (this laptop) — so nothing pretends success.
    """
    missing = _missing_fit_libs()
    if missing:
        raise RuntimeError(
            "cannot fit on this host: missing %s (no GPU here; run on HF free GPU "
            "per ORDER.md §6, or a CPU box with torch+transformers+peft installed "
            "for the CPU fallback)." % ", ".join(missing)
        )
    # The fit body is intentionally NOT YET WRITTEN. Rather than ship a stub that
    # claims a trained artifact it never produced, train() fails closed until the
    # exec host (HF free GPU, §6) is reached and the real body is verified against
    # the pure-Python contract above (split, baseline, exact_label_accuracy,
    # export_metrics). Env vars/base/out are all present for it; nothing here lies.
    raise NotImplementedError(
        "train() fit body not yet implemented on an exec host; refusing to fabricate "
        "a result. The pure-Python contract (split, baseline, export) is real and "
        "green in tests."
    )


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
