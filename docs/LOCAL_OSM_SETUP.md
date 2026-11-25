# Local-First OSM Data Source (Germany-wide .osm.pbf)

## What this is

By default, DAVE_core retrieves OpenStreetMap data by querying the public
Overpass API for every requested area. This can be slow and is sometimes
unreliable (server timeouts, rate limiting, "no response from server").

This feature lets DAVE_core instead read OSM data from a locally stored,
Germany-wide .osm.pbf file. If the file is present and readable, it is
used automatically and the Overpass API is never contacted. If the file
is missing, outdated, or the optional reader library isn't installed,
DAVE_core prints a message and transparently falls back to the original
Overpass-based request - nobody is forced to download a multi-GB file
just to try DAVE_core, and no existing behavior changes if you decline.

## One-time setup

### 1. Install the optional dependencies

pip install dave_core[osm_local]

This installs osmium (the PBF reader), pyarrow (used for the on-disk
cache), and requests (used for downloading the file). If you installed
dave_core without the [osm_local] extra already, you can instead run:

pip install osmium pyarrow requests

### 2. Run your script as normal

You do not need to manually download anything or set any environment
variable to get started. The first time your script calls create_grid()
in a way that actually needs OSM data (roads, buildings, landuse,
railways, or waterways), DAVE_core will automatically check for a local
file and prompt you if needed - see "Automatic download and update
prompts" below.

### Optional: choose where the file is stored

By default, the file is stored under a per-user folder
(~/.dave_core/osm/germany.osm.pbf). To use a different location
(e.g. if you already downloaded the file somewhere), set the
DAVE_OSM_PBF_PATH environment variable to the full file path.

Windows (PowerShell), permanent for your user account:
setx DAVE_OSM_PBF_PATH "C:\path\to\germany.osm.pbf"
Restart your terminal/IDE afterwards for the variable to take effect.

Linux / macOS, add to ~/.bashrc or ~/.zshrc:
export DAVE_OSM_PBF_PATH="/path/to/germany.osm.pbf"
Then reload your shell (source ~/.bashrc) or open a new terminal.

## Automatic download and update prompts

The first time a script needs OSM data, DAVE_core checks the local file
and does ONE of the following:

- **File present and <= 6 months old:** used automatically, with a short
  confirmation message. No prompt.
- **File present but older than 6 months:** you are told the file is
  outdated and asked:
  "Do you want to download an updated version now? [y/n]"
  - Answering yes deletes the old file, clears any existing disk cache
    (it would otherwise no longer match the new file and sit unused),
    and downloads a fresh copy.
  - Answering no leaves the existing file and any existing cache
    completely untouched, and DAVE_core continues using them as before.
- **File missing entirely:** you are asked:
  "Do you want to download it now? [y/n]"
  - Answering yes downloads the file (~4.5 GB; a progress bar is shown).
  - Answering no means DAVE_core will NOT use a local file for the rest
    of this run, and will fall back to the Overpass API for all OSM
    requests instead - exactly as DAVE_core behaved before this feature
    existed.

The prompt accepts y/yes/n/no (any mix of upper/lower case). An invalid
or empty answer is re-asked once; a second invalid answer is treated as
"no" automatically (nothing hangs waiting for input).

You are only ever asked ONCE per script run, no matter how many times
create_grid() is called afterward - the decision from the first prompt
is reused automatically for the rest of that run.

## How the caching works

- The first time DAVE_core requests OSM data for a given area (e.g. a
  city), it scans the entire local .osm.pbf file once for that area's
  exact polygon shape. This is a full-file scan and can take
  15-25+ minutes depending on your disk/CPU, since the file covers all
  of Germany.
- All OSM categories DAVE_core uses (roads, buildings, landuse, leisure,
  natural, railways, waterways) are extracted together in that single
  scan, not one scan per category.
- The result is cached to disk under data/osm/cache/<hash>/ (one folder
  per distinct area polygon, as .parquet files). This cache folder is
  gitignored - it's regenerated locally and is never committed (with one
  documented exception for Wuppertal, see below).
- Any later request for the same area polygon is served from this disk
  cache almost instantly, without touching the .pbf file again.
- If the .pbf file is replaced (a fresh download after being missing or
  outdated), any existing cache is automatically deleted at that point,
  since it was built from the old file and would otherwise be left on
  disk unused forever. A message confirms this when it happens.

### Expect roughly 2-3 full scans per new city

DAVE_core requests OSM data using a few different polygon shapes per
area (a plain boundary, a buffered boundary, and a separately-buffered
shape for natural features). Each distinct shape triggers its own
one-time scan the first time a new city is processed. Every later run
for that same city reuses the cache and completes in seconds.

## Pre-generated cache for Wuppertal

This branch includes a pre-generated disk cache under data/osm/cache/
for Wuppertal, saved from a prior full scan. If your copy of
germany.osm.pbf happens to share the same file modification time as the
one used to generate this cache, DAVE_core will use it automatically and
skip the initial scan entirely.

If your downloaded file has a different modification time (likely, since
Geofabrik periodically re-publishes "latest" snapshots), the cache will
be silently ignored and a fresh scan will run instead - this is expected
and not an error.

## Known limitations (current state of this branch)

- **LV (low-voltage) network connectivity.** When generating a full
  city's electrical network with power_levels=["hv","mv","lv"], the
  low-voltage steiner-tree step that connects buildings to the grid can
  fail for some or all network groups, without raising an error by
  default. topology_utils.py now logs
  "LV steiner tree failed for net group ..." with the underlying
  exception when this happens (previously the error was silently
  swallowed). The root cause is not yet fixed, and in testing, buildings
  ended up fully disconnected from the LV network for a full city run.
  If you hit this, please save the printed messages.
- **High-pressure gas topology.** create_hp_topology() currently raises
  TypeError: unhashable type: 'dict' inside gaslib_pipe_clustering()
  (src/dave_core/topology/high_pressure.py). This is unrelated to the
  local-OSM feature in this branch. Workaround: pass gas_levels=[] to
  create_grid() to skip gas generation for now.
- A few small, pre-existing DAVE_core bugs unrelated to OSM were found
  and fixed while validating this feature against a full real city: an
  ehv_data typo in create.py, a mismatched function signature and an
  unguarded empty-dataframe case in loads.py, and a typo in clean_up.py.

## Troubleshooting

"OSM data locally not found" always prints -> You previously declined
the download prompt, or DAVE_OSM_PBF_PATH points to a path that doesn't
exist. Check with "echo $env:DAVE_OSM_PBF_PATH" in PowerShell, or
"echo $DAVE_OSM_PBF_PATH" on Linux/macOS.

"Local OSM file found but the optional 'osmium' package is not
installed" -> Run "pip install osmium".

First run for a city takes 15-25+ minutes -> Expected, this is the
one-time full-file scan. Subsequent runs for the same area are fast.

"Local OSM file could not be read (...)" -> The .pbf file may be
corrupted or incomplete, try re-downloading it (you'll be prompted
automatically next run since the file is still present - delete it
manually first if you want the "missing file" prompt instead).