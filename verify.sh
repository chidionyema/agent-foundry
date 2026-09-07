#!/usr/bin/env bash
# Verify agent-foundry: compile, unit tests, and end-to-end headless convergence
# proof that an order converges and meters. Exit 0 is the ONLY definition of done.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python"
if [ ! -x "${PY}" ]; then
	python3 -m venv .venv
	./.venv/bin/pip install --quiet --upgrade pip
	./.venv/bin/pip install --quiet nats-py jsonschema beautifulsoup4 pytest
fi

echo "== unit tests =="
"${PY}" -m pytest -q -p no:cacheprovider

echo "== headless convergence + metering proof =="
DEMO_METER="$(mktemp -d)"
trap 'rm -rf "${DEMO_METER}"' EXIT
"${PY}" -m af.cli validate examples/order.price-alert.json
"${PY}" -m af.cli run examples/order.price-alert.json \
	--seed-html examples/seed.competitor.html \
	--meter-dir "${DEMO_METER}" >/dev/null
ROWS="$(wc -l <"${DEMO_METER}/executions.jsonl" | tr -d '[:space:]')"
echo "metering rows: ${ROWS}"
test "${ROWS}" = "5" || {
	echo "expected 5 metering rows"
	exit 1
}

echo "== deploy_hf_space.py: local-validation gates (no HF required) =="
# These prove the script refuses dark + runs dry-run without importing
# huggingface_hub (matches the pattern in foundry/foundry_trainer.py where heavy
# libs are imported only when actually needed).
FAKE_TOK="$(mktemp)"
printf 'hf_fake_token_for_test_only\n' >"${FAKE_TOK}"
chmod 600 "${FAKE_TOK}"
FAKE_AUDIT="$(mktemp -d)"

# 1) refuse-dark when no audit sink configured (exits non-zero, audit msg present)
set +e
OUT="$("${PY}" scripts/deploy_hf_space.py --space-name test --token-file "${FAKE_TOK}" 2>&1)"
RC=$?
set -e
test "${RC}" -ne 0 || {
	echo "expected refuse-dark to exit non-zero"
	exit 1
}
echo "${OUT}" | grep -q 'no audit sink configured' || {
	echo 'expected refuse-dark message'
	exit 1
}

# 2) --help works without huggingface_hub on sys.path (exit 0)
"${PY}" scripts/deploy_hf_space.py --help >/dev/null

# 3) dry-run prints the plan, never contacts HF (exit 0)
OUT="$("${PY}" scripts/deploy_hf_space.py \
	--space-name agent-foundry-trainer \
	--token-file "${FAKE_TOK}" \
	--dry-run --audit-dir "${FAKE_AUDIT}" 2>&1)"
echo "${OUT}" | grep -q 'DRY RUN' || {
	echo 'expected DRY RUN plan'
	exit 1
}
test ! -f "${FAKE_AUDIT}/deploy_audit.jsonl" || {
	echo 'dry-run must not write audit row'
	exit 1
}

rm -f "${FAKE_TOK}"
rm -rf "${FAKE_AUDIT}"

echo "agent-foundry VERIFIED"
