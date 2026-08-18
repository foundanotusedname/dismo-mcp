"""Run metadata and generated artifact management."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import PathPolicyError

RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: str
    operation: str
    directory: Path


class ArtifactStore:
    def __init__(self, workspace: Path) -> None:
        self.root = workspace / ".dismo-mcp" / "runs"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, operation: str) -> RunContext:
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
        current = self.read(context.run_id)
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
