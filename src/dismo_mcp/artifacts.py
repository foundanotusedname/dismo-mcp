"""Run metadata and generated artifact management."""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ArtifactStoreError, PathPolicyError

RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: str
    operation: str
    directory: Path


class ArtifactStore:
    def __init__(
        self,
        workspace: Path,
        *,
        max_runs: int = 1000,
        max_bytes: int = 10_000_000_000,
        retention_seconds: int = 0,
    ) -> None:
        if max_runs < 1 or max_bytes < 1 or retention_seconds < 0:
            raise ValueError("Invalid run storage limits")
        self.root = workspace / ".dismo-mcp" / "runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_runs = max_runs
        self.max_bytes = max_bytes
        self.retention_seconds = retention_seconds
        self._lock = threading.RLock()

    def _run_directories(self) -> list[Path]:
        return [
            path
            for path in self.root.iterdir()
            if path.is_dir() and RUN_ID_RE.fullmatch(path.name)
        ]

    @staticmethod
    def _directory_size(directory: Path) -> int:
        total = 0
        for path in directory.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    @staticmethod
    def _metadata_for(directory: Path) -> dict[str, Any] | None:
        try:
            value = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _prune_locked(self) -> None:
        directories = self._run_directories()
        metadata = {path: self._metadata_for(path) for path in directories}
        candidates = [
            path
            for path in directories
            if metadata[path] and metadata[path].get("status") in {"completed", "failed", "cancelled"}
        ]
        candidates.sort(key=lambda path: path.name)
        now = datetime.now(UTC).timestamp()
        removed: set[Path] = set()

        def remove(path: Path) -> None:
            try:
                shutil.rmtree(path)
            except OSError:
                return
            removed.add(path)

        if self.retention_seconds:
            cutoff = now - self.retention_seconds
            for path in candidates:
                value = metadata[path] or {}
                timestamp = value.get("completed_at") or value.get("created_at")
                try:
                    old = datetime.fromisoformat(str(timestamp)).timestamp()
                except (TypeError, ValueError):
                    old = path.stat().st_mtime
                if old < cutoff:
                    remove(path)

        directories = [path for path in directories if path not in removed]
        candidates = [path for path in candidates if path not in removed]
        total_bytes = sum(self._directory_size(path) for path in directories)
        while len(directories) >= self.max_runs or total_bytes >= self.max_bytes:
            if not candidates:
                raise ArtifactStoreError(
                    "Run storage limit reached; remove completed runs or increase "
                    "DISMO_MCP_MAX_RUNS/DISMO_MCP_MAX_RUN_BYTES"
                )
            path = candidates.pop(0)
            size = self._directory_size(path)
            remove(path)
            if path in removed:
                directories.remove(path)
                total_bytes -= size

    def create(self, operation: str) -> RunContext:
        with self._lock:
            self._prune_locked()
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            run_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
            directory = self.root / run_id
            directory.mkdir(parents=False, exist_ok=False)
            context = RunContext(run_id, operation, directory)
            self.write_metadata(
                context,
                {
                    "run_id": run_id,
                    "operation": operation,
                    "status": "running",
                    "created_at": datetime.now(UTC).isoformat(),
                    "artifacts": [],
                },
            )
            return context

    def write_metadata(self, context: RunContext, metadata: dict[str, Any]) -> None:
        with self._lock:
            target = context.directory / "metadata.json"
            temporary = context.directory / "metadata.json.tmp"
            temporary.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)

    def finish(
        self,
        context: RunContext,
        result: dict[str, Any],
        *,
        status: str = "completed",
    ) -> dict[str, Any]:
        with self._lock:
            metadata = {
                "run_id": context.run_id,
                "operation": context.operation,
                "status": status,
                "created_at": self.read(context.run_id).get("created_at"),
                "completed_at": datetime.now(UTC).isoformat(),
                "artifacts": result.get("artifacts", []),
                "result": result,
            }
            self.write_metadata(context, metadata)
            return metadata

    def fail(self, context: RunContext, message: str) -> None:
        with self._lock:
            current = self.read(context.run_id)
            if current.get("status") in {"completed", "failed", "cancelled"}:
                return
            current.update(
                status="failed",
                completed_at=datetime.now(UTC).isoformat(),
                error=message,
            )
            self.write_metadata(context, current)

    def directory_for(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise PathPolicyError(f"Invalid run_id: {run_id}")
        directory = self.root / run_id
        if not directory.is_dir():
            raise PathPolicyError(f"Unknown run_id: {run_id}")
        return directory

    def read(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            path = self.directory_for(run_id) / "metadata.json"
            return json.loads(path.read_text(encoding="utf-8"))

    def artifact_path(self, run_id: str, role: str) -> Path:
        metadata = self.read(run_id)
        for item in metadata.get("artifacts", []):
            if item.get("role") == role:
                path = Path(item["path"]).resolve()
                directory = self.directory_for(run_id).resolve()
                try:
                    path.relative_to(directory)
                except ValueError as exc:
                    raise PathPolicyError(
                        f"Artifact metadata points outside run directory: {run_id}/{role}"
                    ) from exc
                if not path.is_file():
                    raise PathPolicyError(f"Artifact is missing: {run_id}/{role}")
                return path
        raise PathPolicyError(f"Run {run_id} has no artifact with role: {role}")

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        directories = sorted(
            (path for path in self.root.iterdir() if path.is_dir() and RUN_ID_RE.fullmatch(path.name)),
            key=lambda path: path.name,
            reverse=True,
        )
        items = []
        for directory in directories[:limit]:
            try:
                metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
                items.append(
                    {
                        key: metadata.get(key)
                        for key in ("run_id", "operation", "status", "created_at", "completed_at")
                    }
                )
            except (OSError, json.JSONDecodeError):
                continue
        return items
