args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2) {
  stop("Usage: bridge.R <request.json> <response.json>")
}

request_path <- args[[1]]
response_path <- args[[2]]

if (!requireNamespace("jsonlite", quietly = TRUE)) {
  stop("The R package 'jsonlite' is required by dismo-mcp")
}

write_envelope <- function(value) {
  jsonlite::write_json(
    value,
    response_path,
    auto_unbox = TRUE,
    pretty = TRUE,
    null = "null",
    na = "null",
    digits = NA
  )
}

artifact <- function(path, media_type, role) {
  info <- file.info(path)
  list(
    name = basename(path),
    path = normalizePath(path, winslash = "/", mustWork = TRUE),
    media_type = media_type,
    role = role,
    size_bytes = unname(info$size)
  )
}

require_core <- function() {
  missing <- c("dismo", "raster", "sp", "terra")[
    !vapply(c("dismo", "raster", "sp", "terra"), requireNamespace, logical(1), quietly = TRUE)
  ]
  if (length(missing)) {
    stop("Missing required R packages: ", paste(missing, collapse = ", "))
  }
}

param <- function(params, name, default = NULL) {
  value <- params[[name]]
  if (is.null(value)) default else value
}

as_flag <- function(value, default = FALSE) {
  if (is.null(value) || !length(value)) default else isTRUE(value[[1]])
}

raster_cell_limit <- function() {
  value <- suppressWarnings(as.numeric(Sys.getenv("DISMO_MCP_MAX_RASTER_CELLS", "50000000")))
  if (!is.finite(value) || value < 1) 50000000 else value
}

check_raster_size <- function(x, label = "raster") {
  cells <- as.double(raster::ncell(x)) * as.double(raster::nlayers(x))
  if (cells > raster_cell_limit()) {
    stop(label, " exceeds the configured raster cell limit (", raster_cell_limit(), ")")
  }
  invisible(x)
}

predictor_geometry <- function(x) {
  ext <- raster::extent(x)
  list(
    layers = raster::nlayers(x),
    layer_names = names(x),
    rows = raster::nrow(x),
    columns = raster::ncol(x),
    resolution = unname(raster::res(x)),
    extent = c(ext@xmin, ext@xmax, ext@ymin, ext@ymax),
    crs = as.character(raster::crs(x))
  )
}

write_predictor_manifest <- function(params, x, run_dir) {
  manifest <- param(params, "predictor_manifest", list())
  if (!is.list(manifest)) stop("predictor_manifest must be an object")
  manifest$geometry <- predictor_geometry(x)
  manifest$created_by <- "dismo-mcp-r-bridge"
  manifest_path <- file.path(run_dir, "predictor_manifest.json")
  jsonlite::write_json(manifest, manifest_path, auto_unbox = TRUE, pretty = TRUE, null = "null")
  manifest_path
}

validate_predictor_manifest <- function(params, x) {
  manifest_path <- param(params, "expected_predictor_manifest_path")
  if (is.null(manifest_path)) return(invisible(TRUE))
  if (!file.exists(manifest_path)) stop("Predictor manifest artifact is missing")
  manifest <- jsonlite::fromJSON(manifest_path, simplifyVector = FALSE)
  expected <- manifest$geometry
  if (is.null(expected)) stop("Predictor manifest has no geometry")
  actual <- predictor_geometry(x)
  if (!identical(as.integer(expected$layers), as.integer(actual$layers)) ||
      !identical(as.integer(expected$rows), as.integer(actual$rows)) ||
      !identical(as.integer(expected$columns), as.integer(actual$columns))) {
    stop("Predictor raster layer count or geometry differs from the model manifest")
  }
  if (!identical(as.character(unlist(expected$layer_names)), as.character(actual$layer_names))) {
    stop("Predictor raster layer names differ from the model manifest")
  }
  expected_resolution <- as.numeric(unlist(expected$resolution))
  expected_extent <- as.numeric(unlist(expected$extent))
  if (max(abs(expected_resolution - as.numeric(actual$resolution))) > 1e-9 ||
      max(abs(expected_extent - as.numeric(actual$extent))) > 1e-8) {
    stop("Predictor raster extent or resolution differs from the model manifest")
  }
  expected_crs <- validated_crs(expected$crs, "manifest CRS")
  actual_crs <- validated_crs(actual$crs, "predictor CRS")
  expected_probe <- terra::vect(
    data.frame(x = 0, y = 0), geom = c("x", "y"), crs = expected_crs$value
  )
  actual_probe <- terra::vect(
    data.frame(x = 0, y = 0), geom = c("x", "y"), crs = actual_crs$value
  )
  if (!terra::same.crs(expected_probe, actual_probe)) {
    stop("Predictor raster CRS differs from the model manifest")
  }
  invisible(TRUE)
}

