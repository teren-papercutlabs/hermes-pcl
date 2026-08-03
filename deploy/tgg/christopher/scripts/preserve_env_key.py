#!/usr/bin/env python3
"""Carry one previously migrated env key into a staged replacement file."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _key_lines(text: str, key: str) -> list[str]:
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=")
    return [line for line in text.splitlines() if pattern.match(line)]


def preserve_env_key(current: Path, staged: Path, key: str) -> None:
    staged_text = staged.read_text(encoding="utf-8")
    if _key_lines(staged_text, key):
        raise RuntimeError(f"staged env unexpectedly contains {key}")

    if not current.is_file():
        return
    current_matches = _key_lines(current.read_text(encoding="utf-8"), key)
    if len(current_matches) > 1:
        raise RuntimeError(f"destination env contains duplicate {key}")
    if not current_matches:
        return
    preserved = current_matches[0]
    if not preserved.startswith(f"{key}="):
        raise RuntimeError(f"destination env contains non-canonical {key}")
    staged.write_text(
        staged_text.rstrip("\n") + "\n" + preserved + "\n",
        encoding="utf-8",
    )


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: preserve_env_key.py CURRENT STAGED KEY")
    preserve_env_key(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
