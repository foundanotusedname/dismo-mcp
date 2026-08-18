from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dismo_mcp.artifacts import ArtifactStore
from dismo_mcp.config import Settings
from dismo_mcp.errors import RBridgeError
from dismo_mcp.provenance import build_predictor_manifest
from dismo_mcp.r_bridge import RBridge
from dismo_mcp.server import EnvelopeModel, create_server


def _example_directory(rscript: Path) -> Path:
    completed = subprocess.run(
        [
            str(rscript),
            "--vanilla",
            "-e",
            "cat(system.file('ex', package='dismo'))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    path = Path(completed.stdout.strip())
    if not path.is_dir():
        pytest.skip("The installed dismo package has no example data")
    return path


@pytest.mark.integration
def test_bioclim_end_to_end(tmp_path: Path) -> None:
    discovered = Settings.from_env().rscript
    if discovered is None:
        pytest.skip("Rscript is not installed")
    examples = _example_directory(discovered)

    for name in ("bio1.grd", "bio1.gri", "bio12.grd", "bio12.gri", "bradypus.csv"):
        shutil.copy2(examples / name, tmp_path / name)

    settings = Settings(tmp_path, (tmp_path,), discovered, 120)
    store = ArtifactStore(tmp_path)
    bridge = RBridge(settings, store)
    predictors = [str(tmp_path / "bio1.grd"), str(tmp_path / "bio12.grd")]
    occurrences = str(tmp_path / "bradypus.csv")

    raster, _ = bridge.execute("inspect_raster", {"paths": predictors})
    assert raster["layers"] == 2

    background, _ = bridge.execute(
        "generate_background_points",
        {
            "mask_path": predictors[0],
            "n": 100,
            "presence_path": occurrences,
            "lon_col": "lon",
            "lat_col": "lat",
            "seed": 7,
        },
    )
    background_path = background["artifacts"][0]["path"]
    assert background["generated"] == 100

    partitioned, _ = bridge.execute(
        "partition_occurrences",
        {
            "points_path": occurrences,
            "k": 3,
            "lon_col": "lon",
            "lat_col": "lat",
            "seed": 7,
        },
    )
    folds_path = partitioned["artifacts"][0]["path"]
    fold_counts = {int(key): int(value) for key, value in partitioned["fold_counts"].items()}
    held_out = next(fold for fold, count in fold_counts.items() if count > 0)

    fitted, _ = bridge.execute(
        "fit_model",
        {
            "model_type": "bioclim",
            "predictor_paths": predictors,
            "predictor_manifest": build_predictor_manifest(predictors),
            "presence_path": occurrences,
            "lon_col": "lon",
            "lat_col": "lat",
            "folds_path": folds_path,
            "held_out_fold": held_out,
            "seed": 7,
        },
    )
    assert fitted["model_type"] == "bioclim"
    assert fitted["presence_rows"] == sum(
        count for fold, count in fold_counts.items() if fold != held_out
    )
    training_path = store.artifact_path(fitted["run_id"], "training_points")
    model_path = store.artifact_path(fitted["run_id"], "model")
    manifest_path = store.artifact_path(fitted["run_id"], "predictor_manifest")

    predicted, _ = bridge.execute(
        "predict_model",
        {
            "model_path": str(model_path),
            "predictor_paths": predictors,
            "predictor_manifest": build_predictor_manifest(predictors),
            "expected_predictor_manifest_path": str(manifest_path),
            "include_stats": True,
        },
    )
    assert Path(predicted["artifacts"][0]["path"]).is_file()

    evaluated, _ = bridge.execute(
        "evaluate_model",
        {
            "model_path": str(model_path),
            "predictor_paths": predictors,
            "presence_path": occurrences,
            "absence_path": background_path,
            "lon_col": "lon",
            "lat_col": "lat",
            "folds_path": folds_path,
            "test_fold": held_out,
            "training_points_path": str(training_path),
        },
    )
    assert 0 <= evaluated["auc"] <= 1
    assert Path(evaluated["artifacts"][0]["path"]).is_file()

    with pytest.raises(RBridgeError, match="overlaps"):
        bridge.execute(
            "evaluate_model",
            {
                "model_path": str(model_path),
                "predictor_paths": predictors,
                "expected_predictor_manifest_path": str(manifest_path),
                "presence_path": occurrences,
                "absence_path": background_path,
                "lon_col": "lon",
                "lat_col": "lat",
                "training_points_path": str(training_path),
            },
        )

    responses, _ = bridge.execute(
        "response_curves",
        {"model_path": str(model_path), "variables": ["bio1"]},
    )
    assert responses["rows"] == 100

    mess, _ = bridge.execute(
        "environmental_similarity",
        {
            "predictor_paths": predictors,
            "reference_points_path": occurrences,
            "lon_col": "lon",
            "lat_col": "lat",
            "include_stats": False,
        },
    )
    assert Path(mess["artifacts"][0]["path"]).is_file()

    overlap, _ = bridge.execute(
        "niche_overlap",
        {
            "first_prediction_path": predicted["artifacts"][0]["path"],
            "second_prediction_path": predicted["artifacts"][0]["path"],
            "statistic": "I",
        },
    )
    assert overlap["value"] == pytest.approx(1.0)

    range_model, _ = bridge.execute(
        "fit_range_model",
        {
            "model_type": "convex_hull",
            "presence_path": occurrences,
            "lon_col": "lon",
            "lat_col": "lat",
            "lonlat": True,
            "clusters": 1,
            "seed": 7,
        },
    )
    assert store.artifact_path(range_model["run_id"], "range_geometry").is_file()

    rectangular, _ = bridge.execute(
        "fit_range_model",
        {
            "model_type": "rectangular_hull",
            "presence_path": occurrences,
            "lon_col": "lon",
            "lat_col": "lat",
            "lonlat": True,
            "clusters": 1,
            "seed": 7,
        },
    )
    assert store.artifact_path(rectangular["run_id"], "range_geometry").is_file()

    circle_hull, _ = bridge.execute(
        "fit_range_model",
        {
            "model_type": "circle_hull",
            "presence_path": occurrences,
            "lon_col": "lon",
            "lat_col": "lat",
            "lonlat": True,
            "seed": 7,
        },
    )
    assert store.artifact_path(circle_hull["run_id"], "range_geometry").is_file()

    for model_type in ("domain", "mahal"):
        fitted_other, _ = bridge.execute(
            "fit_model",
            {
                "model_type": model_type,
                "predictor_paths": predictors,
                "presence_path": occurrences,
                "predictor_manifest": build_predictor_manifest(predictors),
                "lon_col": "lon",
                "lat_col": "lat",
                "seed": 7,
            },
        )
        assert fitted_other["model_type"] == model_type

    monthly = [predictors[0]] * 12
    biovars, _ = bridge.execute(
        "compute_biovars",
        {
            "precipitation_paths": monthly,
            "minimum_temperature_paths": monthly,
            "maximum_temperature_paths": monthly,
        },
    )
    assert Path(biovars["artifacts"][0]["path"]).is_file()

    with pytest.raises(RBridgeError, match="exactly 12 monthly layers"):
        bridge.execute(
            "compute_biovars",
            {
                "precipitation_paths": predictors,
                "minimum_temperature_paths": predictors,
                "maximum_temperature_paths": predictors,
            },
        )

    info, _ = bridge.execute("system_info", {})
    if info["maxent_available"]:
        maxent, _ = bridge.execute(
            "fit_model",
            {
                "model_type": "maxent",
                "predictor_paths": predictors,
                "presence_path": occurrences,
                "background_path": background_path,
                "lon_col": "lon",
                "lat_col": "lat",
                "seed": 7,
            },
        )
        maxent_model = store.artifact_path(maxent["run_id"], "model")
        maxent_prediction, _ = bridge.execute(
            "predict_model",
            {
                "model_path": str(maxent_model),
                "predictor_paths": predictors,
                "include_stats": False,
            },
        )
        assert Path(maxent_prediction["artifacts"][0]["path"]).is_file()


@pytest.mark.integration
def test_projected_crs_validation_and_circles(tmp_path: Path) -> None:
    discovered = Settings.from_env().rscript
    if discovered is None:
        pytest.skip("Rscript is not installed")

    raster_path = tmp_path / "projected.tif"
    create_raster = (
        "library(raster); "
        "r <- raster(nrows=10, ncols=10, xmn=-100000, xmx=100000, "
        "ymn=-100000, ymx=100000, crs='EPSG:3857'); "
        "values(r) <- seq_len(ncell(r)); "
        f"writeRaster(r, '{raster_path.as_posix()}', format='GTiff', overwrite=TRUE)"
    )
    subprocess.run(
        [str(discovered), "--vanilla", "-e", create_raster],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    geographic_points = tmp_path / "geographic.csv"
    geographic_points.write_text("lon,lat\n0,0\n0.1,0.1\n", encoding="utf-8")
    invalid_points = tmp_path / "invalid.csv"
    invalid_points.write_text("lon,lat\n10,120\n", encoding="utf-8")
    projected_points = tmp_path / "projected.csv"
    projected_points.write_text(
        "lon,lat\n0,0\n20000,0\n0,20000\n20000,20000\n",
        encoding="utf-8",
    )

    settings = Settings(tmp_path, (tmp_path,), discovered, 120)
    store = ArtifactStore(tmp_path)
    bridge = RBridge(settings, store)

    extracted, _ = bridge.execute(
        "extract_predictor_values",
        {
            "predictor_paths": [str(raster_path)],
            "points_path": str(geographic_points),
            "lon_col": "lon",
            "lat_col": "lat",
            "point_crs": "EPSG:4326",
        },
    )
    assert extracted["complete_predictor_rows"] == 2

    with pytest.raises(RBridgeError, match="outside longitude"):
        bridge.execute(
            "inspect_points",
            {
                "path": str(invalid_points),
                "lon_col": "lon",
                "lat_col": "lat",
                "point_crs": "EPSG:4326",
            },
        )

    circles, _ = bridge.execute(
        "fit_range_model",
        {
            "model_type": "circles",
            "presence_path": str(projected_points),
            "lon_col": "lon",
            "lat_col": "lat",
            "lonlat": False,
            "crs": "EPSG:3857",
            "distance": 5000,
            "seed": 7,
        },
    )
    geometry_path = store.artifact_path(circles["run_id"], "range_geometry")
    read_crs = (
        "library(terra); "
        f"info <- crs(vect('{geometry_path.as_posix()}'), describe=TRUE); "
        "cat(info$code)"
    )
    completed = subprocess.run(
        [str(discovered), "--vanilla", "-e", read_crs],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.stdout.strip() == "3857"

    projected_test_points = tmp_path / "projected_test.csv"
    projected_test_points.write_text(
        "lon,lat\n5000,5000\n-5000,-5000\n",
        encoding="utf-8",
    )
    server = create_server(settings)
    components = server._local_provider.__dict__["_components"]
    generate_background = components["tool:generate_background_points@"].fn
    generated = generate_background(
        mask_path="projected.tif",
        n=10,
        presence_path="projected.csv",
        lon_col="lon",
        lat_col="lat",
        point_crs="EPSG:3857",
        seed=7,
    )
    fit_sdm = components["tool:fit_sdm@"].fn
    fitted = fit_sdm(
        model_type=EnvelopeModel.DOMAIN,
        predictor_paths=["projected.tif"],
        presence_path="projected.csv",
        background_run_id=generated["run_id"],
        point_crs="EPSG:3857",
        seed=7,
    )
    evaluate_sdm = components["tool:evaluate_sdm@"].fn
    evaluated = evaluate_sdm(
        model_run_id=fitted["run_id"],
        predictor_paths=["projected.tif"],
        presence_path="projected_test.csv",
        absence_run_id=generated["run_id"],
        point_crs="EPSG:3857",
    )
    assert 0 <= evaluated["auc"] <= 1
