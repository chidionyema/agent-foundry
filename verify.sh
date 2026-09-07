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

echo "agent-foundry VERIFIED"