validated_crs <- function(value, label = "point_crs") {
  if (is.null(value) || !length(value) || is.na(value[[1]]) || !nzchar(trimws(value[[1]]))) {
    stop(label, " must be a non-empty CRS such as EPSG:4326")
  }
  value <- as.character(value[[1]])
  probe <- suppressWarnings(terra::vect(
    data.frame(x = 0, y = 0), geom = c("x", "y"), crs = value
  ))
  resolved <- terra::crs(probe)
  if (is.na(resolved) || !nzchar(resolved)) stop("Invalid ", label, ": ", value)
  list(value = value, resolved = resolved, lonlat = isTRUE(terra::is.lonlat(probe)))
}

predictor_crs <- function(x) {
  value <- as.character(raster::crs(x))
  if (!length(value) || is.na(value[[1]]) || !nzchar(value[[1]])) {
    stop("Predictor raster CRS is missing; spatial point operations require an explicit raster CRS")
  }
  validated_crs(value[[1]], "predictor CRS")$value
}

point_keys <- function(coords) {
  if (!nrow(coords)) return(character())
  paste(sprintf("%.12g", coords[, 1]), sprintf("%.12g", coords[, 2]), sep = "|")
}

same_point_set <- function(first, second) {
  identical(sort(point_keys(first)), sort(point_keys(second)))
}

read_points <- function(
  path,
  lon_col = "lon",
  lat_col = "lat",
  point_crs = "EPSG:4326",
  target_crs = NULL,
  allow_empty = FALSE
) {
  if (!requireNamespace("terra", quietly = TRUE)) {
    stop("The R package 'terra' is required for CRS validation and coordinate conversion")
  }
  data <- utils::read.csv(path, check.names = FALSE, stringsAsFactors = FALSE)
  point_limit <- suppressWarnings(as.numeric(Sys.getenv("DISMO_MCP_MAX_POINT_ROWS", "1000000")))
  if (is.finite(point_limit) && nrow(data) > point_limit) {
    stop("Point table exceeds the configured row limit (", point_limit, ")")
  }
  if (!(lon_col %in% names(data))) stop("Longitude column not found: ", lon_col)
  if (!(lat_col %in% names(data))) stop("Latitude column not found: ", lat_col)

  lon <- suppressWarnings(as.numeric(data[[lon_col]]))
  lat <- suppressWarnings(as.numeric(data[[lat_col]]))
  valid <- is.finite(lon) & is.finite(lat)
  coords <- cbind(lon[valid], lat[valid])
  colnames(coords) <- c(lon_col, lat_col)
  if (!allow_empty && nrow(coords) == 0) stop("No valid coordinate rows found")
  source <- validated_crs(point_crs)
  if (source$lonlat && nrow(coords)) {
    outside <- coords[, 1] < -180 | coords[, 1] > 180 | coords[, 2] < -90 | coords[, 2] > 90
    if (any(outside)) {
      rows <- which(valid)[outside]
      stop(
        "Geographic coordinates outside longitude [-180, 180] or latitude [-90, 90] ",
        "at CSV row(s): ", paste(rows + 1L, collapse = ", ")
      )
    }
  }
  output_crs <- source$value
  if (nrow(coords)) {
    vector <- terra::vect(
      data.frame(x = coords[, 1], y = coords[, 2]),
      geom = c("x", "y"),
      crs = source$value
    )
    if (!is.null(target_crs)) {
      target <- validated_crs(target_crs, "target CRS")
      if (!terra::same.crs(vector, target$value)) {
        vector <- terra::project(vector, target$value)
      }
      output_crs <- target$value
    }
    coords <- terra::crds(vector)
    colnames(coords) <- c(lon_col, lat_col)
    if (any(!is.finite(coords))) stop("Coordinate transformation produced non-finite values")
  }
  list(
    data = data,
    valid_data = data[valid, , drop = FALSE],
    coords = coords,
    valid = valid,
    invalid_rows = which(!valid),
    total_rows = nrow(data),
    point_crs = source$value,
    coordinate_crs = output_crs,
    lonlat = source$lonlat
  )
}

load_predictors <- function(paths) {
  require_core()
  paths <- as.character(unlist(paths, use.names = FALSE))
  if (!length(paths)) stop("At least one predictor raster path is required")
  x <- raster::stack(paths)
  names(x) <- make.names(names(x), unique = TRUE)
  check_raster_size(x, "Predictor raster")
  x
}

