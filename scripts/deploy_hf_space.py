#!/usr/bin/env python3
"""deploy_hf_space.py — one-shot create + push for the Agent Foundry trainer Space.

Uses huggingface_hub to create a new Space (sdk=docker) and uploads the three
files from foundry/space/ (app.py, requirements.txt, README.md). The HF token
is read from a FILE path the operator passes in -- never a literal, never an env
var (ORDER.md \u00a76 policy-clean seam).

The script also writes a deploy-audit row to a JSONL sink using the same
Execution shape af/meter.py emits (LAW 50: every workload emits to the central
collector). It refuses dark if no audit sink is configured.

Usage:
    # 1) Put your HF Write token in a file you control:
    #       mkdir -p ~/.hf && umask 077 && nano ~/.hf/write_token
    #       (paste hf_xxxxxxxxxxxxxxxxx, save)
    #
    # 2) Run the script:
    #       python3 scripts/deploy_hf_space.py \\
    #           --space-name agent-foundry-trainer \\
    #           --token-file ~/.hf/write_token \\
    #           --audit-dir /var/log/agent-foundry/deploys

Examples:
    # Use your default HF account, default source dir:
    python3 scripts/deploy_hf_space.py --space-name agent-foundry-trainer \\
        --token-file ~/.hf/write_token --audit-dir /tmp/af-audit

    # Override everything explicitly (useful for clients/tenants):
    python3 scripts/deploy_hf_space.py \\
        --username their-org --space-name my-foundry \\
        --token-file /run/secrets/hf_write \\
        --source /opt/agent-foundry/foundry/space \\
        --visibility private --audit-dir /var/log/af-audit
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Ensure the project root is on sys.path so 'from af.meter import ...' resolves
# regardless of where this script is invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from af.meter import Execution  # reuse the platform's event shape (no format drift)

_REQUIRED_FILES = ("app.py", "requirements.txt", "README.md")


# --- central-collector audit post (LAW 50 / ORDER.md §6) -------------------------
def _post_audit(
    *,
    audit_dir: Path,
    username: str,
    space_name: str,
    visibility: str,
    repo_id: str,
) -> None:
    """Append one Execution row to <audit_dir>/deploy_audit.jsonl."""
    audit_dir.mkdir(parents=True, exist_ok=True)
    row = Execution(
        tenant_id=f"hf:{username}",  # tenancy = the HF owner; not a real tenant
        order_id=f"deploy-{space_name}",
        run_id=f"space-create-{int(time.time())}",
        agent_slug="foundry.hf_space_deploy",
        status="ran",
        started_at=time.time(),
        finished_at=time.time(),
        detail={
            "hf_repo_id": repo_id,
            "visibility": visibility,
            "space_name": space_name,
        },
    ).to_json()
    out = audit_dir / "deploy_audit.jsonl"
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Create + push the Agent Foundry trainer Space",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--username",
        default=None,
        help="HF username (owner of the Space). Default: auto-detect from the token.",
    )
    ap.add_argument(
        "--space-name",
        required=True,
        help="Space name (slug, must be unique on HF Hub)",
    )
    ap.add_argument(
        "--token-file",
        required=True,
        type=Path,
        help="Path to a file containing ONLY the HF Write token (mode 600)",
    )
    ap.add_argument(
        "--visibility",
        choices=("public", "private"),
        default="public",
        help="Space visibility (default: public)",
    )
    ap.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "Directory holding app.py, requirements.txt, README.md. "
            "Default: <repo>/foundry/space (auto-resolved from this script's location)."
        ),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print the plan without contacting HF.",
    )
    ap.add_argument(
        "--audit-dir",
        type=Path,
        default=None,
        help=(
            "Directory to append the deploy audit row to (JSONL). "
            "Default: $AF_DEPLOY_AUDIT_DIR if set. Refuses dark if neither is "
            "given (LAW 50: every workload emits to the central collector)."
        ),
    )
    args = ap.parse_args()

    # === LOCAL VALIDATION (no huggingface_hub required) ============================

    # --- Sanity: token file exists, mode <= 0o600, contains one non-empty line ---
    tok_path: Path = args.token_file.expanduser()
    if not tok_path.exists():
        raise SystemExit(f"FAIL: token file {tok_path} does not exist")
    mode = tok_path.stat().st_mode & 0o777
    if mode > 0o600:
        raise SystemExit(
            f"FAIL: token file mode is {oct(mode)} (must be <= 0o600). "
            f"Fix with: chmod 600 {tok_path}"
        )
    token = tok_path.read_text(encoding="utf-8").strip()
    if not token.startswith("hf_"):
        raise SystemExit(
            "FAIL: token file content does not start with hf_ -- not an HF token"
        )

    # --- Resolve source dir (default: <repo>/foundry/space relative to this script) ---
    if args.source is None:
        repo_root = Path(__file__).resolve().parent.parent
        args.source = repo_root / "foundry" / "space"
    src: Path = args.source.expanduser()
    missing = [n for n in _REQUIRED_FILES if not (src / n).exists()]
    if missing:
        raise SystemExit(f"FAIL: {src} missing: {missing}")

    # --- Resolve audit dir (refuse-dark per LAW 50) ---
    audit_dir = args.audit_dir or (
        Path(os.environ["AF_DEPLOY_AUDIT_DIR"])
        if os.environ.get("AF_DEPLOY_AUDIT_DIR")
        else None
    )
    if audit_dir is None:
        raise SystemExit(
            "FAIL: no audit sink configured. Pass --audit-dir <dir> or set "
            "$AF_DEPLOY_AUDIT_DIR. A deploy without an audit row is an unobserved "
            "workload (LAW 50)."
        )

    # --- Dry-run: print the plan, no HF calls, no audit row ---
    if args.dry_run:
        print("DRY RUN -- no calls to HF will be made")
        print(f"  token file       : {tok_path} (mode {oct(mode)})")
        print(f"  token prefix     : {token[:6]}...")
        print(f"  source directory : {src}")
        print(f"  files to upload  : {list(_REQUIRED_FILES)}")
        print(f"  username (raw)   : {args.username or '<auto-detect>'}")
        print(f"  space name       : {args.space_name}")
        print(f"  visibility       : {args.visibility}")
        print(f"  audit dir        : {audit_dir}")
        return

    # === HEAVY IMPORT + ACTUAL DEPLOY =============================================
    # Done last so --help + refuse-dark + dry-run all work on hosts without
    # huggingface_hub installed.
    from huggingface_hub import HfApi, whoami

    api = HfApi(token=token)
    me = whoami(token=token)
    username = args.username or me.get("name")
    if not username:
        raise SystemExit(
            "FAIL: could not auto-detect HF username from the token; pass --username."
        )
    print(f"authenticated as: {username}")

    repo_id = f"{username}/{args.space_name}"
    repo_type = "space"

    print(f"creating space {repo_id} (sdk=docker, {args.visibility})...")
    api.create_repo(
        repo_id=repo_id,
        repo_type=repo_type,
        space_sdk="docker",
        private=(args.visibility == "private"),
        exist_ok=True,
    )

    # --- Stage files in a temp dir, upload via API ---
    with tempfile.TemporaryDirectory(prefix="hf-space-") as tmp:
        tmp_path = Path(tmp)
        for fname in _REQUIRED_FILES:
            shutil.copy2(src / fname, tmp_path / fname)

        print("uploading files via API (no local git needed)...")
        api.upload_folder(
            folder_path=str(tmp_path),
            repo_id=repo_id,
            repo_type=repo_type,
            commit_message="agent-foundry trainer Space (initial)",
        )

    # --- Audit post (refuse-dark was already enforced above) ---
    _post_audit(
        audit_dir=audit_dir,
        username=username,
        space_name=args.space_name,
        visibility=args.visibility,
        repo_id=repo_id,
    )
    print(f"audit row appended to: {audit_dir / 'deploy_audit.jsonl'}")

    print(f"DONE: https://huggingface.co/spaces/{repo_id}")
    print(
        "next: open the Space page, add HF_TOKEN_FILE secret (Settings -> "
        "Variables and secrets), then watch the Logs tab."
    )


if __name__ == "__main__":
    main()
