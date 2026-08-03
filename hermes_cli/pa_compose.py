"""Deploy-time compiler for typed PA constitution sources."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

TYPED_SOURCE_DIRS = ("rules", "compliance", "reference", "templates", "job-briefs")
HEADER_START = "# pa-source:"
HEADER_END = "# ---"
REQUIRED_PROVENANCE = ("approved_by", "approved_date", "ruling_ref", "status")
VALID_STATUSES = {"approved", "pending", "unverified"}


class PaComposeError(ValueError):
    """Raised when typed PA sources cannot be composed safely."""


@dataclass(frozen=True)
class SourceArtifact:
    path: Path
    relative_path: str
    source_type: str
    sequence: int
    provenance: dict[str, Any]
    raw: bytes
    body: bytes

    @property
    def included_in_constitution(self) -> bool:
        return self.provenance.get("compose", True) is not False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_empty_provenance(value: Any) -> bool:
    """A provenance value must carry a real claim, never null or a null-bearing list.

    ``approved_date: [null]`` reads as an approval in every downstream manifest while
    recording nothing that can be traced, so it fails closed here rather than being
    promoted by implication.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return not value or any(_is_empty_provenance(item) for item in value)
    return False


def _parse_artifact(path: Path, root: Path, source_type: str) -> SourceArtifact:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PaComposeError(f"{path}: source must be UTF-8") from exc

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != HEADER_START:
        raise PaComposeError(f"{path}: missing '{HEADER_START}' provenance header")

    metadata_lines: list[str] = []
    body_start = None
    for index, line in enumerate(lines[1:], start=1):
        stripped = line.rstrip("\r\n")
        if stripped == HEADER_END:
            body_start = index + 1
            break
        if not stripped.startswith("# "):
            raise PaComposeError(f"{path}: malformed provenance header line {index + 1}")
        metadata_lines.append(stripped[2:])
    if body_start is None:
        raise PaComposeError(f"{path}: missing '{HEADER_END}' provenance terminator")

    try:
        provenance = yaml.safe_load("\n".join(metadata_lines)) or {}
    except yaml.YAMLError as exc:
        raise PaComposeError(f"{path}: invalid provenance YAML: {exc}") from exc
    if not isinstance(provenance, dict):
        raise PaComposeError(f"{path}: provenance header must be a mapping")

    missing = [key for key in REQUIRED_PROVENANCE if key not in provenance]
    if missing:
        raise PaComposeError(f"{path}: missing provenance fields: {', '.join(missing)}")
    empty = [key for key in REQUIRED_PROVENANCE if _is_empty_provenance(provenance[key])]
    if empty:
        raise PaComposeError(
            f"{path}: provenance fields must be non-null and non-empty: {', '.join(empty)}"
        )
    status = provenance["status"]
    if status not in VALID_STATUSES:
        raise PaComposeError(
            f"{path}: status must be one of {sorted(VALID_STATUSES)}, got {status!r}"
        )
    sequence = provenance.get("sequence")
    if not isinstance(sequence, int) or sequence < 0:
        raise PaComposeError(f"{path}: sequence must be a non-negative integer")

    body = "".join(lines[body_start:]).encode("utf-8")
    if not body:
        raise PaComposeError(f"{path}: source body is empty")

    return SourceArtifact(
        path=path,
        relative_path=path.relative_to(root).as_posix(),
        source_type=source_type,
        sequence=sequence,
        provenance=provenance,
        raw=raw,
        body=body,
    )