raster_metadata <- function(x, include_stats = FALSE) {
  ext <- raster::extent(x)
  result <- list(
    class = class(x)[[1]],
    layers = raster::nlayers(x),
    layer_names = names(x),
    rows = raster::nrow(x),
    columns = raster::ncol(x),
    cells = raster::ncell(x),
    resolution = unname(raster::res(x)),
    extent = list(
      xmin = ext@xmin,
      xmax = ext@xmax,
      ymin = ext@ymin,
      ymax = ext@ymax
    ),
    crs = as.character(raster::crs(x)),
    in_memory = raster::inMemory(x)
  )
  if (isTRUE(include_stats)) {
    result$statistics <- lapply(seq_len(raster::nlayers(x)), function(i) {
      layer <- x[[i]]
      list(
        name = names(x)[[i]],
        minimum = unname(raster::cellStats(layer, "min", na.rm = TRUE)),
        maximum = unname(raster::cellStats(layer, "max", na.rm = TRUE)),
        mean = unname(raster::cellStats(layer, "mean", na.rm = TRUE))
      )
    })
  }
  result
}

write_raster <- function(x, path) {
  if (!file.exists(path)) {
    x <- raster::writeRaster(x, path, format = "GTiff", overwrite = TRUE)
  }
  x
}

system_info <- function(params, run_dir) {
  packages <- c(
    "dismo", "raster", "sp", "terra", "jsonlite", "rJava",
    "gbm", "randomForest", "kernlab", "ROCR"
  )
  status <- lapply(packages, function(package) {
    installed <- requireNamespace(package, quietly = TRUE)
    list(
      package = package,
      installed = installed,
      version = if (installed) as.character(utils::packageVersion(package)) else NULL
    )
  })
  names(status) <- NULL

  maxent_available <- FALSE
  if (requireNamespace("dismo", quietly = TRUE)) {
    maxent_available <- isTRUE(tryCatch(dismo::maxent(silent = TRUE), error = function(e) FALSE))
  }
  list(
    r_version = R.version.string,
    platform = R.version$platform,
    library_paths = .libPaths(),
    packages = status,
    maxent_available = maxent_available
  )
}

inspect_raster <- function(params, run_dir) {
  x <- load_predictors(param(params, "paths"))
  raster_metadata(x, as_flag(param(params, "include_stats")))
}

inspect_points <- function(params, run_dir) {
  points <- read_points(
    param(params, "path"),
    param(params, "lon_col", "lon"),
    param(params, "lat_col", "lat"),
    param(params, "point_crs", "EPSG:4326"),
    target_crs = NULL,
    allow_empty = TRUE
  )
  coords <- points$coords
  bbox <- if (nrow(coords)) {
    list(
      xmin = min(coords[, 1]), xmax = max(coords[, 1]),
      ymin = min(coords[, 2]), ymax = max(coords[, 2])
    )
  } else {
    NULL
  }
  list(
    rows = points$total_rows,
    valid_coordinate_rows = nrow(coords),
    invalid_coordinate_rows = length(points$invalid_rows),
    invalid_row_numbers = as.integer(points$invalid_rows),
    duplicate_coordinates = if (nrow(coords)) sum(duplicated(as.data.frame(coords))) else 0L,
    columns = names(points$data),
    bbox = bbox,
    point_crs = points$point_crs,
    longitude_latitude = points$lonlat
  )
}

extract_predictor_values <- function(params, run_dir) {
  x <- load_predictors(param(params, "predictor_paths"))
  points <- read_points(
    param(params, "points_path"),
    param(params, "lon_col", "lon"),
    param(params, "lat_col", "lat"),
    param(params, "point_crs", "EPSG:4326"),
    predictor_crs(x)
  )
  values <- as.data.frame(raster::extract(x, points$coords))
  output <- cbind(points$valid_data, values)
  output_path <- file.path(run_dir, "predictor_values.csv")
  utils::write.csv(output, output_path, row.names = FALSE, na = "")
  complete <- stats::complete.cases(values)
  list(
    rows = nrow(output),
    complete_predictor_rows = sum(complete),
    incomplete_predictor_rows = sum(!complete),
    predictor_names = names(x),
    predictor_crs = points$coordinate_crs,
    artifacts = list(artifact(output_path, "text/csv", "predictor_values"))
  )
}

