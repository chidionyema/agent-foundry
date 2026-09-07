#!/usr/bin/env python3
"""foundry/generator.py — synthesize the secret-detector training corpus.

Make-to-stock validation task (ORDER.md §6/§7): given a messy log line, classify
whether a secret is present and, if so, its type. This module creates the labeled
dataset the LoRA is trained and graded on.

Deterministic by design (an estate law): the same seed yields byte-identical
splits, so a rerun never silently changes what "correct" means and a test can pin
the exact dataset. No network, no GPU — pure Python, verifiable on this laptop.

Label vocabulary — the whole output space is a member of this set:
    {none, aws_access_key, aws_secret_key, github_pat, google_api, slack_token}
A 'none' line is a plausible noisy log with no secret embedded. Every other class
embeds exactly one secret of its type inside otherwise-identical noise, so the only
reliable signal the model can latch on to is the secret's *shape* (class), which is
exactly what a secret-detector should learn.

Usage:
    python3 foundry/generator.py --seed 7 --out foundry/secret-train.jsonl --per-class 500
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path
from typing import Iterable

# Closed vocabulary. The trainer maps these to single answer tokens and grades by
# exact match, never free text.
LABELS = (
    "none",
    "aws_access_key",
    "aws_secret_key",
    "github_pat",
    "google_api",
    "slack_token",
)

_AKIA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_ALNUM62 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_HEX = "0123456789abcdef"


def _aws_access_key(rng: random.Random) -> str:
    return "AKIA" + "".join(rng.choice(_AKIA) for _ in range(16))


def _aws_secret_key(rng: random.Random) -> str:
    return "".join(rng.choice(_ALNUM62 + "+/") for _ in range(40))


def _github_pat(rng: random.Random) -> str:
    return "ghp_" + "".join(rng.choice(_HEX) for _ in range(36))


def _google_api(rng: random.Random) -> str:
    return "AIza" + "".join(rng.choice(_ALNUM62 + "-_") for _ in range(35))


def _slack_token(rng: random.Random) -> str:
    parts = ("".join(rng.choice(_HEX) for _ in range(24)) for _ in range(3))
    return "xoxb-" + "-".join(parts)


_SECRET_FN = {
    "aws_access_key": _aws_access_key,
    "aws_secret_key": _aws_secret_key,
    "github_pat": _github_pat,
    "google_api": _google_api,
    "slack_token": _slack_token,
}


def _hex(rng: random.Random, n: int = 12) -> str:
    return "".join(rng.choice(_HEX) for _ in range(n))


def _rid(rng: random.Random) -> str:
    return _hex(rng, 8)


def _log_scaffold(rng: random.Random) -> str:
    """A contextual log prefix; never carries a secret. Keeps the model from
    shortcutting on formatting rather than on secret shape."""
    prefixes = (
        "ERROR gateway/node-{a} upstream={ip}:{port} TLS={proto} code={code} rid={rid}",
        "WARN {svc}-worker-{n} kubelet checksum={c1} status=degraded retry={r}",
        "DEBUG trace={c1} span={svc} attempt {r} region=us-east-1 rid={rid}",
        "INFO 2026-09-07T{hh:02d}:{mm:02d}:{ss:02d}Z pod/{name} [{svc}] rid={rid}",
        "NOTICE {svc} metric=latency value={ms}ms instance={name} tid={rid}",
    )
    hostish = rng.choice(("ip-10-0-{a}.{b}", "node-{a}.cluster.local", "k8s-pod-{a}-x"))
    name = hostish.format(a=rng.randrange(256), b=rng.randrange(256))
    return rng.choice(prefixes).format(
        a=rng.randrange(256),
        ip=f"10.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(2, 255)}",
        port=rng.randrange(1024, 65535),
        proto=rng.choice(("1.2", "1.3")),
        code=rng.choice(("403", "500", "503")),
        rid=_rid(rng),
        svc=rng.choice(
            ("api", "auth", "billing", "sync", "ingest", "worker", "registry")
        ),
        n=rng.randrange(20),
        c1=_hex(rng),
        r=rng.randrange(5),
        hh=rng.randrange(24),
        mm=rng.randrange(60),
        ss=rng.randrange(60),
        name=name,
        ms=rng.randrange(5, 9000),
    )


# Ways a secret provably appears in a real log line. Each is templated with a
# {secret}; "(rng)" placers also carry a random trailing red herring token so the
# secret is never cleanly at a statement boundary.
_PLACERS = (
    "env={secret}",
    "--set {kw}={secret}",
    'payload="{secret}"',
    "key {secret} in vault",
    "value={secret} sha256={c1}",
)


def _embed(rng: random.Random, secret: str) -> str:
    red = _hex(rng, 6)
    p = rng.choice(_PLACERS)
    # sha256 placers and env placers carry the red herring too; kw-style keep it.
    glue = p.format(secret=secret, kw=secret, c1=red)
    return glue + f" pad={red}"


def _noise_line(rng: random.Random, label: str) -> str:
    """Deterministic construction of ONE labeled row's text."""
    scaffold = _log_scaffold(rng)
    if label == "none":
        return scaffold
    secret = _SECRET_FN[label](rng)
    return f"{scaffold} {_embed(rng, secret)}"


def generate(seed: int = 7, n_per_class: int = 400) -> list[dict]:
    """Deterministic, class-balanced dataset. One row {text,label} per draw.

    Balanced on purpose so the majority-class baseline is 1/len(LABELS); beating
    it proves real secret-shape learning.
    """
    rng = random.Random(seed)  # noqa: S311 - seed-determinism is a requirement (estate law), never crypto
    rows: list[dict] = []
    for label in LABELS:
        for _ in range(n_per_class):
            rows.append({"text": _noise_line(rng, label), "label": label})
    rng.shuffle(rows)  # deterministic by construction (same RNG stream)
    return rows


def write_jsonl(rows: Iterable[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthesize the secret-detector corpus")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="foundry/secret-train.jsonl")
    ap.add_argument("--per-class", type=int, default=400)
    args = ap.parse_args()
    rows = generate(seed=args.seed, n_per_class=args.per_class)
    write_jsonl(rows, Path(args.out))
    print(
        "wrote %d rows -> %s  labels=%s"
        % (len(rows), args.out, dict(Counter(r["label"] for r in rows)))
    )


if __name__ == "__main__":
    main()
