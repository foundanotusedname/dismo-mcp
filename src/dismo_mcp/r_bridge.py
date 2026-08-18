"""Process-isolated bridge to a fixed set of R operations."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore, RunContext
from .config import Settings
from .errors import ConfigurationError, RBridgeError


class RBridge:
    def __init__(self, settings: Settings, store: ArtifactStore) -> None:
        self.settings = settings
        self.store = store
        self.script = Path(__file__).parent / "r" / "bridge.R"
        self._queue_slots = threading.BoundedSemaphore(
            settings.max_concurrent_r + settings.max_queued_r
        )
        self._active_slots = threading.BoundedSemaphore(settings.max_concurrent_r)
        self._process_lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()

    def execute(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        context: RunContext | None = None,
    ) -> tuple[dict[str, Any], RunContext]:
        run = context or self.store.create(operation)
        request_path = run.directory / "request.json"
        response_path = run.directory / "response.json"
        request = {
            "operation": operation,
            "params": params,
            "run_dir": str(run.directory),
        }
        if not self._queue_slots.acquire(blocking=False):
            message = (
                "R operation queue is full; retry later or increase "
                "DISMO_MCP_MAX_QUEUED_R"
            )
            self.store.fail(run, message)
            raise RBridgeError(message)
        active_acquired = False
        try:
            deadline = time.monotonic() + self.settings.queue_wait_seconds
            while not self._active_slots.acquire(timeout=0.2):
                with self._process_lock:
                    if run.run_id in self._cancelled:
                        message = f"R operation cancelled while queued: {operation}"
                        self.store.fail(run, message)
                        raise RBridgeError(message)
                if time.monotonic() >= deadline:
                    message = "R operation queue wait timed out"
                    self.store.fail(run, message)
                    raise RBridgeError(message)
            active_acquired = True
            with self._process_lock:
                if run.run_id in self._cancelled:
                    message = f"R operation cancelled before start: {operation}"
                    self.store.fail(run, message)
                    raise RBridgeError(message)
            request_path.write_text(
                json.dumps(request, ensure_ascii=False), encoding="utf-8"
            )
            command = [
                str(self.settings.require_rscript()),
                "--vanilla",
                str(self.script),
                str(request_path),
                str(response_path),
            ]
            env = os.environ.copy()
            env.setdefault("R_DEFAULT_PACKAGES", "utils,stats,graphics,grDevices,methods")
            env["DISMO_MCP_MAX_RASTER_CELLS"] = str(self.settings.max_raster_cells)
            env["DISMO_MCP_MAX_POINT_ROWS"] = str(self.settings.max_point_rows)
            env["R_MAX_VSIZE"] = f"{self.settings.max_r_memory_mb}M"
            process = subprocess.Popen(
                command,
                cwd=run.directory,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with self._process_lock:
                self._processes[run.run_id] = process
                if run.run_id in self._cancelled:
                    process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=self.settings.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                stdout, stderr = process.communicate()
                message = f"R operation timed out after {self.settings.timeout_seconds}s: {operation}"
                self.store.fail(run, message)
                raise RBridgeError(message) from exc
            completed = subprocess.CompletedProcess(
                command, process.returncode, stdout=stdout, stderr=stderr
            )
        except ConfigurationError as exc:
            self.store.fail(run, str(exc))
            raise
        except subprocess.TimeoutExpired as exc:
            message = f"R operation timed out after {self.settings.timeout_seconds}s: {operation}"
            self.store.fail(run, message)
            raise RBridgeError(message) from exc
        except OSError as exc:
            message = f"Could not initialize or start Rscript: {exc}"
            self.store.fail(run, message)
            raise RBridgeError(message) from exc
        finally:
            with self._process_lock:
                self._processes.pop(run.run_id, None)
                self._cancelled.discard(run.run_id)
            if active_acquired:
                self._active_slots.release()
            self._queue_slots.release()

        if not response_path.is_file():
            current = self.store.read(run.run_id)
            if current.get("status") == "failed" and "cancelled" in str(
                current.get("error", "")
            ).lower():
                raise RBridgeError(f"R operation cancelled: {operation}")
            details = (completed.stderr or completed.stdout).strip()[-4000:]
            message = f"R operation produced no response (exit {completed.returncode}): {details}"
            self.store.fail(run, message)
            raise RBridgeError(message)

        try:
            envelope = json.loads(response_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            message = f"R operation returned invalid JSON: {exc}"
            self.store.fail(run, message)
            raise RBridgeError(message) from exc

        if not envelope.get("ok"):
            error = envelope.get("error", {})
            message = error.get("message", "Unknown R error")
            error_class = error.get("class", "error")
            self.store.fail(run, f"{error_class}: {message}")
            raise RBridgeError(f"{operation} failed in R ({error_class}): {message}")

        result = envelope.get("result") or {}
        result["run_id"] = run.run_id
        result["operation"] = operation
        self.store.finish(run, result)
        return result, run

    def cancel(self, run_id: str) -> bool:
        """Request cancellation of a queued or running R operation."""
        directory = self.store.directory_for(run_id)
        metadata = self.store.read(run_id)
        if metadata.get("status") != "running":
            return False
        with self._process_lock:
            self._cancelled.add(run_id)
            process = self._processes.get(run_id)
            if process is not None and process.poll() is None:
                process.terminate()
        self.store.fail(
            RunContext(run_id, metadata.get("operation", "unknown"), directory),
            "R operation cancelled by caller",
        )
        return True