generate_background_points <- function(params, run_dir) {
  require_core()
  mask <- raster::raster(param(params, "mask_path"))
  check_raster_size(mask, "Background mask raster")
  n <- as.integer(param(params, "n", 1000L))
  if (n < 1L) stop("n must be positive")
  seed <- as.integer(param(params, "seed", 1L))
  set.seed(seed)

  presence_path <- param(params, "presence_path")
  presence <- NULL
  if (!is.null(presence_path)) {
    presence <- read_points(
      presence_path,
      param(params, "lon_col", "lon"),
      param(params, "lat_col", "lat"),
      param(params, "point_crs", "EPSG:4326"),
      predictor_crs(mask)
    )$coords
  }
  extent_values <- param(params, "extent")
  ext <- if (is.null(extent_values)) NULL else raster::extent(as.numeric(unlist(extent_values)))

  background <- dismo::randomPoints(
    mask = mask,
    n = n,
    p = presence,
    ext = ext,
    excludep = as_flag(param(params, "exclude_presence"), TRUE),
    prob = as_flag(param(params, "probability_weighted"), FALSE),
    lonlatCorrection = as_flag(param(params, "lonlat_correction"), TRUE),
    warn = 2
  )
  output_path <- file.path(run_dir, "background.csv")
  output <- data.frame(lon = background[, 1], lat = background[, 2])
  utils::write.csv(output, output_path, row.names = FALSE)
  list(
    requested = n,
    generated = nrow(output),
    seed = seed,
    output_crs = predictor_crs(mask),
    artifacts = list(artifact(output_path, "text/csv", "background_points"))
  )
}

partition_occurrences <- function(params, run_dir) {
  require_core()
  points <- read_points(
    param(params, "points_path"),
    param(params, "lon_col", "lon"),
    param(params, "lat_col", "lat"),
    param(params, "point_crs", "EPSG:4326")
  )
  k <- as.integer(param(params, "k", 5L))
  if (k < 2L) stop("k must be at least 2")
  seed <- as.integer(param(params, "seed", 1L))
  set.seed(seed)
  by_col <- param(params, "by_col")
  by <- NULL
  if (!is.null(by_col)) {
    if (!(by_col %in% names(points$valid_data))) stop("Grouping column not found: ", by_col)
    by <- points$valid_data[[by_col]]
  }
  folds <- dismo::kfold(points$valid_data, k = k, by = by)
  output <- points$valid_data
  output$fold <- folds
  output_path <- file.path(run_dir, "folds.csv")
  utils::write.csv(output, output_path, row.names = FALSE)
  counts <- as.list(as.integer(table(factor(folds, levels = seq_len(k)))))
  names(counts) <- as.character(seq_len(k))
  list(
    rows = nrow(output),
    k = k,
    seed = seed,
    point_crs = points$point_crs,
    fold_counts = counts,
    artifacts = list(artifact(output_path, "text/csv", "fold_assignments"))
  )
}