def load_typed_sources(source_dir: Path | str) -> list[SourceArtifact]:
    """Load and validate all typed source artifacts in deterministic order."""
    root = Path(source_dir).expanduser().resolve()
    if not root.is_dir():
        raise PaComposeError(f"source directory does not exist: {root}")

    artifacts: list[SourceArtifact] = []
    for source_type in TYPED_SOURCE_DIRS:
        typed_dir = root / source_type
        if not typed_dir.is_dir():
            raise PaComposeError(f"missing typed source directory: {typed_dir}")
        paths = sorted(p for p in typed_dir.rglob("*.yaml") if p.is_file())
        if not paths:
            raise PaComposeError(f"typed source directory has no YAML artifacts: {typed_dir}")
        artifacts.extend(_parse_artifact(path, root, source_type) for path in paths)

    sequences: dict[int, str] = {}
    for artifact in artifacts:
        if artifact.sequence in sequences:
            raise PaComposeError(
                f"duplicate sequence {artifact.sequence}: "
                f"{sequences[artifact.sequence]} and {artifact.relative_path}"
            )
        sequences[artifact.sequence] = artifact.relative_path
    return sorted(artifacts, key=lambda item: (item.sequence, item.relative_path))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def compose_pa_constitution(
    source_dir: Path | str,
    output_path: Path | str,
    manifest_path: Path | str,
    *,
    allow_unverified: bool = False,
) -> dict[str, Any]:
    """Compose typed sources, validate YAML, and atomically write output + manifest."""
    root = Path(source_dir).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    manifest = Path(manifest_path).expanduser().resolve()
    if output == manifest:
        raise PaComposeError("output and manifest paths must be different")

    artifacts = load_typed_sources(root)
    unverified_compliance = [
        item.relative_path
        for item in artifacts
        if item.source_type == "compliance" and item.provenance["status"] == "unverified"
    ]
    if unverified_compliance and not allow_unverified:
        joined = ", ".join(unverified_compliance)
        raise PaComposeError(
            "unverified compliance artifacts refuse composition by default: "
            f"{joined}; pass --allow-unverified to record and accept this escape"
        )

    composed_artifacts = [item for item in artifacts if item.included_in_constitution]
    composed = b"".join(item.body for item in composed_artifacts)
    try:
        parsed = yaml.safe_load(composed.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PaComposeError(f"composed constitution is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PaComposeError("composed constitution must be a YAML mapping")

    entries = [
        {
            "path": item.relative_path,
            "type": item.source_type,
            "sequence": item.sequence,
            "status": item.provenance["status"],
            "approved_by": item.provenance["approved_by"],
            "approved_date": item.provenance["approved_date"],
            "ruling_ref": item.provenance["ruling_ref"],
            "source_sha256": _sha256(item.raw),
            "body_sha256": _sha256(item.body),
            "included_in_constitution": item.included_in_constitution,
        }
        for item in artifacts
    ]
    digest_input = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    manifest_data: dict[str, Any] = {
        "schema_version": 1,
        "source_root": root.name,
        "allow_unverified": allow_unverified,
        "unverified_compliance": unverified_compliance,
        "source_count": len(entries),
        "composed_source_count": len(composed_artifacts),
        "sources_sha256": _sha256(digest_input),
        "constitution_sha256": _sha256(composed),
        "sources": entries,
    }
    manifest_bytes = (json.dumps(manifest_data, indent=2, sort_keys=True) + "\n").encode()

    _atomic_write(output, composed)
    _atomic_write(manifest, manifest_bytes)
    return manifest_data


def sync_pa_knowledge(
    source_dir: Path | str,
    constitution_path: Path | str,
    target_dir: Path | str,
    manifest_path: Path | str,
) -> dict[str, Any]:
    """Copy only constitution-declared knowledge into a runtime knowledge root."""
    from agent.pa_constitution import load_constitution

    source = Path(source_dir).expanduser().resolve()
    target = Path(target_dir).expanduser().resolve()
    manifest = Path(manifest_path).expanduser().resolve()
    try:
        constitution = load_constitution(constitution_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise PaComposeError(f"cannot load constitution: {exc}") from exc
    model_visible = {
        entry for brief in constitution.job_briefs.values() for entry in brief.knowledge
    }
    runtime_only = {
        str(entry)
        for brief in constitution.job_briefs.values()
        for entry in (
            (
                brief.response_policy.get("output_assembly", {}).get("artifacts", ())
                if isinstance(brief.response_policy.get("output_assembly"), dict)
                else ()
            )
        )
    }
    # Case-record config is runtime-only too: the required-field set and the
    # disclaimer-selection mapping are read by the runtime, never rendered to
    # the model, so they must be synced without appearing in the manifest the
    # model sees.
    for brief in constitution.job_briefs.values():
        case_record = brief.response_policy.get("case_record")
        if not isinstance(case_record, dict):
            continue
        for key in ("field_sets", "disclaimer_selection"):
            entry = case_record.get(key)
            if entry:
                runtime_only.add(str(entry))
    declared = sorted(model_visible | runtime_only)
    if not declared:
        raise PaComposeError("constitution declares no knowledge entries")

    copied: list[dict[str, Any]] = []
    for entry in declared:
        relative = Path(entry)
        if relative.is_absolute() or ".." in relative.parts:
            raise PaComposeError(f"knowledge entry must be relative and cannot traverse: {entry}")
        source_candidates = [source / relative, source / "knowledge" / relative]
        existing = [candidate.resolve() for candidate in source_candidates if candidate.is_file()]
        if len(existing) > 1:
            raise PaComposeError(f"declared knowledge entry is ambiguous in source: {entry}")
        if not existing:
            raise PaComposeError(f"declared knowledge entry is missing: {entry}")
        source_path = existing[0]
        try:
            source_path.relative_to(source)
        except ValueError as exc:
            raise PaComposeError(f"knowledge entry escapes source directory: {entry}") from exc
        target_path = (target / relative).resolve()
        try:
            target_path.relative_to(target)
        except ValueError as exc:
            raise PaComposeError(f"knowledge entry escapes target directory: {entry}") from exc
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        copied.append(
            {
                "path": entry,
                "bytes": source_path.stat().st_size,
                "sha256": _sha256(source_path.read_bytes()),
                "visibility": "model" if entry in model_visible else "runtime-only",
            }
        )

    result = {
        "schema_version": 1,
        "source_root": source.name,
        "target_root": target.name,
        "file_count": len(copied),
        "files": copied,
    }
    _atomic_write(
        manifest,
        (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return result


def cmd_pa(args: Any) -> None:
    """Dispatch ``hermes pa`` commands."""
    command = getattr(args, "pa_command", None)
    try:
        if command == "compose":
            result = compose_pa_constitution(
                args.source_dir,
                args.output,
                args.manifest,
                allow_unverified=args.allow_unverified,
            )
        elif command == "sync-knowledge":
            result = sync_pa_knowledge(
                args.source_dir,
                args.constitution,
                args.target_dir,
                args.manifest,
            )
        else:
            raise PaComposeError("a PA subcommand is required")
    except PaComposeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=os.sys.stderr)
        raise SystemExit(2) from exc
    if command == "compose":
        data = {
            "output": str(Path(args.output).expanduser().resolve()),
            "manifest": str(Path(args.manifest).expanduser().resolve()),
            "source_count": result["source_count"],
            "composed_source_count": result["composed_source_count"],
            "constitution_sha256": result["constitution_sha256"],
            "sources_sha256": result["sources_sha256"],
            "allow_unverified": result["allow_unverified"],
        }
    else:
        data = {
            "target_dir": str(Path(args.target_dir).expanduser().resolve()),
            "manifest": str(Path(args.manifest).expanduser().resolve()),
            "file_count": result["file_count"],
        }
    print(json.dumps({"ok": True, "data": data}, sort_keys=True))
