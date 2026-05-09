"""dfogang.com score lookup client. Cache, dedup, retry, rate-limit included."""

import threading
import time
import hashlib
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional
import re


_SENTINEL = object()   # distinguishes "never fetched" from a cached None


def _safe_print(message: str) -> None:
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        print(message)
    except Exception:
        pass


@dataclass(frozen=True)
class ScoreInfo:
    """A single character's lookup result. `score` is the formatted display
    string ("92.1k", "1.2M", "—"). `is_buffer` decides overlay color."""
    name: str
    score: str
    is_buffer: bool
    fame: Optional[int] = None
    rank_percentile: Optional[float] = None  # 0..1, lower = better


@dataclass(frozen=True)
class NeopleCharacter:
    server_id: str
    character_id: str
    name: str
    fame: int
    job_name: str = ""
    job_grow_name: str = ""


class _RateLimiter:
    """
    Token-bucket limiter: allows at most `rate` calls per second.
    Callers block until a token is available.
    """

    def __init__(self, rate: float):
        self._rate = rate               # tokens per second
        self._tokens = rate             # start full
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)


class DfogangClient:
    """
    Thread-safe client for dfogang.com character score lookups.

    Parameters
    ----------
    ttl:          Positive-result cache TTL in seconds (default 300 = 5 min).
    negative_ttl: Negative-result cache TTL in seconds (default 30).
    max_rps:      Maximum requests per second (default 5).
    demo:         If True, return deterministic fake scores instead of hitting
                  the network. Useful for end-to-end testing before the API
                  spec is wired up.
    """

    def __init__(
        self,
        ttl: float = 300.0,
        negative_ttl: float = 60.0,
        max_rps: float = 5.0,
        demo: bool = False,
        base_url: str = "https://api.dfogang.com",
        server: str = "auto",
        neople_api_key: str = "",
        neople_base_url: str = "https://api.dfoneople.com",
        request_timeout_s: float = 8.0,
        in_flight_timeout_s: float = 12.0,
    ) -> None:
        self._ttl = ttl
        self._negative_ttl = negative_ttl
        self._demo = demo
        self._base_url = base_url.rstrip("/")
        self._server = server
        self._neople_api_key = neople_api_key.strip()
        self._neople_base_url = neople_base_url.rstrip("/")
        self._request_timeout = float(request_timeout_s)
        self._in_flight_timeout = float(in_flight_timeout_s)

        # cache: name -> (ScoreInfo or None, fetched_at)
        self._cache: dict[str, tuple[Optional[ScoreInfo], float]] = {}
        self._cache_lock = threading.Lock()
        self._fame_cache: dict[int, tuple[list[NeopleCharacter], float]] = {}

        # in-flight dedup: name -> (Event, started_at).
        self._in_flight: dict[str, tuple[threading.Event, float]] = {}
        self._in_flight_lock = threading.Lock()

        self._limiter = _RateLimiter(max_rps)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_info(self, name: str) -> Optional[ScoreInfo]:
        """Return ScoreInfo for *name*, or None if not registered. Blocking.

        The OCR result is tried as-is first. If that misses, we retry a small
        set of visual-confusable variants (`|` -> `l`, `}`/`+` -> `k`) so
        ambiguous glyphs can still resolve through dfogang's canonical name.
        """
        name = name.strip()
        if not name:
            return None

        cache_key = self._cache_key(name)
        cached = self._cache_get(cache_key)
        if cached is not _SENTINEL:
            return cached  # type: ignore[return-value]

        my_event: threading.Event | None = None
        existing: tuple[threading.Event, float] | None = None
        with self._in_flight_lock:
            entry = self._in_flight.get(cache_key)
            if entry is not None:
                ev, started_at = entry
                # If the existing in-flight call has been running too long,
                # treat it as abandoned and start fresh. This prevents one
                # stuck request from blocking every subsequent caller.
                if (time.monotonic() - started_at) > self._in_flight_timeout:
                    self._in_flight.pop(cache_key, None)
                    my_event = threading.Event()
                    self._in_flight[cache_key] = (my_event, time.monotonic())
                else:
                    existing = entry
            else:
                my_event = threading.Event()
                self._in_flight[cache_key] = (my_event, time.monotonic())

        if existing is not None:
            ev, _ = existing
            ev.wait(timeout=self._in_flight_timeout)
            cached = self._cache_get(cache_key)
            return cached if cached is not _SENTINEL else None  # type: ignore[return-value]

        try:
            info = self._fetch_with_retry(name)
            self._cache_set(cache_key, info)
            return info
        finally:
            with self._in_flight_lock:
                entry = self._in_flight.pop(cache_key, None)
            if entry is not None:
                entry[0].set()

    def get_score(self, name: str) -> Optional[str]:
        """Backward-compatible: returns just the score string, or None."""
        info = self.get_info(name)
        return info.score if info is not None else None

    def get_many_info(self, names: list[str]) -> dict[str, Optional[ScoreInfo]]:
        """Batch lookup names. Exact names are fetched with one POST request;
        truncated or variant-needed misses fall back to individual lookup."""
        clean_names = []
        for name in names:
            n = name.strip()
            if n and n not in clean_names:
                clean_names.append(n)

        out: dict[str, Optional[ScoreInfo]] = {}
        missing_names: list[str] = []
        for name in clean_names:
            cached = self._cache_get(self._cache_key(name))
            if cached is not _SENTINEL:
                out[name] = cached  # type: ignore[assignment]
            else:
                missing_names.append(name)

        if missing_names:
            if self._demo:
                for name in missing_names:
                    info = self._demo_score(name)
                    self._cache_set(self._cache_key(name), info)
                    out[name] = info
            else:
                fetched: dict[str, Optional[ScoreInfo]] = {}

                # First ask for the OCR names exactly as seen. This keeps the
                # common case fast and prevents a large variant batch from
                # timing out and wiping out otherwise-valid exact hits.
                for i in range(0, len(missing_names), 20):
                    fetched.update(self._fetch_many_exact(missing_names[i:i + 20]))

                unresolved = [name for name in missing_names if fetched.get(name) is None]
                variant_map: dict[str, list[str]] = {}
                all_variants: list[str] = []
                for name in unresolved:
                    if name.endswith("..."):
                        continue
                    variants = [v for v in self._query_variants(name) if v != name]
                    if not variants:
                        continue
                    variant_map[name] = variants
                    for query in variants:
                        if query not in all_variants:
                            all_variants.append(query)

                for i in range(0, len(all_variants), 10):
                    fetched.update(self._fetch_many_exact(all_variants[i:i + 10]))

                for name in missing_names:
                    variants = [name] + variant_map.get(name, [])
                    info = next((fetched.get(v) for v in variants if fetched.get(v) is not None), None)
                    self._cache_set(self._cache_key(name), info)
                    out[name] = info
        return out

    def resolve_name_by_fame(self, ocr_name: str, fame: int) -> str | None:
        """Return the Neople character name at *fame* most similar to OCR text.

        Requires a Neople API key. The exact fame bracket is queried, then the
        best candidate is selected by normalized string similarity. Truncated
        OCR names ending in "..." are matched as prefixes.
        """
        if not self._neople_api_key or fame <= 0:
            return None
        candidates = self._fetch_characters_by_fame(fame)
        if not candidates:
            return None

        ocr = ocr_name.strip()
        if not ocr:
            return None
        best: tuple[float, NeopleCharacter] | None = None
        for candidate in candidates:
            score = self._name_similarity(ocr, candidate.name)
            if best is None or score > best[0]:
                best = (score, candidate)
        if best is None:
            return None
        threshold = 0.55 if ocr.endswith("...") else 0.68
        return best[1].name if best[0] >= threshold else None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(name: str) -> str:
        return name.strip()

    def _cache_get(self, name: str):
        """Return cached value or _SENTINEL if missing/expired."""
        with self._cache_lock:
            entry = self._cache.get(name)
        if entry is None:
            return _SENTINEL
        score, fetched_at = entry
        ttl = self._negative_ttl if score is None else self._ttl
        if time.monotonic() - fetched_at > ttl:
            return _SENTINEL
        return score

    def _cache_set(self, name: str, score: Optional[ScoreInfo]) -> None:
        with self._cache_lock:
            self._cache[name] = (score, time.monotonic())

    def _fame_cache_get(self, fame: int) -> list[NeopleCharacter] | None:
        with self._cache_lock:
            entry = self._fame_cache.get(fame)
        if entry is None:
            return None
        rows, fetched_at = entry
        if time.monotonic() - fetched_at > self._ttl:
            return None
        return rows

    def _fame_cache_set(self, fame: int, rows: list[NeopleCharacter]) -> None:
        with self._cache_lock:
            self._fame_cache[fame] = (rows, time.monotonic())

    def _fetch_characters_by_fame(self, fame: int) -> list[NeopleCharacter]:
        cached = self._fame_cache_get(fame)
        if cached is not None:
            return cached
        if self._demo:
            rows = [NeopleCharacter(
                server_id="cain",
                character_id=hashlib.md5(str(fame).encode()).hexdigest(),
                name=f"Demo{fame}",
                fame=fame,
            )]
            self._fame_cache_set(fame, rows)
            return rows

        import requests

        self._limiter.acquire()
        url = f"{self._neople_base_url}/df/servers/all/characters-fame"
        params = {
            "minFame": fame,
            "maxFame": fame,
            "limit": 200,
            "apikey": self._neople_api_key,
        }
        resp = requests.get(url, params=params, timeout=self._request_timeout)
        if resp.status_code == 404:
            rows = []
        else:
            resp.raise_for_status()
            rows = self._parse_neople_fame_rows(resp.json())
        self._fame_cache_set(fame, rows)
        return rows

    @staticmethod
    def _parse_neople_fame_rows(data: dict) -> list[NeopleCharacter]:
        out: list[NeopleCharacter] = []
        for item in data.get("rows", []):
            name = item.get("characterName") or ""
            fame = item.get("fame")
            if not name or fame is None:
                continue
            out.append(NeopleCharacter(
                server_id=item.get("serverId") or "",
                character_id=item.get("characterId") or "",
                name=name,
                fame=int(fame),
                job_name=item.get("jobName") or "",
                job_grow_name=item.get("jobGrowName") or "",
            ))
        return out

    @staticmethod
    def _name_similarity(ocr_name: str, candidate_name: str) -> float:
        def norm(value: str) -> str:
            value = value.replace("...", "")
            value = value.replace("|", "l")
            return re.sub(r"[^0-9a-z]+", "", value.lower())

        ocr = norm(ocr_name)
        cand = norm(candidate_name)
        if not ocr or not cand:
            return 0.0
        ratio = SequenceMatcher(None, ocr, cand).ratio()
        if ocr_name.endswith("...") and cand.startswith(ocr):
            ratio = max(ratio, min(0.95, len(ocr) / max(1, len(cand)) + 0.25))
        return ratio

    # ------------------------------------------------------------------
    # Retry wrapper
    # ------------------------------------------------------------------

    def _fetch_with_retry(self, name: str) -> Optional[ScoreInfo]:
        # Two attempts max with a short fixed backoff. The capture loop runs
        # at 10fps so even if both attempts fail, the next frame will retry
        # via the negative-cache TTL — there's no point in long backoff here.
        delays = [0.5]
        last_exc: Exception = RuntimeError("unreachable")
        attempts = len(delays) + 1
        for attempt in range(attempts):
            try:
                self._limiter.acquire()
                for query in self._query_variants(name):
                    info = self._demo_score(query) if self._demo else self._fetch_score(query)
                    if info is not None:
                        return info
                return None
            except NotImplementedError:
                raise
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500:
                    return None
                last_exc = exc
                _safe_print(f"[dfogang] fetch failed for {name!r} "
                            f"(attempt {attempt + 1}/{attempts}): {exc}")
                if attempt < len(delays):
                    time.sleep(delays[attempt])
        # Cache as None so the negative-cache TTL prevents tight retries on a
        # persistently-broken backend; the loop will retry after that.
        _safe_print(f"[dfogang] giving up on {name!r}: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # Demo mode
    # ------------------------------------------------------------------

    @staticmethod
    def _demo_score(name: str) -> Optional[ScoreInfo]:
        """Deterministic fake info. Names starting with '_' simulate 'not found'."""
        if name.startswith("_"):
            return None
        digest = int(hashlib.md5(name.lower().encode()).hexdigest(), 16)
        raw = (digest % 999_000) + 1_000
        # Format like the real backend: "92.1k" / "1.2M".
        if raw >= 1_000_000:
            score_str = f"{raw / 1_000_000:.1f}M"
        else:
            score_str = f"{raw / 1_000:.1f}k"
        return ScoreInfo(
            name=name,
            score=score_str,
            is_buffer=(digest % 4 == 0),  # ~25% are buffers (matches reality roughly)
            fame=raw,
            rank_percentile=(digest % 10000) / 10000.0,
        )

    @staticmethod
    def _query_variants(name: str) -> list[str]:
        substitutions = {
            "|": ["l", "i"],
            "}": ["k"],
            "+": ["k"],
            "Z": ["2"],
            "B": ["6"],
            "w": ["m"],
            "O": ["o"],
            "L": ["k"],
            "h": ["k"],
            "l": ["I"],
            "k": ["li"],
            "s": ["S"],
            "J": ["1"],
        }
        variants = [name]
        seen = {name}
        if "kng" in name:
            candidate = name.replace("kng", "king")
            if candidate not in seen:
                seen.add(candidate)
                variants.append(candidate)
        for bad, good in (("IJ", "U"), ("lJ", "U"), ("iJ", "U")):
            if bad in name:
                candidate = name.replace(bad, good)
                if candidate not in seen:
                    seen.add(candidate)
                    variants.append(candidate)
        # Keep the combinatorics bounded. OCR mistakes in names are usually
        # one or two visually-confusable glyphs, and dfogang lookup is remote.
        for i, ch in enumerate(name):
            for repl in substitutions.get(ch, []):
                candidate = name[:i] + repl + name[i + 1:]
                if candidate not in seen:
                    seen.add(candidate)
                    variants.append(candidate)
        base_variants = list(variants)
        for base in base_variants:
            for i, ch in enumerate(base):
                for repl in substitutions.get(ch, []):
                    candidate = base[:i] + repl + base[i + 1:]
                    if candidate not in seen:
                        seen.add(candidate)
                        variants.append(candidate)
                    if len(variants) >= 16:
                        return variants
        return variants

    # ------------------------------------------------------------------
    # THE stub — fill this in once the dfogang API spec is available
    # ------------------------------------------------------------------

    def _fetch_score(self, name: str) -> Optional[ScoreInfo]:
        import requests
        # If the recognized name ends with '...' the OCR truncated it; the
        # backend offers a prefix endpoint that picks the best-fame match.
        truncated = name.endswith("...")
        if truncated:
            url = f"{self._base_url}/api/v1/realtime/score_by_prefix"
            params = {"server": self._server, "prefix": name[:-3]}
        else:
            url = f"{self._base_url}/api/v1/realtime/score"
            params = {"server": self._server, "name": name}
        resp = requests.get(url, params=params, timeout=self._request_timeout)
        # 404 from the backend = name (or prefix) not found. Return None
        # without retrying — it's a definitive answer, not a transient error.
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not data.get("found"):
            return None
        return ScoreInfo(
            name=data.get("name") or name,
            score=data.get("score_display") or "—",
            is_buffer=bool(data.get("is_buffer")),
            fame=data.get("fame"),
            rank_percentile=data.get("rank_percentile"),
        )

    def _fetch_many_exact(self, names: list[str]) -> dict[str, Optional[ScoreInfo]]:
        import requests

        self._limiter.acquire()
        url = f"{self._base_url}/api/v1/realtime/scores_fast"
        resp = requests.post(url, json={"server": self._server, "names": names}, timeout=self._request_timeout)
        if not resp.ok:
            fast_error = resp.status_code
            non_prefix = [n for n in names if not n.endswith("...")]
            out: dict[str, Optional[ScoreInfo]] = {}
            if non_prefix:
                url = f"{self._base_url}/api/v1/realtime/scores"
                resp = requests.post(url, json={"server": self._server, "names": non_prefix}, timeout=10)
                resp.raise_for_status()
                out.update(self._parse_many_response(non_prefix, resp.json()))
            for name in names:
                if name.endswith("..."):
                    out[name] = self._fetch_score(name)
                else:
                    out.setdefault(name, None)
            if fast_error not in (404, 500):
                _safe_print(f"[dfogang] scores_fast unavailable (HTTP {fast_error}); used fallback")
            return out
        resp.raise_for_status()
        return self._parse_many_response(names, resp.json())

    @staticmethod
    def _parse_many_response(names: list[str], data: dict) -> dict[str, Optional[ScoreInfo]]:
        out: dict[str, Optional[ScoreInfo]] = {}
        for original, item in zip(names, data.get("results", [])):
            if not item.get("found"):
                out[original] = None
                continue
            out[original] = ScoreInfo(
                name=item.get("name") or original,
                score=item.get("score_display") or "??",
                is_buffer=bool(item.get("is_buffer")),
                fame=item.get("fame"),
                rank_percentile=item.get("rank_percentile"),
            )
        for name in names:
            out.setdefault(name, None)
        return out


# ---------------------------------------------------------------------------
# __main__ demo — run with:  python src/dfogang.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import concurrent.futures

    print("=== DfogangClient demo ===\n")

    client = DfogangClient(demo=True, ttl=10.0, negative_ttl=5.0)

    # 1. Basic lookup
    info = client.get_info("zerkaa")
    print(f"[1] zerkaa -> {info!r}")
    assert info is not None, "Expected info"
    score = info.score

    # 2. Cache hit on second call
    t0 = time.monotonic()
    score2 = client.get_score("zerkaa")
    elapsed = time.monotonic() - t0
    print(f"[2] zerkaa (cached) -> {score2!r}  (took {elapsed*1000:.2f} ms)")
    assert score == score2, "Cache should return the same value"
    assert elapsed < 0.01, "Cache hit should be near-instant"

    # 3. Negative result (names starting with '_' → None)
    missing = client.get_score("_ghost")
    print(f"[3] _ghost -> {missing!r}")
    assert missing is None, "Expected None for missing name"

    # Confirm negative is also cached
    missing2 = client.get_score("_ghost")
    assert missing2 is None

    # 4. Concurrency: 4 threads fetch the same name — only 1 underlying call.
    # Subclass to intercept _demo_score and count actual invocations.
    counter = {"n": 0}

    class _InstrumentedClient(DfogangClient):
        @staticmethod
        def _demo_score(name: str) -> Optional[ScoreInfo]:
            counter["n"] += 1
            time.sleep(0.05)   # simulate latency so threads pile up
            return DfogangClient._demo_score(name)

    c2 = _InstrumentedClient(demo=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(c2.get_score, "raiden") for _ in range(4)]
        results = [f.result() for f in futures]

    print(f"[4] 4 threads fetched 'raiden': results={results}, underlying calls={counter['n']}")
    assert counter["n"] == 1, f"Expected 1 underlying fetch, got {counter['n']}"
    assert len(set(results)) == 1, "All threads should get the same score"

    print("\nAll assertions passed.")