fit_model <- function(params, run_dir) {
  require_core()
  model_type <- as.character(param(params, "model_type"))
  supported <- c("bioclim", "domain", "mahal", "maxent")
  if (!(model_type %in% supported)) {
    stop("Unsupported model_type. Choose one of: ", paste(supported, collapse = ", "))
  }
  x <- load_predictors(param(params, "predictor_paths"))
  target_crs <- predictor_crs(x)
  points <- read_points(
    param(params, "presence_path"),
    param(params, "lon_col", "lon"),
    param(params, "lat_col", "lat"),
    param(params, "point_crs", "EPSG:4326"),
    target_crs
  )
  training_points <- points
  selected_folds <- NULL
  folds_path <- param(params, "folds_path")
  train_folds <- as.integer(unlist(param(params, "train_folds", integer()), use.names = FALSE))
  held_out_fold <- param(params, "held_out_fold")
  if (length(train_folds) && !is.null(held_out_fold)) {
    stop("Use train_folds or held_out_fold, not both")
  }
  if (!is.null(folds_path)) {
    if (!length(train_folds) && is.null(held_out_fold)) {
      stop("folds_path requires train_folds or held_out_fold")
    }
    fold_points <- read_points(
      folds_path,
      param(params, "lon_col", "lon"),
      param(params, "lat_col", "lat"),
      param(params, "point_crs", "EPSG:4326"),
      target_crs
    )
    if (!same_point_set(points$coords, fold_points$coords)) {
      stop("folds_path coordinates do not match presence_path")
    }
    if (!("fold" %in% names(fold_points$valid_data))) {
      stop("folds_path must contain a fold column")
    }
    fold_values <- suppressWarnings(as.numeric(fold_points$valid_data$fold))
    if (any(!is.finite(fold_values)) || any(fold_values < 1) || any(fold_values != floor(fold_values))) {
      stop("fold values must be positive integers")
    }
    selected <- if (length(train_folds)) {
      if (any(train_folds < 1L)) stop("train_folds must contain positive integers")
      fold_values %in% unique(train_folds)
    } else {
      held_out_fold <- as.integer(held_out_fold[[1]])
      if (!(held_out_fold %in% fold_values)) stop("held_out_fold is not present in folds_path")
      fold_values != held_out_fold
    }
    if (!any(selected)) stop("Training fold selection produced no presence points")
    training_points <- fold_points
    training_points$coords <- fold_points$coords[selected, , drop = FALSE]
    training_points$valid_data <- fold_points$valid_data[selected, , drop = FALSE]
    selected_folds <- sort(unique(as.integer(fold_values[selected])))
  } else if (length(train_folds) || !is.null(held_out_fold)) {
    stop("folds_path is required when selecting training folds")
  }
  p <- training_points$coords
  seed <- as.integer(param(params, "seed", 1L))
  set.seed(seed)

  model <- switch(
    model_type,
    bioclim = dismo::bioclim(x, p),
    domain = dismo::domain(x, p),
    mahal = dismo::mahal(x, p),
    maxent = {
      background <- NULL
      background_path <- param(params, "background_path")
      if (!is.null(background_path)) {
        background <- read_points(
          background_path,
          param(params, "lon_col", "lon"),
          param(params, "lat_col", "lat"),
          param(params, "background_crs", param(params, "point_crs", "EPSG:4326")),
          target_crs
        )$coords
      }
      factors <- as.character(unlist(param(params, "factors", character()), use.names = FALSE))
      maxent_args <- as.character(unlist(param(params, "maxent_args", character()), use.names = FALSE))
      maxent_dir <- file.path(run_dir, "maxent")
      dir.create(maxent_dir, recursive = TRUE, showWarnings = FALSE)
      do.call(
        dismo::maxent,
        list(
          x = x,
          p = p,
          a = background,
          factors = if (length(factors)) factors else NULL,
          removeDuplicates = as_flag(param(params, "remove_duplicates"), TRUE),
          nbg = as.integer(param(params, "background_count", 10000L)),
          args = if (length(maxent_args)) maxent_args else NULL,
          path = maxent_dir,
          silent = TRUE
        )
      )
    }
  )

  model_path <- file.path(run_dir, "model.rds")
  saveRDS(model, model_path)
  training_path <- file.path(run_dir, "training_points.csv")
  training_output <- data.frame(x = p[, 1], y = p[, 2], crs = target_crs)
  if ("fold" %in% names(training_points$valid_data)) {
    training_output$fold <- training_points$valid_data$fold
  }
  utils::write.csv(training_output, training_path, row.names = FALSE)
  manifest_path <- write_predictor_manifest(params, x, run_dir)
  list(
    model_type = model_type,
    model_class = class(model),
    presence_rows = nrow(p),
    predictor_names = names(x),
    predictor_crs = target_crs,
    training_folds = selected_folds,
    held_out_fold = if (is.null(held_out_fold)) NULL else as.integer(held_out_fold[[1]]),
    seed = seed,
    artifacts = list(
      artifact(model_path, "application/x-r-rds", "model"),
      artifact(training_path, "text/csv", "training_points"),
      artifact(manifest_path, "application/json", "predictor_manifest")
    )
  )
}

