"""Foundry spine tests — pin the GPU-free, deterministic contract (ORDER.md §6/§7).

What is gradeable HERE, without HF libs or a GPU: generator determinism + class
integrity + label balance; the train/test split stratification; the honest grading
helpers (majority baseline, exact-label accuracy, metric export); and the train()'
FAIL-CLOSED behaviour on a host that cannot fit (it must refuse, never fabricate a
trained result). The real fit body is not testable on this laptop — that belongs on
the exec host (§6); these tests prove the parts that must hold regardless.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry import foundry_trainer as ft
from foundry.generator import LABELS, generate


# --------------------------------------------------------------------------- #
# generator                                                                   #
# --------------------------------------------------------------------------- #


def test_generate_is_deterministic_same_seed():
    a = generate(seed=7, n_per_class=50)
    b = generate(seed=7, n_per_class=50)
    assert a == b


def test_generate_differs_across_seeds():
    a = generate(seed=1, n_per_class=30)
    b = generate(seed=2, n_per_class=30)
    assert a != b


def test_generate_is_class_balanced():
    rows = generate(seed=7, n_per_class=40)
    from collections import Counter

    counts = Counter(r["label"] for r in rows)
    assert set(counts) == set(LABELS)
    assert len({c for c in counts.values()}) == 1, counts  # every class equal mass


def test_none_rows_carry_no_secret_shape():
    """A 'none' row must contain none of the detector's own secret signatures."""
    rows = generate(seed=7, n_per_class=25)
    none_rows = [r for r in rows if r["label"] == "none"]
    assert none_rows
    for r in none_rows:
        assert "AKIA" not in r["text"]
        assert "ghp_" not in r["text"]
        assert "AIza" not in r["text"]
        assert "xoxb-" not in r["text"]


def test_secret_class_rows_embed_only_their_own_shape():
    """A row labeled aws_access_key embeds AKIA…, github_pat embeds ghp_, etc."""
    rows = generate(seed=7, n_per_class=25)
    sig = {
        "aws_access_key": "AKIA",
        "github_pat": "ghp_",
        "google_api": "AIza",
        "slack_token": "xoxb-",
    }
    for label, marker in sig.items():
        for r in rows:
            if r["label"] == label:
                assert marker in r["text"], f"{label} row missing {marker}: {r['text']}"


def test_generator_main_writes_jsonl(tmp_path):
    out = tmp_path / "data.jsonl"
    from foundry.generator import main as gen_main
    import sys

    sys.argv = ["generator.py", "--seed", "3", "--out", str(out), "--per-class", "10"]
    gen_main()
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(LABELS) * 10


# --------------------------------------------------------------------------- #
# trainer pure helpers                                                        #
# --------------------------------------------------------------------------- #


def _corpus(n_per_class: int = 30) -> list[dict]:
    return generate(seed=7, n_per_class=n_per_class)


def test_build_examples_has_prompt_and_completion_each_row():
    rows = _corpus(n_per_class=5)
    ex = ft.build_examples(rows)
    assert len(ex) == len(rows)
    for e, r in zip(ex, rows, strict=True):
        assert r["label"] in e["completion"]
        assert r["text"] in e["prompt"]


def test_split_stratifies_every_class_both_slices():
    rows = _corpus(n_per_class=60)
    train, test = ft.split(rows, seed=7, test_frac=0.2)
    tr_lab = {r["label"] for r in train}
    te_lab = {r["label"] for r in test}
    assert tr_lab == set(LABELS)
    assert te_lab == set(LABELS)
    # deterministic: same seed => identical split
    t2, e2 = ft.split(rows, seed=7, test_frac=0.2)
    assert train == t2 and test == e2


def test_majority_baseline_is_actual_most_common_label():
    rows = _corpus(n_per_class=40)
    _, test = ft.split(rows)
    # force an imbalance so the floor is unambiguous
    lopsided = test + [{"text": "x", "label": "none"} for _ in range(90)]
    label, acc = ft.majority_baseline(lopsided)
    assert label == "none"
    n_none = sum(1 for r in lopsided if r["label"] == "none")
    assert acc == pytest.approx(n_none / len(lopsided))


def test_exact_label_accuracy_counts_matches():
    gold = [
        {"text": "a", "label": "none"},
        {"text": "b", "label": "aws_access_key"},
        {"text": "c", "label": "none"},
    ]
    assert ft.exact_label_accuracy(
        gold, ["none", "aws_access_key", "none"]
    ) == pytest.approx(1.0)
    # predicted all-'none': position 0 and 2 match gold, position 1 does not
    assert ft.exact_label_accuracy(gold, ["none", "none", "none"]) == pytest.approx(
        2 / 3
    )


def test_export_metrics_writes_json(tmp_path):
    p = ft.export_metrics(tmp_path, {"a": 1})
    assert p.name == "metrics.json"
    import json

    assert json.loads(p.read_text())["a"] == 1


# --------------------------------------------------------------------------- #
# honest fail-closed: no fake training on a GPU-less host                     #
# --------------------------------------------------------------------------- #


def test_train_never_fabricates_on_host_without_hf_libs():
    """train() MUST fail closed (RuntimeError) here; it may never return a
    'trained' dict it did not actually fit, and must name the missing libs so the
    exec-host (HF Space §6) is the clear, plain next step."""
    rows = _corpus(n_per_class=5)
    train, test = ft.split(rows)
    with pytest.raises(RuntimeError) as exc:
        ft.train(train, test, out_dir=Path("unused"))
    msg = str(exc.value)
    # the failure is honest and actionable: names absent libs + where to run
    assert "HF free GPU" in msg or "missing" in msg


def test_pick_device_returns_expected_values_without_gpu():
    """Resolution logic returns a sane string and never crashes without torch."""
    d = ft.pick_device()
    assert d in {"host-without-torch", "cuda", "mps", "cpu"}
