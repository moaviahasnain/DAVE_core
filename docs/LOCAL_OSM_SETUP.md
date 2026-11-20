# Local-First OSM Data Source (Germany-wide .osm.pbf)

## What this is

By default, DAVE_core retrieves OpenStreetMap data by querying the public
Overpass API for every requested area. This can be slow and is sometimes
unreliable (server timeouts, rate limiting, "no response from server").

This feature lets DAVE_core instead read OSM data from a locally stored,
Germany-wide .osm.pbf file. If the file is present and readable, it is
used automatically and the Overpass API is never contacted. If the file is
missing, unreadable, or the optional reader library isn't installed,
DAVE_core prints "OSM data locally not found" and transparently falls back
to the original Overpass-based request - no code changes are needed either
way, and no existing behavior changes if you don't set this up.

## One-time setup

### 1. Download a Germany-wide OSM extract

Download the file from Geofabrik (free, public):

https://download.geofabrik.de/europe/germany-latest.osm.pbf

This file is large (several GB) and is not stored in this git
repository - keep it anywhere on your own disk.

### 2. Install the optional dependencies

pip install dave_core[osm_local]

This installs osmium (the PBF reader) and pyarrow (used for the on-disk
cache described below). If you installed dave_core without the [osm_local]
extra already, you can instead run:

pip install osmium pyarrow

### 3. Point DAVE_core at your downloaded file

Set the DAVE_OSM_PBF_PATH environment variable to the full path of the
file you downloaded in step 1.

Windows (PowerShell), permanent for your user account:
setx DAVE_OSM_PBF_PATH "C:\path\to\germany-latest.osm.pbf"
Restart your terminal/IDE afterwards for the variable to take effect.

Linux / macOS, add to ~/.bashrc or ~/.zshrc:
export DAVE_OSM_PBF_PATH="/path/to/germany-latest.osm.pbf"
Then reload your shell (source ~/.bashrc) or open a new terminal.

That's it - no code or settings file changes needed. src/dave_core/settings.py
reads this environment variable automatically; if it's unset, DAVE_core
falls back to the normal Overpass behavior exactly as before this feature
existed.

## How it works

- The first time DAVE_core requests OSM data for a given area (e.g. a city),
  it scans the entire local .osm.pbf file once for that area's exact
  polygon shape. This is a full-file scan and can take 15-25+ minutes
  depending on your disk/CPU, since the file covers all of Germany.
- All OSM categories DAVE_core uses (roads, buildings, landuse, leisure,
  natural, railways, waterways) are extracted together in that single scan,
  not one scan per category.
- The result is cached to disk under data/osm/cache/<hash>/ (one folder
  per distinct area polygon, as .parquet files). This cache folder is
  gitignored - it's regenerated locally and is never committed.
- Any later request for the same area polygon is served from this disk
  cache almost instantly, without touching the .pbf file again.
- The cache automatically invalidates itself if you replace your .pbf
  file with a newer download (detected via the file's modification time).

### Expect roughly 2-3 full scans per new city

DAVE_core requests OSM data using a few different polygon shapes per area
(a plain boundary, a buffered boundary, and a separately-buffered shape for
natural features). Each distinct shape triggers its own one-time scan the
first time a new city is processed. Every later run for that same city
reuses the cache and completes in seconds.

## Known limitations (current state of this branch)

- LV (low-voltage) network connectivity: when generating a full city's
  electrical network (power_levels=["hv","mv","lv"]), the low-voltage
  steiner-tree step that connects buildings to the grid may currently fail
  silently for some or all network groups. A diagnostic fix has been added
  (topology_utils.py now prints "LV steiner tree failed for net group ..."
  with the real exception instead of swallowing it), but the underlying
  cause has not yet been fixed. If you hit this, please capture the printed
  exception messages - that's the next thing to resolve.
- High-pressure gas topology: create_hp_topology() currently raises
  TypeError: unhashable type: 'dict' inside gaslib_pipe_clustering()
  (src/dave_core/topology/high_pressure.py). This is unrelated to the OSM
  work in this branch. Workaround for now: pass gas_levels=[] to
  create_grid() to skip gas entirely.
- A few small, pre-existing DAVE_core bugs unrelated to OSM were found and
  fixed while testing a full real city end-to-end (an ehv_data typo in
  create.py, a missing empty-check in power_plants.py's line creation,
  an unused/mismatched parameter in loads.py, and a typo in clean_up.py).
  These fixes are included in this branch.

## Troubleshooting

"OSM data locally not found" always prints -> DAVE_OSM_PBF_PATH is unset, or
points to a path that doesn't exist. Check with "echo $env:DAVE_OSM_PBF_PATH"
in PowerShell, or "echo $DAVE_OSM_PBF_PATH" on Linux/macOS.

"Local OSM file found but the optional 'osmium' package is not installed" ->
Run "pip install osmium".

First run for a city takes 15-25+ minutes -> Expected, this is the one-time
full-file scan. Subsequent runs for the same area are fast.

"Local OSM file could not be read (...)" -> The .pbf file may be corrupted
or incomplete, try re-downloading it.
