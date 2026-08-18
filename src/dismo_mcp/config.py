"""Runtime configuration and filesystem access policy."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError, PathPolicyError


def _discover_rscript() -> Path | None:
    configured = os.getenv("DISMO_MCP_RSCRIPT")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise ConfigurationError(f"DISMO_MCP_RSCRIPT is not a file: {candidate}")

    on_path = shutil.which("Rscript") or shutil.which("Rscript.exe")
    if on_path:
        return Path(on_path).resolve()

    if os.name == "nt":
        roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "R"]
        roots.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "R")
        matches: list[Path] = []
        for root in roots:
            if root.is_dir():
                matches.extend(root.glob("R-*/bin/Rscript.exe"))
                matches.extend(root.glob("R-*/bin/x64/Rscript.exe"))
        if matches:
            return sorted(matches, reverse=True)[0].resolve()
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Settings:
    workspace: Path
    allowed_roots: tuple[Path, ...]
    rscript: Path | None
    timeout_seconds: int = 600
    max_concurrent_r: int = 2
    max_queued_r: int = 8
    queue_wait_seconds: int = 30
    max_raster_cells: int = 50_000_000
    max_point_rows: int = 1_000_000
    max_input_bytes: int = 2_000_000_000
    max_r_memory_mb: int = 4096

    @classmethod
    def from_env(cls) -> "Settings":
        workspace = Path(os.getenv("DISMO_MCP_WORKSPACE", Path.cwd())).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        extra_roots = []
        raw_roots = os.getenv("DISMO_MCP_ALLOWED_ROOTS", "")
        for value in raw_roots.split(os.pathsep):
            if value.strip():
                extra_roots.append(Path(value.strip()).expanduser().resolve())

        timeout = int(os.getenv("DISMO_MCP_TIMEOUT_SECONDS", "600"))
        if timeout < 1:
            raise ConfigurationError("DISMO_MCP_TIMEOUT_SECONDS must be positive")
        max_concurrent_r = int(os.getenv("DISMO_MCP_MAX_CONCURRENT_R", "2"))
        max_queued_r = int(os.getenv("DISMO_MCP_MAX_QUEUED_R", "8"))
        queue_wait_seconds = int(os.getenv("DISMO_MCP_QUEUE_WAIT_SECONDS", "30"))
        max_raster_cells = int(os.getenv("DISMO_MCP_MAX_RASTER_CELLS", "50000000"))
        max_point_rows = int(os.getenv("DISMO_MCP_MAX_POINT_ROWS", "1000000"))
        max_input_bytes = int(os.getenv("DISMO_MCP_MAX_INPUT_BYTES", "2000000000"))
        max_r_memory_mb = int(os.getenv("DISMO_MCP_MAX_R_MEMORY_MB", "4096"))
        if max_concurrent_r < 1 or max_queued_r < 0 or queue_wait_seconds < 1:
            raise ConfigurationError("R concurrency and queue settings must be non-negative/positive")
        if max_raster_cells < 1 or max_point_rows < 1 or max_input_bytes < 1 or max_r_memory_mb < 128:
            raise ConfigurationError("R resource limits must be positive")
        roots = tuple(dict.fromkeys([workspace, *extra_roots]))
        return cls(
            workspace,
            roots,
            _discover_rscript(),
            timeout,
            max_concurrent_r,
            max_queued_r,
            queue_wait_seconds,
            max_raster_cells,
            max_point_rows,
            max_input_bytes,
            max_r_memory_mb,
        )

    def resolve_input(self, value: str, *, suffixes: tuple[str, ...] | None = None) -> Path:
        raw = Path(value).expanduser()
        path = (self.workspace / raw if not raw.is_absolute() else raw).resolve()
        if not path.is_file():
            raise PathPolicyError(f"Input file does not exist: {path}")
        if path.stat().st_size > self.max_input_bytes:
            raise PathPolicyError(
                f"Input file exceeds DISMO_MCP_MAX_INPUT_BYTES ({self.max_input_bytes}): {path}"
            )
        if not any(_is_within(path, root) for root in self.allowed_roots):
            roots = ", ".join(str(root) for root in self.allowed_roots)
            raise PathPolicyError(f"Input file is outside allowed roots ({roots}): {path}")
        if suffixes and path.suffix.lower() not in suffixes:
            allowed = ", ".join(suffixes)
            raise PathPolicyError(f"Expected one of [{allowed}], got: {path.name}")
        return path

    def require_rscript(self) -> Path:
        if self.rscript is None:
            raise ConfigurationError(
                "Rscript was not found. Install R or set DISMO_MCP_RSCRIPT to the executable."
            )
        return self.rscript
