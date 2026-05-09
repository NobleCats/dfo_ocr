\"\"\"Real-time character score lookup by name - used by the dfo_ocr overlay client.\"\"\"

import time
import threading
import asyncio
import aiohttp

from flask import Blueprint, request, jsonify, current_app

from api.config import ranking_repo, API_KEY
from api.api_helpers import async_get_character_id
from api.routes.profile_cache import create_or_update_profile_cache

realtime_bp = Blueprint('realtime', __name__)

VALID_SERVERS = {"cain", "siroco", "prey", "kard", "bakal", "all", "auto"}
# auto = pick best of cain + siroco only (active populated servers).
AUTO_SERVERS = ("cain", "siroco")

# In-memory LRU-style cache: (server, lower_name) -> (result_dict, fetched_at)
_cache: dict = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 60.0  # seconds

# Track background updates to throttle to once per 10 seconds
_background_updates = {}
_bg_lock = threading.Lock()
_UPDATE_INTERVAL = 10.0

def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
    if entry is None:
        return None
    result, fetched_at = entry
    if time.monotonic() - fetched_at > _CACHE_TTL:
        return None
    return result


def _cache_set(key, value):
    with _cache_lock:
        _cache[key] = (value, time.monotonic())


def _format_score(value) -> str:
    if value is None:
        return "??"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    return f"{value / 1000:.1f}k"


