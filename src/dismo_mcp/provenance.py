"""Content and geometry provenance for predictor raster inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import PathPolicyError

_SIDECAR_SUFFIXES = (
    ".gri",
    ".hdr",
    ".prj",
    ".aux.xml",
    ".ovr",
    ".tfw",
    ".wld",
)


def _component_paths(path: Path) -> list[tuple[str, Path]]:
    components = [("primary", path)]
    stem = path.with_suffix("")
    for suffix in _SIDECAR_SUFFIXES:
        candidate = Path(f"{stem}{suffix}")
        if candidate.is_file():
            components.append((suffix.lstrip("."), candidate))
    for suffix in (".aux.xml", ".ovr"):
        candidate = Path(f"{path}{suffix}")
        if candidate.is_file():
            components.append((suffix.lstrip("."), candidate))
    return components


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_predictor_manifest(
    paths: list[str | Path], *, max_bytes: int | None = None
) -> dict[str, Any]:
    """Fingerprint predictor files, including common raster sidecars."""
    inputs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for index, raw_path in enumerate(paths):
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise PathPolicyError(f"Predictor file does not exist: {path}")
        components: list[dict[str, Any]] = []
        for role, component in _component_paths(path):
            size_bytes = component.stat().st_size
            if max_bytes is not None and size_bytes > max_bytes:
                raise PathPolicyError(
                    f"Predictor component exceeds the configured input limit ({max_bytes}): {component}"
                )
            record = {
                "input_index": index,
                "role": role,
                "name": component.name,
                "size_bytes": size_bytes,
                "sha256": _sha256(component),
            }
            files.append(record)
            components.append(record.copy())
        inputs.append({"index": index, "name": path.name, "components": components})
    return {
        "schema_version": 1,
        "algorithm": "sha256",
        "inputs": inputs,
        "files": files,
    }


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PathPolicyError(f"Invalid predictor manifest: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise PathPolicyError(f"Unsupported predictor manifest: {path}")
    return value


def assert_file_manifest_matches(
    expected: dict[str, Any], current: dict[str, Any]
) -> None:
    expected_files = [
        (item.get("input_index"), item.get("role"), item.get("size_bytes"), item.get("sha256"))
        for item in expected.get("files", [])
    ]
    current_files = [
        (item.get("input_index"), item.get("role"), item.get("size_bytes"), item.get("sha256"))
        for item in current.get("files", [])
    ]
    if expected_files != current_files:
        raise PathPolicyError(
            "Predictor files differ from the model manifest; refuse to use a different dataset"
        )