fit_range_model <- function(params, run_dir) {
  require_core()
  model_type <- as.character(param(params, "model_type"))
  supported <- c("convex_hull", "rectangular_hull", "circle_hull", "circles")
  if (!(model_type %in% supported)) {
    stop("Unsupported range model. Choose one of: ", paste(supported, collapse = ", "))
  }
  lonlat <- as_flag(param(params, "lonlat"), TRUE)
  crs <- param(params, "crs")
  if (is.null(crs)) {
    if (!lonlat) stop("crs is required when lonlat is false")
    crs <- "EPSG:4326"
  }
  points <- read_points(
    param(params, "presence_path"),
    param(params, "lon_col", "lon"),
    param(params, "lat_col", "lat"),
    crs
  )
  if (!identical(lonlat, points$lonlat)) {
    stop("lonlat does not match the declared point CRS")
  }
  p <- points$coords
  clusters <- as.integer(param(params, "clusters", 1L))
  seed <- as.integer(param(params, "seed", 1L))
  set.seed(seed)

  model <- switch(
    model_type,
    convex_hull = dismo::convHull(p, n = clusters, crs = crs),
    rectangular_hull = dismo::rectHull(p, n = clusters, crs = crs),
    circle_hull = dismo::circleHull(p, crs = crs),
    circles = {
      distance <- param(params, "distance")
      result <- if (is.null(distance)) {
        dismo::circles(p, lonlat = lonlat)
      } else {
        dismo::circles(p, d = as.numeric(distance), lonlat = lonlat)
      }
      sp::proj4string(result@polygons) <- sp::CRS(crs)
      result
    }
  )
  model_path <- file.path(run_dir, "model.rds")
  saveRDS(model, model_path)
  geometry_path <- file.path(run_dir, "range.gpkg")
  if (requireNamespace("terra", quietly = TRUE)) {
    geometry <- terra::vect(model@polygons)
    terra::crs(geometry) <- crs
    terra::writeVector(geometry, geometry_path, overwrite = TRUE)
  }
  artifacts <- list(artifact(model_path, "application/x-r-rds", "model"))
  if (file.exists(geometry_path)) {
    artifacts <- append(artifacts, list(artifact(geometry_path, "application/geopackage+sqlite3", "range_geometry")))
  }
  list(
    model_type = model_type,
    model_class = class(model),
    presence_rows = nrow(p),
    lonlat = lonlat,
    crs = terra::crs(terra::vect(model@polygons), proj = TRUE),
    artifacts = artifacts
  )
}

predict_model <- function(params, run_dir) {
  require_core()
  model <- readRDS(param(params, "model_path"))
  x <- load_predictors(param(params, "predictor_paths"))
  validate_predictor_manifest(params, x)
  output_path <- file.path(run_dir, "prediction.tif")
  prediction_args <- as.character(
    unlist(param(params, "prediction_args", character()), use.names = FALSE)
  )
  call_args <- list(object = model, x = x, filename = output_path, overwrite = TRUE)
  if (length(prediction_args)) call_args$args <- prediction_args
  prediction <- do.call(dismo::predict, call_args)
  prediction <- write_raster(prediction, output_path)
  list(
    model_class = class(model),
    raster = raster_metadata(prediction, as_flag(param(params, "include_stats"), TRUE)),
    artifacts = list(artifact(output_path, "image/tiff; application=geotiff", "prediction"))
  )
}

