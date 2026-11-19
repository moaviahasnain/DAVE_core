# Copyright (c) 2022-2024 by Fraunhofer Institute for Energy Economics and Energy System Technology (IEE)
# Kassel and individual contributors (see AUTHORS file for details).
# All rights reserved.
# Copyright (c) 2024-2026 DAVE_core contributors
# Use of this source code is governed by a BSD-style license that can be found in the LICENSE file.

"""
Local-first OpenStreetMap data source.

This module tries to satisfy OSM data requests from a locally available,
Germany-wide ``.osm.pbf`` extract before DAVE_core falls back to the public
Overpass API. It reproduces the structure produced by
``dave_core.datapool.osm_request.query_osm`` / ``read_osm`` closely enough
that ``osm_request()`` and the downstream processing in ``osm_data.py``
need no changes at all: ways are returned as raw ``LineString`` geometries
(closed or not), with an ``id`` column and whatever tag columns are present,
in the ``crs_degree`` CRS.

Reading is done with the optional "osmium" package (pyosmium). If it is not
installed, or the configured file is missing/unreadable, the caller falls
back to the existing Overpass request path.
"""
import hashlib
import time
from pathlib import Path

from geopandas import GeoDataFrame
from geopandas import GeoSeries
from pandas import read_excel
from shapely.prepared import prep
from tqdm import tqdm

from dave_core.datapool.read_data import get_data_path
from dave_core.settings import dave_settings


def _status(message):
    """Print a status message without clashing with an active tqdm progress bar."""
    try:
        tqdm.write(message)
    except Exception:
        print(message)

# bump this if the extraction logic in _scan_pbf() changes in a way that
# makes previously cached disk extracts incompatible or outdated
CACHE_SCHEMA_VERSION = 1

# cache: polygon-WKB -> {data_type: GeoDataFrame}
# filled lazily; one full scan of the local pbf per distinct area polygon
# requested during a DAVE run, reused by every data_type sharing that area
_SCAN_CACHE = {}

# only warn once per process if the optional "osmium" package is missing
_WARNED_NO_READER = False


class _CategoryFilter:
    """Holds one entry of dave_settings['osm_tags'] in a convenient shape."""

    __slots__ = ("key", "values", "types", "columns")

    def __init__(self, key, values, types, columns):
        self.key = key
        self.values = values
        self.types = types
        self.columns = columns

    def matches(self, tags):
        if self.key not in tags:
            return False
        if self.values is True:
            return True
        return tags.get(self.key) in self.values


def _area_cache_key(area):
    """Build a stable cache key for a shapely polygon."""
    return area.wkb


def _disk_cache_dir(pbf_path, area):
    """
    Build a per-area, per-pbf-version cache folder path. The folder name is
    a hash of the area polygon, the pbf file's modification time (so an
    updated germany.osm.pbf invalidates old caches automatically) and the
    cache schema version above (so code changes to _scan_pbf() can
    invalidate old caches on purpose).
    """
    cache_root = dave_settings.get("osm_pbf_cache_dir") or ""
    cache_root = Path(cache_root) if cache_root else pbf_path.parent / "cache"
    pbf_mtime = int(pbf_path.stat().st_mtime)
    key_material = f"{area.wkb_hex}|{pbf_mtime}|{CACHE_SCHEMA_VERSION}".encode()
    key_hash = hashlib.sha256(key_material).hexdigest()[:16]
    return cache_root / key_hash


def _load_disk_cache(cache_dir, categories):
    """
    Try to load a previously cached scan result from disk. Returns None if
    the cache folder does not exist or is incomplete, in which case the
    caller should re-scan the pbf file.
    """
    if not cache_dir.is_dir():
        return None
    result = {}
    for name in categories:
        file_path = cache_dir / f"{name}.parquet"
        if not file_path.is_file():
            return None
        try:
            from geopandas import read_parquet

            result[name] = read_parquet(file_path)
        except Exception:
            return None
    return result


