"""Shared host/runtime-visible paths for python_sandbox datasets."""

from pathlib import Path, PurePosixPath
from typing import Any, Mapping


PYTHON_SANDBOX_INPUTS_ROOT = PurePosixPath("/inputs")


def is_python_sandbox_dataset_name(name: str) -> bool:
    return bool(name) and all(
        char.isascii() and (char.isalnum() or char in "_-") for char in name
    )


def python_sandbox_dataset_path(name: str) -> PurePosixPath:
    """Return the path at which a configured dataset is visible in the jail."""
    if not is_python_sandbox_dataset_name(name):
        raise ValueError(f"invalid dataset name: {name!r}")
    return PYTHON_SANDBOX_INPUTS_ROOT / name


def host_path_to_python_sandbox_path(
    host_path: str | Path,
    config: Mapping[str, Any],
) -> PurePosixPath | None:
    """Translate a configured path-dataset member to its jail-visible path.

    Returns ``None`` unless *host_path* is inside an explicitly configured
    ``type: path`` dataset.  The lexical containment check deliberately avoids
    resolving either side: the sandbox mount code owns symlink validation, and
    message-context rendering must not turn an undeclared symlink target into a
    broader visible path.
    """
    datasets = config.get("datasets", {})
    if not isinstance(datasets, Mapping):
        return None
    candidate = Path(host_path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    for raw_name, raw_spec in datasets.items():
        name = str(raw_name)
        if not is_python_sandbox_dataset_name(name) or not isinstance(raw_spec, Mapping):
            continue
        raw_root = raw_spec.get("path")
        if raw_spec.get("type") != "path" or not isinstance(raw_root, str):
            continue
        root = Path(raw_root).expanduser()
        if not root.is_absolute() or ".." in root.parts:
            continue
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        return python_sandbox_dataset_path(name) / PurePosixPath(relative.as_posix())
    return None
