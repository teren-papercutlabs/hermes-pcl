#!/usr/bin/env python3
"""Verify an agent-local Codex OAuth credential without printing secrets."""
from __future__ import annotations
import argparse, json, os, pwd, stat
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--credential-label", required=True)
    parser.add_argument("--service-user", default="pclaw")
    args = parser.parse_args()
    home = Path(args.hermes_home).resolve()
    if Path(os.environ.get("HERMES_HOME", "")).resolve() != home:
        raise RuntimeError("HERMES_HOME must name the verified agent-local home")
    auth_path = home / "auth.json"
    details = auth_path.stat()
    if details.st_uid != pwd.getpwnam(args.service_user).pw_uid or stat.S_IMODE(details.st_mode) != 0o600:
        raise RuntimeError("auth.json must be owned by the service user with mode 0600")
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")
    matches = [e for e in pool.entries() if e.label.strip().lower() == args.credential_label.strip().lower()]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one Codex credential labeled {args.credential_label!r}")
    entry = pool.select_label(args.credential_label)
    if entry is None or not (getattr(entry, "access_token", None) or getattr(entry, "runtime_api_key", None)) or not getattr(entry, "refresh_token", None):
        raise RuntimeError("selected Codex OAuth credential is unavailable or incomplete")
    print(json.dumps({"ok": True, "provider": "openai-codex", "credential_label": entry.label,
                      "auth_store": str(auth_path), "auth_store_owner": args.service_user,
                      "auth_store_mode": "0600", "has_access_token": True,
                      "has_refresh_token": True}, sort_keys=True))
    return 0
if __name__ == "__main__": raise SystemExit(main())