evaluate_model <- function(params, run_dir) {
  require_core()
  model <- readRDS(param(params, "model_path"))
  x <- load_predictors(param(params, "predictor_paths"))
  validate_predictor_manifest(params, x)
  target_crs <- predictor_crs(x)
  presence <- read_points(
    param(params, "presence_path"),
    param(params, "lon_col", "lon"),
    param(params, "lat_col", "lat"),
    param(params, "point_crs", "EPSG:4326"),
    target_crs
  )
  test_fold <- param(params, "test_fold")
  folds_path <- param(params, "folds_path")
  if (!is.null(test_fold)) {
    if (is.null(folds_path)) stop("folds_path is required when selecting a test fold")
    fold_points <- read_points(
      folds_path,
      param(params, "lon_col", "lon"),
      param(params, "lat_col", "lat"),
      param(params, "point_crs", "EPSG:4326"),
      target_crs
    )
    if (!same_point_set(presence$coords, fold_points$coords)) {
      stop("folds_path coordinates do not match presence_path")
    }
    if (!("fold" %in% names(fold_points$valid_data))) {
      stop("folds_path must contain a fold column")
    }
    fold_values <- suppressWarnings(as.numeric(fold_points$valid_data$fold))
    test_fold <- as.integer(test_fold[[1]])
    selected <- is.finite(fold_values) & fold_values == test_fold
    if (!any(selected)) stop("test_fold is not present in folds_path")
    presence <- fold_points
    presence$coords <- fold_points$coords[selected, , drop = FALSE]
    presence$valid_data <- fold_points$valid_data[selected, , drop = FALSE]
  } else if (!is.null(folds_path)) {
    stop("test_fold is required when folds_path is supplied for evaluation")
  }
  p <- presence$coords
  a <- read_points(
    param(params, "absence_path"),
    param(params, "lon_col", "lon"),
    param(params, "lat_col", "lat"),
    param(params, "absence_crs", param(params, "point_crs", "EPSG:4326")),
    target_crs
  )$coords
  allow_overlap <- as_flag(param(params, "allow_training_overlap"), FALSE)
  if (!allow_overlap) {
    training_path <- param(params, "training_points_path")
    if (is.null(training_path) || !file.exists(training_path)) {
      stop("training_points_path is required to verify held-out evaluation")
    }
    training <- utils::read.csv(training_path, check.names = FALSE)
    if (!all(c("x", "y", "crs") %in% names(training))) {
      stop("training_points artifact must contain x, y, and crs columns")
    }
    training_crs <- unique(training$crs[!is.na(training$crs) & nzchar(training$crs)])
    if (length(training_crs) != 1L) stop("training_points artifact has no single valid CRS")
    training_probe <- terra::vect(
      data.frame(x = 0, y = 0), geom = c("x", "y"), crs = training_crs[[1]]
    )
    if (!terra::same.crs(training_probe, target_crs)) {
      stop("Evaluation predictor CRS does not match the model training predictor CRS")
    }
    training_coords <- cbind(
      suppressWarnings(as.numeric(training$x)),
      suppressWarnings(as.numeric(training$y))
    )
    training_coords <- training_coords[stats::complete.cases(training_coords), , drop = FALSE]
    overlap <- intersect(unique(point_keys(p)), unique(point_keys(training_coords)))
    if (length(overlap)) {
      stop(
        "Evaluation presence data overlaps ", length(overlap),
        " training coordinate(s); use a held-out fold or explicitly set allow_training_overlap"
      )
    }
  }
  evaluation <- dismo::evaluate(p = p, a = a, model = model, x = x)
  thresholds <- dismo::threshold(evaluation)
  best_tss <- which.max(evaluation@TPR + evaluation@TNR)
  best_kappa <- which.max(evaluation@kappa)
  table <- data.frame(
    threshold = evaluation@t,
    sensitivity = evaluation@TPR,
    specificity = evaluation@TNR,
    tss = evaluation@TPR + evaluation@TNR - 1,
    kappa = evaluation@kappa,
    tp = evaluation@confusion[, "tp"],
    fp = evaluation@confusion[, "fp"],
    fn = evaluation@confusion[, "fn"],
    tn = evaluation@confusion[, "tn"]
  )
  output_path <- file.path(run_dir, "evaluation_thresholds.csv")
  utils::write.csv(table, output_path, row.names = FALSE)
  list(
    auc = unname(evaluation@auc),
    correlation = unname(evaluation@cor),
    presence_rows = evaluation@np,
    absence_rows = evaluation@na,
    test_fold = if (is.null(test_fold)) NULL else test_fold,
    training_overlap_allowed = allow_overlap,
    thresholds = as.list(thresholds[1, , drop = TRUE]),
    best_tss = list(
      threshold = evaluation@t[[best_tss]],
      value = evaluation@TPR[[best_tss]] + evaluation@TNR[[best_tss]] - 1,
      sensitivity = evaluation@TPR[[best_tss]],
      specificity = evaluation@TNR[[best_tss]]
    ),
    best_kappa = list(
      threshold = evaluation@t[[best_kappa]],
      value = evaluation@kappa[[best_kappa]]
    ),
    artifacts = list(artifact(output_path, "text/csv", "evaluation_thresholds"))
  )
}

response_curves <- function(params, run_dir) {
  require_core()
  model <- readRDS(param(params, "model_path"))
  variables <- as.character(unlist(param(params, "variables", character()), use.names = FALSE))
  available <- colnames(model@presence)
  if (!length(variables)) variables <- available
  unknown <- setdiff(variables, available)
  if (length(unknown)) stop("Unknown model variables: ", paste(unknown, collapse = ", "))

  plot_path <- file.path(run_dir, "response_curves.pdf")
  grDevices::pdf(plot_path, onefile = TRUE)
  on.exit(grDevices::dev.off(), add = TRUE)
  rows <- lapply(variables, function(variable) {
    curve <- dismo::response(model, var = variable, rug = FALSE)
    data.frame(variable = variable, value = curve[, 1], prediction = curve[, 2])
  })
  grDevices::dev.off()
  on.exit(NULL, add = FALSE)
  output <- do.call(rbind, rows)
  csv_path <- file.path(run_dir, "response_curves.csv")
  utils::write.csv(output, csv_path, row.names = FALSE)
  list(
    variables = variables,
    rows = nrow(output),
    artifacts = list(
      artifact(csv_path, "text/csv", "response_curve_data"),
      artifact(plot_path, "application/pdf", "response_curve_plot")
    )
  )
}

