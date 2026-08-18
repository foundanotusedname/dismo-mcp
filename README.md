# dismo-mcp

`dismo-mcp` exposes the official R [`rspatial/dismo`](https://github.com/rspatial/dismo)
species distribution modeling package through Model Context Protocol (MCP).

The server uses FastMCP 3 for the protocol layer and a process-isolated, fixed-operation
R bridge. It never exposes arbitrary R evaluation. Inputs are restricted to the configured
workspace/allowlist, and generated models, rasters, tables, and plots are addressed by
immutable `run_id` values.

## Quick start

Prerequisites:

- Python 3.11 or newer and `uv`
- R 3.6.3 or newer
- R packages `dismo`, `raster`, `sp`, `terra`, and `jsonlite`
- Optional: `rJava`/Java for MaxEnt; `gbm`, `randomForest`, `kernlab`, and `ROCR`
  for optional ecosystem features

```powershell
uv sync --extra dev
uv run dismo-mcp
```

FastMCP defaults to stdio, which is appropriate for Codex Desktop, Claude Desktop, and
other local MCP clients. A Streamable HTTP server can also be started explicitly:

```powershell
uv run dismo-mcp --transport http --host 127.0.0.1 --port 8000
```

HTTP transport is authenticated and binds only to loopback. Keep the secret in the
environment instead of a command-line argument, then place an HTTPS reverse proxy in
front of the local listener:

```powershell
$env:DISMO_MCP_BEARER_TOKEN = [Convert]::ToHexString(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
)
uv run dismo-mcp --transport http --host 127.0.0.1 --port 8000
```

`DISMO_MCP_BEARER_TOKEN` grants both `dismo:read` and `dismo:write`. For least privilege,
configure `DISMO_MCP_READ_TOKEN` and `DISMO_MCP_WRITE_TOKEN` separately. Tokens must be at
least 32 characters. Requests without a valid token receive `401`, detailed server
exceptions are masked by default, and direct non-loopback binding is rejected. Set
`DISMO_MCP_PUBLIC_BASE_URL=https://...` when the reverse proxy is public.

Example MCP client configuration on this machine:

```json
{
  "mcpServers": {
    "dismo": {
      "command": "uv",
      "args": ["--directory", "F:\\DISMO", "run", "dismo-mcp"],
      "env": {
        "DISMO_MCP_WORKSPACE": "F:\\DISMO"
      }
    }
  }
}
```

Run `dismo_system_info` first. It reports the resolved R executable, package versions,
and whether MaxEnt is usable.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISMO_MCP_WORKSPACE` | current directory | Input root and generated artifact store |
| `DISMO_MCP_ALLOWED_ROOTS` | workspace only | Extra input roots, separated by OS path separator |
| `DISMO_MCP_RSCRIPT` | auto-detected | Absolute path to `Rscript` |
| `DISMO_MCP_TIMEOUT_SECONDS` | `600` | Per-operation R timeout |
| `DISMO_MCP_TRANSPORT` | `stdio` | `stdio`, `http`, `streamable-http`, or `sse` |
| `DISMO_MCP_HOST` / `DISMO_MCP_PORT` | `127.0.0.1` / `8000` | Network binding |
| `DISMO_MCP_BEARER_TOKEN` | unset | Full read/write token for HTTP |
| `DISMO_MCP_READ_TOKEN` | unset | Read-only HTTP token |
| `DISMO_MCP_WRITE_TOKEN` | unset | Read/write HTTP token |
| `DISMO_MCP_PUBLIC_BASE_URL` | local HTTP URL | HTTPS public resource URL behind a proxy |
| `DISMO_MCP_MAX_CONCURRENT_R` | `2` | Maximum simultaneous Rscript processes |
| `DISMO_MCP_MAX_QUEUED_R` | `8` | Maximum queued R operations |
| `DISMO_MCP_QUEUE_WAIT_SECONDS` | `30` | Maximum queue wait before failure |
| `DISMO_MCP_MAX_RASTER_CELLS` | `50000000` | Maximum raster cells times layers |
| `DISMO_MCP_MAX_POINT_ROWS` | `1000000` | Maximum point-table rows |
| `DISMO_MCP_MAX_INPUT_BYTES` | `2000000000` | Maximum input file size |
| `DISMO_MCP_MAX_R_MEMORY_MB` | `4096` | R vector-heap memory limit |

Run artifacts are stored under `.dismo-mcp/runs/<run_id>/`. Model-consuming tools take a
`model_run_id`, preventing clients from substituting an arbitrary serialized object.

Environmental point tools accept `point_crs` (default `EPSG:4326`); range models use
`crs`. Geographic coordinates are checked
against longitude `[-180, 180]` and latitude `[-90, 90]`; projected points are transformed
to the predictor raster CRS, and rasters without a CRS are rejected for spatial matching.

## Tool groups

| Workflow stage | MCP tools | `dismo`/R basis |
| --- | --- | --- |
| Diagnostics | `dismo_system_info` | package/runtime checks, `maxent()` probe |
| Input QA | `inspect_raster`, `inspect_occurrences` | `raster::stack`, coordinate validation |
| Data preparation | `extract_predictor_values`, `generate_background_points`, `partition_occurrences` | `extract`, `randomPoints`, `kfold` |
| Environmental models | `fit_sdm` with optional training folds | `bioclim`, `domain`, `mahal`, `maxent` |
| Geographic range models | `fit_range_model` | `convHull`, `rectHull`, `circleHull`, `circles` |
| Inference | `predict_sdm` | `predict` methods for `DistModel` classes |
| Validation | `evaluate_sdm` with held-out fold selection | `evaluate`, `threshold` |
| Interpretation | `create_response_curves`, `calculate_mess` | `response`, `mess` |
| Comparison/climate | `calculate_niche_overlap`, `compute_bioclimatic_variables` | `nicheOverlap`, `biovars` |
| Provenance/control | `list_runs`, `get_run`, `cancel_run` | MCP-owned run metadata and cancellation |

The server also provides `dismo://capabilities`, `dismo://runs/{run_id}`, and the
`species_distribution_workflow` prompt.

For held-out evaluation, call `partition_occurrences`, then pass its `folds.csv` artifact
to `fit_sdm` with `held_out_fold` (or explicit `train_folds`). Pass the same artifact and
`test_fold` to `evaluate_sdm`. The model run records a `training_points` artifact, and
evaluation rejects overlapping presence coordinates unless the caller explicitly sets
`allow_training_overlap=true`.

Generated background points carry their raster `output_crs`. Use the resulting run ID as
`background_run_id` in `fit_sdm` and `absence_run_id` in `evaluate_sdm` to inherit that CRS.
File-based background/absence inputs must provide `background_crs` / `absence_crs`
explicitly. Fitted models also store a predictor manifest containing SHA-256 hashes,
sidecar files, layer names, extent, resolution, and CRS; prediction and evaluation reject
any changed predictor dataset.

R work is bounded by `DISMO_MCP_MAX_CONCURRENT_R`, `DISMO_MCP_MAX_QUEUED_R`,
`DISMO_MCP_MAX_RASTER_CELLS`, `DISMO_MCP_MAX_POINT_ROWS`, and
`DISMO_MCP_MAX_INPUT_BYTES` / `DISMO_MCP_MAX_R_MEMORY_MB`. A running or queued run can be
stopped with `cancel_run`.

## Design boundary

The official package exports legacy web helpers (`gbif`, `geocode`, `gmap`), interactive
plotting helpers, internal BRT utilities, and low-level geographic functions. They are not
individual MCP tools in the first stable surface:

- `gbif`, `geocode`, and `gmap` depend on aging third-party API contracts and credentials.
- Plot-only functions do not return stable structured results; response curves are instead
  exported as both CSV and PDF artifacts.
- Arbitrary function dispatch would erase the input schema and filesystem security boundary.
- BRT and other generic R learners need a separate, explicit tabular-model contract rather
  than pretending they behave like `DistModel` classes.

The architecture and upstream analysis are recorded in
[`docs/architecture.md`](docs/architecture.md).
