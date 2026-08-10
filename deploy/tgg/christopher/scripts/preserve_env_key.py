#!/usr/bin/env python3
"""Carry one live env key into a staged replacement file."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _key_lines(text: str, key: str) -> list[str]:
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=")
    return [line for line in text.splitlines() if pattern.match(line)]


def preserve_env_key(
    current: Path,
    staged: Path,
    key: str,
    *,
    allow_staged_fallback: bool = False,
) -> None:
    staged_text = staged.read_text(encoding="utf-8")
    staged_matches = _key_lines(staged_text, key)
    if len(staged_matches) > 1:
        raise RuntimeError(f"staged env contains duplicate {key}")
    if staged_matches and not allow_staged_fallback:
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
    if staged_matches:
        staged_lines = staged_text.splitlines()
        staged_lines[staged_lines.index(staged_matches[0])] = preserved
        staged.write_text("\n".join(staged_lines) + "\n", encoding="utf-8")
        return
    staged.write_text(
        staged_text.rstrip("\n") + "\n" + preserved + "\n",
        encoding="utf-8",
    )


def main() -> int:
    if len(sys.argv) not in (4, 5):
        raise SystemExit(
            "usage: preserve_env_key.py CURRENT STAGED KEY [--allow-staged-fallback]"
        )
    allow_staged_fallback = len(sys.argv) == 5
    if allow_staged_fallback and sys.argv[4] != "--allow-staged-fallback":
        raise SystemExit(f"unknown argument: {sys.argv[4]}")
    preserve_env_key(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        sys.argv[3],
        allow_staged_fallback=allow_staged_fallback,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