environmental_similarity <- function(params, run_dir) {
  require_core()
  x <- load_predictors(param(params, "predictor_paths"))
  points <- read_points(
    param(params, "reference_points_path"),
    param(params, "lon_col", "lon"),
    param(params, "lat_col", "lat"),
    param(params, "point_crs", "EPSG:4326"),
    predictor_crs(x)
  )
  values <- raster::extract(x, points$coords)
  values <- values[stats::complete.cases(values), , drop = FALSE]
  if (!nrow(values)) stop("No complete reference environmental values were extracted")
  output_path <- file.path(run_dir, "mess.tif")
  result <- dismo::mess(
    x,
    values,
    full = as_flag(param(params, "full"), FALSE),
    filename = output_path,
    format = "GTiff",
    overwrite = TRUE
  )
  result <- write_raster(result, output_path)
  list(
    reference_rows = nrow(values),
    raster = raster_metadata(result, as_flag(param(params, "include_stats"), TRUE)),
    artifacts = list(artifact(output_path, "image/tiff; application=geotiff", "mess"))
  )
}

niche_overlap <- function(params, run_dir) {
  require_core()
  x <- raster::raster(param(params, "first_prediction_path"))
  y <- raster::raster(param(params, "second_prediction_path"))
  check_raster_size(x, "First prediction raster")
  check_raster_size(y, "Second prediction raster")
  stat <- as.character(param(params, "statistic", "I"))
  value <- dismo::nicheOverlap(
    x,
    y,
    stat = stat,
    mask = as_flag(param(params, "mask"), TRUE),
    checkNegatives = as_flag(param(params, "check_negatives"), TRUE)
  )
  list(statistic = stat, value = unname(value))
}

compute_biovars <- function(params, run_dir) {
  require_core()
  precipitation <- load_predictors(param(params, "precipitation_paths"))
  minimum_temperature <- load_predictors(param(params, "minimum_temperature_paths"))
  maximum_temperature <- load_predictors(param(params, "maximum_temperature_paths"))
  layer_counts <- c(
    precipitation = raster::nlayers(precipitation),
    minimum_temperature = raster::nlayers(minimum_temperature),
    maximum_temperature = raster::nlayers(maximum_temperature)
  )
  if (any(layer_counts != 12L)) {
    stop(
      "BIOCLIM inputs must each contain exactly 12 monthly layers; received ",
      paste(names(layer_counts), layer_counts, sep = "=", collapse = ", ")
    )
  }
  output_path <- file.path(run_dir, "biovars.tif")
  result <- dismo::biovars(
    precipitation,
    minimum_temperature,
    maximum_temperature,
    filename = output_path,
    format = "GTiff",
    overwrite = TRUE
  )
  result <- write_raster(result, output_path)
  list(
    raster = raster_metadata(result, as_flag(param(params, "include_stats"), FALSE)),
    artifacts = list(artifact(output_path, "image/tiff; application=geotiff", "bioclimatic_variables"))
  )
}

dispatch <- function(operation, params, run_dir) {
  switch(
    operation,
    system_info = system_info(params, run_dir),
    inspect_raster = inspect_raster(params, run_dir),
    inspect_points = inspect_points(params, run_dir),
    extract_predictor_values = extract_predictor_values(params, run_dir),
    generate_background_points = generate_background_points(params, run_dir),
    partition_occurrences = partition_occurrences(params, run_dir),
    fit_model = fit_model(params, run_dir),
    fit_range_model = fit_range_model(params, run_dir),
    predict_model = predict_model(params, run_dir),
    evaluate_model = evaluate_model(params, run_dir),
    response_curves = response_curves(params, run_dir),
    environmental_similarity = environmental_similarity(params, run_dir),
    niche_overlap = niche_overlap(params, run_dir),
    compute_biovars = compute_biovars(params, run_dir),
    stop("Unknown operation: ", operation)
  )
}

warnings <- character()
tryCatch(
  {
    request <- jsonlite::fromJSON(request_path, simplifyVector = TRUE)
    result <- withCallingHandlers(
      dispatch(request$operation, request$params, request$run_dir),
      warning = function(warning) {
        warnings <<- c(warnings, conditionMessage(warning))
        invokeRestart("muffleWarning")
      }
    )
    if (length(warnings)) result$warnings <- unique(warnings)
    write_envelope(list(ok = TRUE, result = result))
  },
  error = function(error) {
    write_envelope(
      list(
        ok = FALSE,
        error = list(
          class = class(error)[[1]],
          message = conditionMessage(error),
          call = if (is.null(conditionCall(error))) NULL else deparse(conditionCall(error))
        )
      )
    )
    quit(status = 1L)
  }
)
