"""FastMCP tool, resource, and prompt registration."""

from __future__ import annotations

import json
import os
import sys
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider, require_scopes
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .artifacts import ArtifactStore
from .auth import READ_SCOPE, WRITE_SCOPE, auth_from_env, is_loopback_host
from .config import Settings
from .errors import ConfigurationError, PathPolicyError
from .provenance import (
    assert_file_manifest_matches,
    build_predictor_manifest,
    read_manifest,
)
from .r_bridge import RBridge

RASTER_SUFFIXES = (".tif", ".tiff", ".grd", ".asc", ".img", ".bil", ".nc")
POINT_SUFFIXES = (".csv",)


class EnvelopeModel(StrEnum):
    BIOCLIM = "bioclim"
    DOMAIN = "domain"
    MAHAL = "mahal"
    MAXENT = "maxent"


class RangeModel(StrEnum):
    CONVEX_HULL = "convex_hull"
    RECTANGULAR_HULL = "rectangular_hull"
    CIRCLE_HULL = "circle_hull"
    CIRCLES = "circles"


class OverlapStatistic(StrEnum):
    WARREN_I = "I"
    SCHOENER_D = "D"


def create_server(
    settings: Settings | None = None,
    *,
    auth: AuthProvider | None = None,
) -> FastMCP:
    settings = settings or Settings.from_env()
    store = ArtifactStore(settings.workspace)
    bridge = RBridge(settings, store)

    mcp = FastMCP(
        "dismo-mcp",
        version=__version__,
        instructions=(
            "Use this server for reproducible species distribution modeling with the R "
            "rspatial/dismo package. Inspect inputs first, use generated run_id values to "
            "chain model operations, and report optional dependency warnings to the user."
        ),
        auth=auth,
        mask_error_details=True,
        strict_input_validation=True,
    )
    read_auth = require_scopes(READ_SCOPE) if auth is not None else None
    write_auth = require_scopes(WRITE_SCOPE) if auth is not None else None

    def input_path(value: str, suffixes: tuple[str, ...] | None = None) -> str:
        return str(settings.resolve_input(value, suffixes=suffixes))

    def raster_paths(values: list[str]) -> list[str]:
        if not values:
            raise ValueError("At least one raster path is required")
        return [input_path(value, RASTER_SUFFIXES) for value in values]

    def model_predictor_manifest(model_run_id: str, paths: list[str]) -> tuple[str, dict[str, Any]]:
        manifest_path = store.artifact_path(model_run_id, "predictor_manifest")
        expected = read_manifest(manifest_path)
        current = build_predictor_manifest(paths, max_bytes=settings.max_input_bytes)
        assert_file_manifest_matches(expected, current)
        return str(manifest_path), current

    def generated_points_artifact(run_id: str, label: str) -> tuple[str, str]:
        metadata = store.read(run_id)
        output_crs = (metadata.get("result") or {}).get("output_crs")
        if not output_crs:
            raise PathPolicyError(f"{label} run has no output_crs provenance: {run_id}")
        return str(store.artifact_path(run_id, "background_points")), str(output_crs)

    def run(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        result, _ = bridge.execute(operation, params)
        return result

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        tags={"diagnostics"},
        auth=read_auth,
    )
    def dismo_system_info() -> dict[str, Any]:
        """Check R, dismo, geospatial dependencies, and optional model backends."""
        result = run("system_info", {})
        result["python_version"] = sys.version.split()[0]
        result["workspace"] = str(settings.workspace)
        result["allowed_roots"] = [str(path) for path in settings.allowed_roots]
        result["rscript"] = str(settings.rscript) if settings.rscript else None
        return result

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        tags={"inspection", "raster"},
        auth=read_auth,
    )
    def inspect_raster(
        paths: Annotated[list[str], Field(min_length=1)],
        include_stats: bool = False,
    ) -> dict[str, Any]:
        """Inspect predictor raster geometry, CRS, layers, and optional cell statistics."""
        return run(
            "inspect_raster",
            {"paths": raster_paths(paths), "include_stats": include_stats},
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        tags={"inspection", "occurrences"},
        auth=read_auth,
    )
    def inspect_occurrences(
        path: str,
        lon_col: str = "lon",
        lat_col: str = "lat",
        point_crs: str = "EPSG:4326",
    ) -> dict[str, Any]:
        """Validate a CSV occurrence table and summarize coordinate quality and extent."""
        return run(
            "inspect_points",
            {
                "path": input_path(path, POINT_SUFFIXES),
                "lon_col": lon_col,
                "lat_col": lat_col,
                "point_crs": point_crs,
            },
        )

    @mcp.tool(tags={"data", "raster", "occurrences"}, auth=write_auth)
    def extract_predictor_values(
        predictor_paths: Annotated[list[str], Field(min_length=1)],
        points_path: str,
        lon_col: str = "lon",
        lat_col: str = "lat",
        point_crs: str = "EPSG:4326",
    ) -> dict[str, Any]:
        """Extract all predictor values at occurrence coordinates into a CSV artifact."""
        return run(
            "extract_predictor_values",
            {
                "predictor_paths": raster_paths(predictor_paths),
                "points_path": input_path(points_path, POINT_SUFFIXES),
                "lon_col": lon_col,
                "lat_col": lat_col,
                "point_crs": point_crs,
            },
        )

    @mcp.tool(tags={"sampling", "background"}, auth=write_auth)
    def generate_background_points(
        mask_path: str,
        n: Annotated[int, Field(ge=1, le=1_000_000)] = 10_000,
        presence_path: str | None = None,
        lon_col: str = "lon",
        lat_col: str = "lat",
        point_crs: str = "EPSG:4326",
        extent: Annotated[list[float] | None, Field(min_length=4, max_length=4)] = None,
        exclude_presence: bool = True,
        probability_weighted: bool = False,
        lonlat_correction: bool = True,
        seed: int = 1,
    ) -> dict[str, Any]:
        """Sample reproducible background points from non-NA cells of a mask raster."""
        params: dict[str, Any] = {
            "mask_path": input_path(mask_path, RASTER_SUFFIXES),
            "n": n,
            "lon_col": lon_col,
            "lat_col": lat_col,
            "point_crs": point_crs,
            "extent": extent,
            "exclude_presence": exclude_presence,
            "probability_weighted": probability_weighted,
            "lonlat_correction": lonlat_correction,
            "seed": seed,
        }
        if presence_path is not None:
            params["presence_path"] = input_path(presence_path, POINT_SUFFIXES)
        return run("generate_background_points", params)

    @mcp.tool(tags={"sampling", "validation"}, auth=write_auth)
    def partition_occurrences(
        points_path: str,
        k: Annotated[int, Field(ge=2, le=100)] = 5,
        lon_col: str = "lon",
        lat_col: str = "lat",
        point_crs: str = "EPSG:4326",
        by_col: str | None = None,
        seed: int = 1,
    ) -> dict[str, Any]:
        """Create reproducible dismo k-fold assignments, optionally within groups."""
        return run(
            "partition_occurrences",
            {
                "points_path": input_path(points_path, POINT_SUFFIXES),
                "k": k,
                "lon_col": lon_col,
                "lat_col": lat_col,
                "point_crs": point_crs,
                "by_col": by_col,
                "seed": seed,
            },
        )

    @mcp.tool(tags={"model", "training"}, auth=write_auth)
    def fit_sdm(
        model_type: EnvelopeModel,
        predictor_paths: Annotated[list[str], Field(min_length=1)],
        presence_path: str,
        background_path: str | None = None,
        background_run_id: str | None = None,
        background_crs: str | None = None,
        lon_col: str = "lon",
        lat_col: str = "lat",
        point_crs: str = "EPSG:4326",
        folds_path: str | None = None,
        train_folds: Annotated[list[int] | None, Field(min_length=1)] = None,
        held_out_fold: Annotated[int | None, Field(ge=1)] = None,
        factors: list[str] | None = None,
        background_count: Annotated[int, Field(ge=1, le=1_000_000)] = 10_000,
        remove_duplicates: bool = True,
        maxent_args: list[str] | None = None,
        seed: int = 1,
    ) -> dict[str, Any]:
        """Fit an SDM, optionally excluding a held-out fold, and persist its inputs."""
        if train_folds is not None and held_out_fold is not None:
            raise ValueError("Use train_folds or held_out_fold, not both")
        if (train_folds is not None or held_out_fold is not None) and folds_path is None:
            raise ValueError("folds_path is required when selecting training folds")
        if background_path is not None and background_run_id is not None:
            raise ValueError("Use background_path or background_run_id, not both")
        if background_path is not None and not background_crs:
            raise ValueError(
                "background_crs is required for a path; use background_run_id to inherit output_crs"
            )
        predictor_files = raster_paths(predictor_paths)
        params: dict[str, Any] = {
            "model_type": model_type.value,
            "predictor_paths": predictor_files,
            "presence_path": input_path(presence_path, POINT_SUFFIXES),
            "lon_col": lon_col,
            "lat_col": lat_col,
            "point_crs": point_crs,
            "train_folds": train_folds,
            "held_out_fold": held_out_fold,
            "predictor_manifest": build_predictor_manifest(
                predictor_files, max_bytes=settings.max_input_bytes
            ),
            "factors": factors or [],
            "background_count": background_count,
            "remove_duplicates": remove_duplicates,
            "maxent_args": maxent_args or [],
            "seed": seed,
        }
        if background_path is not None:
            params["background_path"] = input_path(background_path, POINT_SUFFIXES)
            params["background_crs"] = background_crs
        if background_run_id is not None:
            params["background_path"], params["background_crs"] = generated_points_artifact(
                background_run_id, "background"
            )
        if folds_path is not None:
            params["folds_path"] = input_path(folds_path, POINT_SUFFIXES)
        return run("fit_model", params)

    @mcp.tool(tags={"model", "range"}, auth=write_auth)
    def fit_range_model(
        model_type: RangeModel,
        presence_path: str,
        lon_col: str = "lon",
        lat_col: str = "lat",
        lonlat: bool = True,
        crs: str | None = None,
        clusters: Annotated[int, Field(ge=1, le=100)] = 1,
        distance: Annotated[float | None, Field(gt=0)] = None,
        seed: int = 1,
    ) -> dict[str, Any]:
        """Fit a geographic hull or circles range model and export its geometry."""
        return run(
            "fit_range_model",
            {
                "model_type": model_type.value,
                "presence_path": input_path(presence_path, POINT_SUFFIXES),
                "lon_col": lon_col,
                "lat_col": lat_col,
                "lonlat": lonlat,
                "crs": crs,
                "clusters": clusters,
                "distance": distance,
                "seed": seed,
            },
        )

    @mcp.tool(tags={"model", "prediction"}, auth=write_auth)
    def predict_sdm(
        model_run_id: str,
        predictor_paths: Annotated[list[str], Field(min_length=1)],
        prediction_args: list[str] | None = None,
        include_stats: bool = True,
    ) -> dict[str, Any]:
        """Predict a saved dismo model over raster layers and create a GeoTIFF artifact."""
        predictor_files = raster_paths(predictor_paths)
        manifest_path, predictor_manifest = model_predictor_manifest(
            model_run_id, predictor_files
        )
        return run(
            "predict_model",
            {
                "model_path": str(store.artifact_path(model_run_id, "model")),
                "predictor_paths": predictor_files,
                "predictor_manifest": predictor_manifest,
                "expected_predictor_manifest_path": manifest_path,
                "prediction_args": prediction_args or [],
                "include_stats": include_stats,
            },
        )

    @mcp.tool(tags={"model", "evaluation"}, auth=write_auth)
    def evaluate_sdm(
        model_run_id: str,
        predictor_paths: Annotated[list[str], Field(min_length=1)],
        presence_path: str,
        absence_path: str | None = None,
        absence_run_id: str | None = None,
        absence_crs: str | None = None,
        lon_col: str = "lon",
        lat_col: str = "lat",
        point_crs: str = "EPSG:4326",
        folds_path: str | None = None,
        test_fold: Annotated[int | None, Field(ge=1)] = None,
        allow_training_overlap: bool = False,
    ) -> dict[str, Any]:
        """Evaluate on held-out points and reject training-presence overlap by default."""
        if test_fold is not None and folds_path is None:
            raise ValueError("folds_path is required when selecting a test fold")
        if absence_path is not None and absence_run_id is not None:
            raise ValueError("Use absence_path or absence_run_id, not both")
        if absence_path is None and absence_run_id is None:
            raise ValueError("Either absence_path or absence_run_id is required")
        if absence_path is not None and not absence_crs:
            raise ValueError(
                "absence_crs is required for a path; use absence_run_id to inherit output_crs"
            )
        predictor_files = raster_paths(predictor_paths)
        manifest_path, predictor_manifest = model_predictor_manifest(
            model_run_id, predictor_files
        )
        if absence_run_id is not None:
            resolved_absence_path, resolved_absence_crs = generated_points_artifact(
                absence_run_id, "absence"
            )
        else:
            resolved_absence_path, resolved_absence_crs = (
                input_path(absence_path or "", POINT_SUFFIXES),
                absence_crs,
            )
        params: dict[str, Any] = {
            "model_path": str(store.artifact_path(model_run_id, "model")),
            "predictor_paths": predictor_files,
            "predictor_manifest": predictor_manifest,
            "expected_predictor_manifest_path": manifest_path,
            "presence_path": input_path(presence_path, POINT_SUFFIXES),
            "absence_path": resolved_absence_path,
            "lon_col": lon_col,
            "lat_col": lat_col,
            "point_crs": point_crs,
            "absence_crs": resolved_absence_crs,
            "test_fold": test_fold,
            "allow_training_overlap": allow_training_overlap,
        }
        if folds_path is not None:
            params["folds_path"] = input_path(folds_path, POINT_SUFFIXES)
        if not allow_training_overlap:
            params["training_points_path"] = str(
                store.artifact_path(model_run_id, "training_points")
            )
        return run(
            "evaluate_model",
            params,
        )

    @mcp.tool(tags={"model", "interpretation"}, auth=write_auth)
    def create_response_curves(
        model_run_id: str,
        variables: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create CSV and PDF response curves for a saved environmental model."""
        return run(
            "response_curves",
            {
                "model_path": str(store.artifact_path(model_run_id, "model")),
                "variables": variables or [],
            },
        )

    @mcp.tool(tags={"analysis", "extrapolation"}, auth=write_auth)
    def calculate_mess(
        predictor_paths: Annotated[list[str], Field(min_length=1)],
        reference_points_path: str,
        lon_col: str = "lon",
        lat_col: str = "lat",
        point_crs: str = "EPSG:4326",
        full: bool = False,
        include_stats: bool = True,
    ) -> dict[str, Any]:
        """Calculate Multivariate Environmental Similarity Surfaces (MESS)."""
        return run(
            "environmental_similarity",
            {
                "predictor_paths": raster_paths(predictor_paths),
                "reference_points_path": input_path(reference_points_path, POINT_SUFFIXES),
                "lon_col": lon_col,
                "lat_col": lat_col,
                "point_crs": point_crs,
                "full": full,
                "include_stats": include_stats,
            },
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        tags={"analysis", "niche"},
        auth=read_auth,
    )
    def calculate_niche_overlap(
        first_prediction_path: str,
        second_prediction_path: str,
        statistic: OverlapStatistic = OverlapStatistic.WARREN_I,
        mask: bool = True,
        check_negatives: bool = True,
    ) -> dict[str, Any]:
        """Calculate Warren's I or Schoener's D between two prediction rasters."""
        return run(
            "niche_overlap",
            {
                "first_prediction_path": input_path(first_prediction_path, RASTER_SUFFIXES),
                "second_prediction_path": input_path(second_prediction_path, RASTER_SUFFIXES),
                "statistic": statistic.value,
                "mask": mask,
                "check_negatives": check_negatives,
            },
        )

    @mcp.tool(tags={"data", "climate"}, auth=write_auth)
    def compute_bioclimatic_variables(
        precipitation_paths: Annotated[list[str], Field(min_length=1)],
        minimum_temperature_paths: Annotated[list[str], Field(min_length=1)],
        maximum_temperature_paths: Annotated[list[str], Field(min_length=1)],
        include_stats: bool = False,
    ) -> dict[str, Any]:
        """Compute the 19 BIOCLIM variables from three 12-layer monthly raster sets."""
        return run(
            "compute_biovars",
            {
                "precipitation_paths": raster_paths(precipitation_paths),
                "minimum_temperature_paths": raster_paths(minimum_temperature_paths),
                "maximum_temperature_paths": raster_paths(maximum_temperature_paths),
                "include_stats": include_stats,
            },
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        tags={"artifacts"},
        auth=read_auth,
    )
    def list_runs(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> list[dict[str, Any]]:
        """List recent dismo-mcp runs and their completion status."""
        return store.list(limit)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        tags={"artifacts"},
        auth=read_auth,
    )
    def get_run(run_id: str) -> dict[str, Any]:
        """Read one run's result metadata and generated artifact paths."""
        return store.read(run_id)

    @mcp.tool(tags={"artifacts", "control"}, auth=write_auth)
    def cancel_run(run_id: str) -> dict[str, Any]:
        """Cancel a queued or running R operation and mark its run failed."""
        cancelled = bridge.cancel(run_id)
        return {"run_id": run_id, "cancelled": cancelled, "status": "failed" if cancelled else store.read(run_id).get("status")}

    @mcp.resource(
        "dismo://capabilities",
        name="dismo-capabilities",
        description="Server capability map and modeling boundaries.",
        mime_type="application/json",
    )
    def capabilities() -> str:
        return json.dumps(
            {
                "server_version": __version__,
                "models": [item.value for item in EnvelopeModel],
                "range_models": [item.value for item in RangeModel],
                "operations": [
                    "inspect inputs",
                    "extract predictor values",
                    "sample background points",
                    "partition occurrences",
                    "fit/predict/evaluate models",
                    "response curves",
                    "MESS",
                    "niche overlap",
                    "BIOCLIM variables",
                ],
                "excluded": [
                    "arbitrary R execution",
                    "legacy GBIF/geocoding helpers",
                    "interactive plotting",
                ],
                "workspace": str(settings.workspace),
            },
            indent=2,
        )

    @mcp.resource(
        "dismo://runs/{run_id}",
        name="dismo-run",
        description="Metadata and artifacts for one dismo-mcp run.",
        mime_type="application/json",
    )
    def run_resource(run_id: str) -> str:
        return json.dumps(store.read(run_id), indent=2, ensure_ascii=False)

    @mcp.prompt(name="species_distribution_workflow")
    def species_distribution_workflow(
        predictors: str,
        occurrences: str,
        model: EnvelopeModel = EnvelopeModel.BIOCLIM,
    ) -> str:
        """Guide an agent through a reproducible dismo modeling workflow."""
        return (
            f"Build a reproducible {model.value} species distribution model. "
            f"Predictors are {predictors}; occurrences are {occurrences}. First run "
            "dismo_system_info, inspect_raster, and inspect_occurrences. Then generate "
            "background points and k-fold partitions. Pass folds_path plus held_out_fold "
            "to fit_sdm, then pass the same folds_path plus test_fold to evaluate_sdm. "
            "Never train on the test fold. Predict a GeoTIFF and calculate MESS. Report "
            "assumptions, warnings, metrics, thresholds, and artifact paths."
        )

    if auth is None:
        def blocked_http_app(*args: Any, **kwargs: Any):
            raise ConfigurationError(
                "Unauthenticated servers cannot create an HTTP app; use create_http_app()"
            )

        mcp.http_app = blocked_http_app  # type: ignore[method-assign]
    return mcp


def create_http_server(
    settings: Settings | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    """Create an authenticated HTTP server safe to place behind HTTPS."""
    if not is_loopback_host(host):
        raise ConfigurationError(
            "HTTP app factory only permits loopback binding; expose it through an HTTPS reverse proxy"
        )
    public_base_url = os.getenv("DISMO_MCP_PUBLIC_BASE_URL", f"http://{host}:{port}")
    parsed = urlparse(public_base_url)
    if parsed.scheme != "https" and not is_loopback_host(parsed.hostname or ""):
        raise ConfigurationError(
            "DISMO_MCP_PUBLIC_BASE_URL must use HTTPS for non-loopback deployments"
        )
    auth = auth_from_env(base_url=public_base_url)
    if auth is None:
        raise ConfigurationError(
            "HTTP transport requires DISMO_MCP_BEARER_TOKEN or read/write token variables"
        )
    return create_server(settings, auth=auth)


def create_http_app(
    settings: Settings | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
):
    """ASGI factory with the same loopback and authentication checks as the CLI."""
    return create_http_server(settings, host=host, port=port).http_app()
