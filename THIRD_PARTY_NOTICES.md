# Third-Party Notices

`dismo-mcp` is an independent MCP integration. This source repository does not
include or redistribute R, R packages, Java, `maxent.jar`, model artifacts,
example datasets, or user data.

The following components are installed separately by users when they choose to
use the corresponding functionality. Their licenses remain their own and are
not replaced by the MIT License for `dismo-mcp`.

| Component | Role | Upstream license |
| --- | --- | --- |
| [dismo](https://github.com/rspatial/dismo) | Species-distribution modeling runtime | GPL-3.0-or-later |
| [raster](https://github.com/rspatial/raster) | Legacy raster operations used by `dismo` | GPL-3.0-or-later |
| [terra](https://github.com/rspatial/terra) | Spatial data operations | GPL-3.0-or-later |
| [sp](https://cran.r-project.org/package=sp) | Spatial classes | GPL-2.0-or-later |
| [jsonlite](https://github.com/jeroen/jsonlite) | R JSON handling | MIT |
| [FastMCP](https://github.com/jlowin/fastmcp) | Python MCP framework | Apache-2.0 |
| [Pydantic](https://github.com/pydantic/pydantic) | Python data validation | MIT |

MaxEnt functionality is optional. Any Java runtime, `rJava`, and `maxent.jar`
needed for it are user-provided and are not part of this repository or its
Python package distribution. Users are responsible for obtaining and using
those components under their applicable terms.

This document is an informational dependency notice, not a grant of rights in
any third-party component. A future distribution that bundles a runtime (for
example, a container image or offline installer) must document the exact
versions and include the notices required by the bundled components.