def _save_disk_cache(cache_dir, scan_result):
    """
    Persist a scan result to disk so later DAVE runs for the same area can
    skip re-scanning the multi-GB pbf file entirely. Writes to a temporary
    folder first and renames it into place, so a crash mid-write cannot
    leave behind a cache folder that _load_disk_cache() would treat as
    complete and valid.
    """
    tmp_dir = cache_dir.with_name(cache_dir.name + ".tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for name, gdf in scan_result.items():
        gdf.to_parquet(tmp_dir / f"{name}.parquet")
    tmp_dir.replace(cache_dir)


def _build_row(way_id, geometry, tags, columns):
    row = {"id": str(way_id), "geometry": geometry}
    for column in columns:
        if column in ("geometry", "id"):
            continue
        if column in tags:
            row[column] = tags[column]
    return row


def get_local_osm_data(data_type, area):
    """
    Try to answer an OSM data request from the local Germany-wide .osm.pbf file.

    INPUT:
        **data_type** (string) - key of dave_settings["osm_tags"]
        **area** (shapely Polygon) - same polygon osm_request() would send
            to the Overpass API

    OUTPUT:
        **data** (GeoDataFrame or None) - None means the local source could
            not be used at all (missing file, unreadable file, optional
            dependency missing). An empty GeoDataFrame is a valid result
            (file read fine, no matching features) and is NOT the same as
            None - it must not trigger the Overpass fallback.
        **status** (string) - "local", "cache", "missing", "no_reader",
            "unreadable" or "disabled" - for logging purposes only.
    """
    global _WARNED_NO_READER

    if not dave_settings.get("osm_use_local_pbf", True):
        return None, "disabled"

    pbf_path = dave_settings.get("osm_pbf_path", "")
    if not pbf_path:
        return None, "missing"

    pbf_path = Path(pbf_path)
    if not pbf_path.is_file():
        return None, "missing"

    try:
        import osmium  # noqa: F401
    except ImportError:
        if not _WARNED_NO_READER:
            print(
                "Local OSM file found but the optional 'osmium' package is not "
                "installed (pip install osmium). Falling back to the OSM server."
            )
            _WARNED_NO_READER = True
        return None, "no_reader"

    cache_key = _area_cache_key(area)
    if cache_key in _SCAN_CACHE:
        return _SCAN_CACHE[cache_key].get(data_type, GeoDataFrame([])), "cache"

    categories = dave_settings["osm_tags"].keys()
    disk_cache_dir = _disk_cache_dir(pbf_path, area)
    disk_result = _load_disk_cache(disk_cache_dir, categories)
    if disk_result is not None:
        _status(f"\n Loaded local OSM data from disk cache ({disk_cache_dir.name}).")
        _SCAN_CACHE[cache_key] = disk_result
        return disk_result.get(data_type, GeoDataFrame([])), "disk_cache"

    _status(f"\n Scanning local OSM file ({pbf_path.name})... this can take several minutes.")
    _scan_start = time.time()
    try:
        scan_result = _scan_pbf(pbf_path, area)
    except Exception as exception:
        print(f"\n Local OSM file could not be read ({exception}). Falling back to the OSM server.")
        return None, "unreadable"
    _status(f"\n Local OSM scan finished in {time.time() - _scan_start:.1f} seconds.")

    try:
        _save_disk_cache(disk_cache_dir, scan_result)
    except Exception as exception:
        # a failed cache write should never break the actual OSM request
        _status(f"\n Could not write local OSM disk cache ({exception}); continuing without it.")

    _SCAN_CACHE[cache_key] = scan_result
    return scan_result.get(data_type, GeoDataFrame([])), "local"


def _scan_pbf(pbf_path, area):
    """
    Perform a single pass over the local .osm.pbf file and extract all OSM
    categories DAVE_core knows about (dave_settings["osm_tags"]) for the
    given area at once, so several osm_request() calls sharing the same
    area only read the (large) file once.
    """
    import osmium
    from osmium import geom as osmium_geom
    from shapely import wkb as wkb_loader

    categories = {
        name: _CategoryFilter(params[0], params[1], params[2], params[3])
        for name, params in dave_settings["osm_tags"].items()
    }
    needs_way_cache = any("relation" in f.types for f in categories.values())

    class _Handler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.wkbfab = osmium_geom.WKBFactory()
            self.prepared_area = prep(area)
            self.rows = {name: [] for name in categories}
            self._way_cache = {}
            self._matched_relations = {name: [] for name in categories}

        def way(self, w):
            try:
                wkb = self.wkbfab.create_linestring(w)
            except (RuntimeError, osmium.InvalidLocationError):
                return
            geometry = wkb_loader.loads(wkb, hex=True)
            if not self.prepared_area.intersects(geometry):
                return
            tags = dict(w.tags)
            if needs_way_cache:
                self._way_cache[w.id] = (geometry, tags)
            for name, filt in categories.items():
                if "way" in filt.types and filt.matches(tags):
                    self.rows[name].append(_build_row(w.id, geometry, tags, filt.columns))

        def relation(self, r):
            tags = dict(r.tags)
            for name, filt in categories.items():
                if "relation" in filt.types and filt.matches(tags):
                    member_ids = [m.ref for m in r.members if m.type == "w"]
                    self._matched_relations[name].append((dict(tags), member_ids))

        def resolve_relations(self):
            # add relation member ways that didn't match the tag filter on
            # their own, filling missing tag columns from the relation's
            # tags - generalizes DAVE's existing landuse/relation merge
            for name, filt in categories.items():
                if "relation" not in filt.types:
                    continue
                already = {row["id"] for row in self.rows[name]}
                for rel_tags, member_ids in self._matched_relations[name]:
                    for way_id in member_ids:
                        way_key = str(way_id)
                        if way_key in already or way_id not in self._way_cache:
                            continue
                        geometry, way_tags = self._way_cache[way_id]
                        merged_tags = {**rel_tags, **way_tags}
                        self.rows[name].append(
                            _build_row(way_id, geometry, merged_tags, filt.columns)
                        )
                        already.add(way_key)

    handler = _Handler()
    index_type = dave_settings.get("osm_pbf_location_index", "flex_mem")
    handler.apply_file(str(pbf_path), locations=True, idx=index_type)
    handler.resolve_relations()

    result = {}
    for name in categories:
        rows = handler.rows[name]
        if not rows:
            result[name] = GeoDataFrame(
                {"geometry": GeoSeries([], dtype="geometry")},
                crs=dave_settings["crs_degree"],
            )
            continue
        result[name] = GeoDataFrame(rows, geometry="geometry", crs=dave_settings["crs_degree"])
    return result


def get_local_osm_meta_data():
    """Same metadata file the Overpass path uses, so grid_data.meta_data stays consistent."""
    return read_excel(get_data_path("osm_meta.xlsx", "data"), sheet_name=None)
