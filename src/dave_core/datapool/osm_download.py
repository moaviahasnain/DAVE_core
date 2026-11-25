# Copyright (c) 2022-2024 by Fraunhofer Institute for Energy Economics and Energy System Technology (IEE)
# Kassel and individual contributors (see AUTHORS file for details).
# All rights reserved.
# Copyright (c) 2024-2026 DAVE_core contributors
# Use of this source code is governed by a BSD-style license that can be found in the LICENSE file.

"""
Download and maintain the local Germany-wide OSM .osm.pbf file used by
dave_core.datapool.local_osm_source.

This module provides a single public entry point, download_basic_data(),
which must be called explicitly (it is never triggered automatically from
inside osm_request()/local_osm_source.py), since it can delete and
re-download a multi-GB file and that should always be a visible, deliberate
action rather than a silent side effect of some unrelated code path.

By default this asks for the user's permission before downloading or
updating the file, so nobody is forced to fetch a multi-GB file just to
try DAVE_core - declining simply falls back to the existing Overpass-based
OSM retrieval, exactly as it always worked before this feature existed.
"""

from datetime import datetime
from pathlib import Path

import requests
from tqdm import tqdm

from dave_core.settings import dave_settings

# Geofabrik republishes this file in place under the same URL ("latest"),
# so its actual content/age changes over time even though the URL doesn't.
DEFAULT_DOWNLOAD_URL = "https://download.geofabrik.de/europe/germany-latest.osm.pbf"

# how old the local file is allowed to get before it is considered stale
# and the user is offered the option to re-download it. Expressed in days
# to avoid adding a calendar-aware dependency just for an approximate
# "6 months".
MAX_AGE_DAYS = 183

# filename used locally regardless of the name Geofabrik publishes the file
# under - matches the name expected by dave_core's existing local-OSM setup
LOCAL_FILENAME = "germany.osm.pbf"

# how many attempts the user gets to answer the y/n prompt before an
# invalid/empty response is treated as "no"
MAX_PROMPT_ATTEMPTS = 2

# tracks whether the user has already been asked (or a decision was already
# made) once in this process, so repeated create_grid() calls never ask
# more than once per run
_SESSION_STATE = {"resolved": False, "path": None}


def _default_local_path():
    """
    Per-user default location, used only if neither DAVE_OSM_PBF_PATH nor
    dave_settings["osm_pbf_path"] is already configured. Lives under the
    user's home directory so it works the same way whether dave_core is
    run from a source checkout or installed via pip.
    """
    return Path.home() / ".dave_core" / "osm" / LOCAL_FILENAME


def _resolve_target_path():
    configured = dave_settings.get("osm_pbf_path", "")
    if configured:
        return Path(configured)
    return _default_local_path()


def _file_age_days(path):
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return (datetime.now() - mtime).days


def _clear_stale_cache(pbf_path):
    """
    Remove any existing disk cache for this pbf file's location, since a
    freshly downloaded file has a new modification time and would leave
    the old cache folders orphaned (never reused, never cleaned up) otherwise.
    """
    import shutil

    cache_dir = pbf_path.parent / "cache"
    if cache_dir.is_dir():
        existing_entries = list(cache_dir.iterdir())
        if existing_entries:
            print(
                f"Deleting {len(existing_entries)} existing local OSM cache "
                f"folder(s) under {cache_dir} - they were built from the "
                f"previous .osm.pbf file and no longer match the newly "
                f"downloaded one. Fresh cache data will be created "
                f"automatically the next time OSM data is requested."
            )
        shutil.rmtree(cache_dir, ignore_errors=True)
    else:
        print(
            f"No existing local OSM cache found at {cache_dir} - "
            f"fresh cache data will be created automatically the next "
            f"time OSM data is requested."
        )


def _download_to(path, url):
    path.parent.mkdir(parents=True, exist_ok=True)
    # download to a temp name first, then atomically rename into place -
    # avoids ever leaving a corrupt/partial file under the final filename
    # if the download is interrupted
    tmp_path = path.with_name(path.name + ".part")

    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        total_size = int(response.headers.get("content-length", 0))
        with open(tmp_path, "wb") as file_obj:
            with tqdm(
                total=total_size,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=f"Downloading {path.name}",
            ) as progress:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file_obj.write(chunk)
                        progress.update(len(chunk))

    tmp_path.replace(path)