async def _background_update_task(server: str, name: str):
    \"\"\"Background task to fetch character from API and update local DB cache.\"\"\"
    if server == 'auto':
        target_servers = AUTO_SERVERS
    elif server == 'all':
        target_servers = ('cain',)
    else:
        target_servers = (server,)

    update_key = (server, name.lower())
    with _bg_lock:
        last_attempt = _background_updates.get(update_key, 0)
        now = time.monotonic()
        if now - last_attempt < _UPDATE_INTERVAL:
            return
        _background_updates[update_key] = now

    try:
        async with aiohttp.ClientSession() as session:
            for srv in target_servers:
                character_id = await async_get_character_id(session, srv, name)
                if character_id:
                    await create_or_update_profile_cache(session, srv, character_id)
                    # Invalidate cache so next request sees fresh data
                    with _cache_lock:
                        _cache.pop((server, name.lower()), None)
                    break
    except Exception as e:
        print(f\"[realtime-bg] update failed for {name}@{server}: {e}\")


def _trigger_background_update(server: str, name: str):
    \"\"\"Helper to run the async task in a thread-safe way from sync code.\"\"\"
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(_background_update_task(server, name), loop)
    except Exception:
        pass


def _lookup(server: str, name: str) -> dict:
    \"\"\"Query DB for a single (server, name) pair and return the response dict.\"\"\"
    cache_key = (server, name.lower())
    cached = _cache_get(cache_key)
    
    # Always trigger background update (throttled internally to 10s)
    _trigger_background_update(server, name)

    if cached is not None:
        return cached

    result = _query_db(server, name)
    _cache_set(cache_key, result)
    return result


def _query_db(server: str, name: str) -> dict:
    try:
        if server in ("all", "auto"):
            if server == "auto":
                query = \"\"\"
                    SELECT c.* FROM characters c
                    LEFT JOIN deleted_characters d ON c.characterId = d.characterId
                    WHERE LOWER(c.characterName) = LOWER(?)
                      AND c.serverId IN (?, ?)
                      AND d.characterId IS NULL
                      AND (c.adventureName IS NULL OR LOWER(c.adventureName) != 'agu')
                    ORDER BY c.fame DESC
                    LIMIT 1
                \"\"\"
                row = ranking_repo.execute_query(query, (name, *AUTO_SERVERS), fetch_one=True)
            else:
                query = \"\"\"
                    SELECT c.* FROM characters c
                    LEFT JOIN deleted_characters d ON c.characterId = d.characterId
                    WHERE LOWER(c.characterName) = LOWER(?)
                      AND d.characterId IS NULL
                      AND (c.adventureName IS NULL OR LOWER(c.adventureName) != 'agu')
                    ORDER BY c.fame DESC
                    LIMIT 1
                \"\"\"
                row = ranking_repo.execute_query(query, (name,), fetch_one=True)
        else:
            query = \"\"\"
                SELECT c.* FROM characters c
                LEFT JOIN deleted_characters d ON c.characterId = d.characterId
                WHERE LOWER(c.characterName) = LOWER(?)
                  AND c.serverId = ?
                  AND d.characterId IS NULL
                  AND (c.adventureName IS NULL OR LOWER(c.adventureName) != 'agu')
                ORDER BY c.fame DESC
                LIMIT 1
            \"\"\"
            row = ranking_repo.execute_query(query, (name, server), fetch_one=True)
    except Exception as e:
        print(f\"[realtime] DB error for ({server}, {name!r}): {e}\")
        return {"name": name, "found": False}

    if not row:
        # Return a placeholder so the client displays the character while we fetch
        return {
            "name": name,
            "found": True,
            "score_display": "Updating...",
            "fame": None,
            "is_buffer": False,
            "rank_percentile": None,
            "updating": True
        }
    return _build_response_from_row(row)


def _build_response_from_row(row: dict) -> dict:
    is_buffer = bool(row.get("is_buffer"))
    score_col = "total_buff_score" if is_buffer else "dps_normalized"
    sort_by = score_col
    score_value = row.get(score_col)

    character_id = row["characterId"]
    server_id = row["serverId"]

    try:
        rank_data = ranking_repo.get_character_rank(character_id, server_id, sort_by=sort_by)
    except Exception:
        rank_data = None

    rank = None
    rank_total = None
    rank_percentile = None
    if rank_data:
        rank = rank_data.get("rank")
        rank_total = rank_data.get("total_characters")
        percentile = rank_data.get("percentile")
        rank_percentile = round(percentile / 100, 4) if percentile is not None else None

    return {
        "name": row["characterName"],
        "found": True,
        "characterId": character_id,
        "serverId": server_id,
        "jobName": row.get("jobName"),
        "jobGrowName": row.get("jobGrowName"),
        "is_buffer": is_buffer,
        "fame": row.get("fame"),
        "dps_normalized": row.get("dps_normalized"),
        "total_buff_score": row.get("total_buff_score"),
        "rank": rank,
        "rank_total": rank_total,
        "rank_percentile": rank_percentile,
        "score_display": _format_score(score_value),
    }


def _build_fast_response_from_row(row: dict) -> dict:
    \"\"\"Lightweight response for overlays.\"\"\"
    is_buffer = bool(row.get("is_buffer"))
    score_value = row.get("total_buff_score") if is_buffer else row.get("dps_normalized")
    return {
        "name": row["characterName"],
        "found": True,
        "characterId": row["characterId"],
        "serverId": row["serverId"],
        "jobName": row.get("jobName"),
        "jobGrowName": row.get("jobGrowName"),
        "is_buffer": is_buffer,
        "fame": row.get("fame"),
        "dps_normalized": row.get("dps_normalized"),
        "total_buff_score": row.get("total_buff_score"),
        "score_display": _format_score(score_value),
    }


def _query_db_fast(server: str, name: str, *, prefix: bool = False) -> dict:
    name_expr = "LIKE LOWER(?)" if prefix else "= LOWER(?)"
    val = f"{name}%" if prefix else name
    if server == "auto":
        query = f\"\"\"
            SELECT c.* FROM characters c
            LEFT JOIN deleted_characters d ON c.characterId = d.characterId
            WHERE LOWER(c.characterName) {name_expr}
              AND c.serverId IN (?, ?)
              AND d.characterId IS NULL
              AND (c.adventureName IS NULL OR LOWER(c.adventureName) != 'agu')
            ORDER BY c.fame DESC
            LIMIT 1
        \"\"\"
        row = ranking_repo.execute_query(query, (val, *AUTO_SERVERS), fetch_one=True)
    elif server == "all":
        query = f\"\"\"
            SELECT c.* FROM characters c
            LEFT JOIN deleted_characters d ON c.characterId = d.characterId
            WHERE LOWER(c.characterName) {name_expr}
              AND d.characterId IS NULL
              AND (c.adventureName IS NULL OR LOWER(c.adventureName) != 'agu')
            ORDER BY c.fame DESC
            LIMIT 1
        \"\"\"
        row = ranking_repo.execute_query(query, (val,), fetch_one=True)
    else:
        query = f\"\"\"
            SELECT c.* FROM characters c
            LEFT JOIN deleted_characters d ON c.characterId = d.characterId
            WHERE LOWER(c.characterName) {name_expr}
              AND c.serverId = ?
              AND d.characterId IS NULL
              AND (c.adventureName IS NULL OR LOWER(c.adventureName) != 'agu')
            ORDER BY c.fame DESC
            LIMIT 1
        \"\"\"
        row = ranking_repo.execute_query(query, (val, server), fetch_one=True)
    if not row:
        return {
            "name": name,
            "found": True,
            "score_display": "Updating...",
            "fame": None,
            "is_buffer": False,
            "updating": True
        }
    result = _build_fast_response_from_row(row)
    if prefix:
        result["matched_via"] = "prefix"
        result["prefix"] = name
    return result


@realtime_bp.route("/api/v1/realtime/score", methods=["GET"])
def get_score():
    limiter = current_app.limiter
    limiter.limit("120 per minute")(lambda: None)()

    server = (request.args.get("server") or "").strip().lower()
    name = (request.args.get("name") or "").strip()

    if not server or not name:
        return jsonify({"error": "server and name are required"}), 400
    if server not in VALID_SERVERS:
        return jsonify({"error": f"invalid server; valid values: {sorted(VALID_SERVERS)}"}), 400

    try:
        result = _lookup(server, name)
    except Exception as e:
        print(f\"[realtime] unexpected error: {e}\")
        return jsonify({"error": "internal server error"}), 500

    return jsonify(result)


@realtime_bp.route("/api/v1/realtime/score_by_prefix", methods=["GET"])
def get_score_by_prefix():
    \"\"\"Lookup by prefix - used when OCR truncates a name with '...'.\"\"\"
    limiter = current_app.limiter
    limiter.limit("120 per minute")(lambda: None)()

    server = (request.args.get("server") or "").strip().lower()
    prefix = (request.args.get("prefix") or "").strip()

    if not server or not prefix:
        return jsonify({"error": "server and prefix are required"}), 400
    if server not in VALID_SERVERS:
        return jsonify({"error": f"invalid server; valid values: {sorted(VALID_SERVERS)}"}), 400
    if len(prefix) < 2:
        return jsonify({"error": "prefix must be at least 2 characters"}), 400

    cache_key = ("prefix", server, prefix.lower())
    cached = _cache_get(cache_key)
    if cached is not None:
        if cached.get("found"):
             _trigger_background_update(cached["serverId"], cached["name"])
        return jsonify(cached)

    pattern = prefix + "%"
    try:
        if server == "auto":
            query = \"\"\"
                SELECT c.* FROM characters c
                LEFT JOIN deleted_characters d ON c.characterId = d.characterId
                WHERE LOWER(c.characterName) LIKE LOWER(?)
                  AND c.serverId IN (?, ?)
                  AND d.characterId IS NULL
                  AND (c.adventureName IS NULL OR LOWER(c.adventureName) != 'agu')
                ORDER BY c.fame DESC
                LIMIT 1
            \"\"\"
            row = ranking_repo.execute_query(query, (pattern, *AUTO_SERVERS), fetch_one=True)
        elif server == "all":
            query = \"\"\"
                SELECT c.* FROM characters c
                LEFT JOIN deleted_characters d ON c.characterId = d.characterId
                WHERE LOWER(c.characterName) LIKE LOWER(?)
                  AND d.characterId IS NULL
                  AND (c.adventureName IS NULL OR LOWER(c.adventureName) != 'agu')
                ORDER BY c.fame DESC
                LIMIT 1
            \"\"\"
            row = ranking_repo.execute_query(query, (pattern,), fetch_one=True)
        else:
            query = \"\"\"
                SELECT c.* FROM characters c
                LEFT JOIN deleted_characters d ON c.characterId = d.characterId
                WHERE LOWER(c.characterName) LIKE LOWER(?)
                  AND c.serverId = ?
                  AND d.characterId IS NULL
                  AND (c.adventureName IS NULL OR LOWER(c.adventureName) != 'agu')
                ORDER BY c.fame DESC
                LIMIT 1
            \"\"\"
            row = ranking_repo.execute_query(query, (pattern, server), fetch_one=True)
    except Exception as e:
        print(f\"[realtime] DB error in score_by_prefix({server}, {prefix!r}): {e}\")
        return jsonify({"error": "internal server error"}), 500

    if not row:
        # For prefix, we don't necessarily want to return 'found: True' for a random string
        # because we don't know if ANY character starts with that prefix.
        # But to be safe and responsive, we can just return found: False and NOT trigger a fetch
        # because we don't have a full name to fetch.
        result = {"name": prefix, "found": False, "prefix": prefix}
        _cache_set(cache_key, result)
        return jsonify(result), 404

    result = _build_response_from_row(row)
    result["matched_via"] = "prefix"
    result["prefix"] = prefix
    
    _trigger_background_update(result["serverId"], result["name"])
    
    _cache_set(cache_key, result)
    return jsonify(result)


@realtime_bp.route("/api/v1/realtime/scores", methods=["POST"])
def get_scores():
    limiter = current_app.limiter
    limiter.limit("120 per minute")(lambda: None)()

    data = request.get_json(silent=True) or {}
    server = (data.get("server") or "").strip().lower()
    names = data.get("names")

    if not server or not names:
        return jsonify({"error": "server and names are required"}), 400
    if server not in VALID_SERVERS:
        return jsonify({"error": f"invalid server; valid values: {sorted(VALID_SERVERS)}"}), 400
    if not isinstance(names, list) or len(names) == 0:
        return jsonify({"error": "names must be a non-empty list"}), 400
    if len(names) > 20:
        return jsonify({"error": "max 20 names per request"}), 400

    try:
        results = [_lookup(server, str(n).strip()) for n in names]
    except Exception as e:
        print(f\"[realtime] unexpected error: {e}\")
        return jsonify({"error": "internal server error"}), 500

    return jsonify({"results": results})


@realtime_bp.route("/api/v1/realtime/scores_fast", methods=["POST"])
def get_scores_fast():
    \"\"\"Fast overlay lookup using cached DB score columns only.\"\"\"
    limiter = current_app.limiter
    limiter.limit("240 per minute")(lambda: None)()

    data = request.get_json(silent=True) or {}
    server = (data.get("server") or "").strip().lower()
    names = data.get("names")

    if not server or not names:
        return jsonify({"error": "server and names are required"}), 400
    if server not in VALID_SERVERS:
        return jsonify({"error": f"invalid server; valid values: {sorted(VALID_SERVERS)}"}), 400
    if not isinstance(names, list) or len(names) == 0:
        return jsonify({"error": "names must be a non-empty list"}), 400
    if len(names) > 80:
        return jsonify({"error": "max 80 names per request"}), 400

    results = []
    for raw_name in names:
        name = str(raw_name).strip()
        if not name:
            results.append({"name": name, "found": False})
            continue
        
        clean_name = name[:-3] if name.endswith("...") else name
        _trigger_background_update(server, clean_name)

        if name.endswith("..."):
            results.append(_query_db_fast(server, name[:-3], prefix=True))
        else:
            results.append(_query_db_fast(server, name, prefix=False))
    return jsonify({"results": results})