def _ask_yes_no(question):
    """
    Ask a y/n question, accepting y/yes/n/no in any mix of upper/lower
    case. Allows up to MAX_PROMPT_ATTEMPTS attempts; an invalid or empty
    answer on the final attempt (or no input at all, e.g. if there is no
    attached terminal) is treated as "no".
    """
    for attempt in range(MAX_PROMPT_ATTEMPTS):
        try:
            answer = input(f"{question} [y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nNo input could be read - treating this as 'no'.")
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        remaining = MAX_PROMPT_ATTEMPTS - attempt - 1
        if remaining > 0:
            print(f"'{answer}' is not a valid answer. Please type 'y'/'yes' or 'n'/'no'.")
    print("No valid response received - treating this as 'no'.")
    return False


def download_basic_data(url=None, max_age_days=MAX_AGE_DAYS, force=False):
    """
    Ensure a usable local Germany-wide .osm.pbf file is present, asking the
    user's permission before downloading or updating it. Must be called
    explicitly - never triggered automatically from inside osm_request().

    Only ever prompts (or resolves automatically with force=True) once per
    process: subsequent calls in the same run immediately return the
    previously resolved path without asking again.

    INPUT:
        **url** (string, default None) - override the download URL. \
            Defaults to the Geofabrik Germany-wide extract.
        **max_age_days** (int, default 183) - maximum age in days before \
            an existing local file is offered for re-download.
        **force** (boolean, default False) - if True, re-download \
            immediately without asking, and without reusing an earlier \
            decision from this process. Intended for scripted/testing use, \
            not for normal interactive runs.

    OUTPUT:
        **path** (str or None) - absolute path to the local .osm.pbf file, \
            or None if no local file is available and the user declined \
            (or did not have the option) to download one. \
            dave_settings["osm_pbf_path"] is updated to match, so the rest \
            of the current process picks up the result immediately.
    """
    if _SESSION_STATE["resolved"] and not force:
        return _SESSION_STATE["path"]

    url = url or DEFAULT_DOWNLOAD_URL
    target_path = _resolve_target_path()

    if not dave_settings.get("osm_pbf_path", ""):
        print(
            f"No DAVE_OSM_PBF_PATH configured - using default location: {target_path}\n"
            f"Set the DAVE_OSM_PBF_PATH environment variable to use a different location instead."
        )

    if force:
        if target_path.is_file():
            print(f"force=True - deleting existing {target_path.name} and re-downloading...")
            target_path.unlink()
            _clear_stale_cache(target_path)
        else:
            print(f"No local OSM file found at {target_path} - downloading now...")
        _download_to(target_path, url)
        print(f"Download complete: {target_path}")
        dave_settings["osm_pbf_path"] = str(target_path)
        _SESSION_STATE["resolved"] = True
        _SESSION_STATE["path"] = str(target_path)
        return str(target_path)

    if target_path.is_file():
        age_days = _file_age_days(target_path)

        if age_days <= max_age_days:
            print(
                f"Existing {target_path.name} is {age_days} days old "
                f"(within the {max_age_days}-day freshness limit) - "
                f"using the existing file, no download needed."
            )
            dave_settings["osm_pbf_path"] = str(target_path)
            _SESSION_STATE["resolved"] = True
            _SESSION_STATE["path"] = str(target_path)
            return str(target_path)

        # file exists, but is older than the freshness limit
        print(
            f"Local OSM file {target_path.name} is {age_days} days old, "
            f"which is older than the {max_age_days}-day freshness limit."
        )
        permission = _ask_yes_no("Do you want to download an updated version now?")
        if permission:
            print("You said yes - downloading an updated OSM file now.")
            target_path.unlink()
            _clear_stale_cache(target_path)
            _download_to(target_path, url)
            print(f"Download complete: {target_path}")
        else:
            print(
                f"Permission to download was not given - continuing to use "
                f"the existing, outdated {target_path.name}. Any previously "
                f"cached data for this file will still be used."
            )
        dave_settings["osm_pbf_path"] = str(target_path)
        _SESSION_STATE["resolved"] = True
        _SESSION_STATE["path"] = str(target_path)
        return str(target_path)

    # no local file exists at all
    print(f"No local OSM file found at {target_path}.")
    permission = _ask_yes_no("Do you want to download it now?")
    if permission:
        print("You said yes - the file is being downloaded now.")
        _download_to(target_path, url)
        print(f"Download complete: {target_path}")
        dave_settings["osm_pbf_path"] = str(target_path)
        _SESSION_STATE["path"] = str(target_path)
    else:
        print(
            "Permission to download was not given - DAVE_core will not use "
            "a local OSM file for this session and will fall back to the "
            "OSM server (Overpass API) for all OSM requests instead."
        )
        dave_settings["osm_use_local_pbf"] = False
        _SESSION_STATE["path"] = None

    _SESSION_STATE["resolved"] = True
    return _SESSION_STATE["path"]
