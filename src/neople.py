"""Neople DFO Open API client.

Wraps the three endpoints we need for fame-based name correction:

    GET /df/jobs                                                  (cached at boot)
    GET /df/servers/<srv>/characters-fame                         (fame range)
    GET /df/servers/<srv>/characters/<characterId>                (validation)

The client is built around three constraints from the project:

* The official rate limit is small (single-digit rps), so a token-bucket
  limits all outbound calls and `max_workers` callers share it.
* The capture loop runs at 10fps and can fan out per-frame requests; the
  client must NOT pile up infinite in-flight calls. Every request enforces a
  hard timeout; the caller never blocks longer than `request_timeout_s`.
* Negative results (no match in fame range) cache for `negative_ttl_s` to
  prevent a tight retry loop on rows we'll never resolve.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

import requests


_logger = logging.getLogger("dfogang.neople")


# Officially documented as ~5rps for free tier. Stay below to leave headroom
# for parallel callers and to avoid 429s under bursty load.
_DEFAULT_MAX_RPS = 4.0
_DEFAULT_TIMEOUT = 8.0
_DEFAULT_TTL = 600.0          # 10 min positive cache
_DEFAULT_NEG_TTL = 60.0       # 1 min negative cache
_DEFAULT_JOBS_TTL = 24 * 3600 # job catalog rarely changes

# Neople exposes a synthetic "all" pseudo-server that aggregates the global
# cluster (cain + siroco). One query at "all" returns rows from both servers
# with their real serverId tagged in each row, halving our API call budget.
ALL_SERVERS = "all"


@dataclass(frozen=True)
class JobInfo:
    job_id: str
    job_name: str
    grow_id: str
    grow_name: str            # 1st awakening / 2nd awakening / "Neo: <X>" name
    is_buff: bool


@dataclass(frozen=True)
class FameCharacter:
    server_id: str
    character_id: str
    name: str
    fame: int
    job_id: str = ""
    job_name: str = ""
    job_grow_id: str = ""
    job_grow_name: str = ""


@dataclass(frozen=True)
class CharacterDetail:
    server_id: str
    character_id: str
    name: str
    fame: Optional[int]
    adventure_name: str
    guild_name: str = ""


class _RateLimiter:
    """Shared token bucket. All HTTP calls go through .acquire()."""

    def __init__(self, rate: float):
        self._rate = float(rate)
        self._tokens = float(rate)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._rate,
                                   self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self._rate
            if deadline is not None and (time.monotonic() + wait) > deadline:
                return False
            time.sleep(min(wait, 0.5))


@dataclass
class _CacheEntry:
    value: object
    fetched_at: float = field(default_factory=time.monotonic)


class NeopleClient:
    """Thread-safe client. One instance per process is enough."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.dfoneople.com",
        max_rps: float = _DEFAULT_MAX_RPS,
        request_timeout_s: float = _DEFAULT_TIMEOUT,
        positive_ttl_s: float = _DEFAULT_TTL,
        negative_ttl_s: float = _DEFAULT_NEG_TTL,
        jobs_ttl_s: float = _DEFAULT_JOBS_TTL,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = request_timeout_s
        self._ttl = positive_ttl_s
        self._neg_ttl = negative_ttl_s
        self._jobs_ttl = jobs_ttl_s
        self._limiter = _RateLimiter(max_rps)

        # Cache: jobs[norm(grow_name)] -> list[JobInfo]. A list because
        # gendered class pairs (Fighter F vs M, Slayer F vs M, ...) often
        # share a jobGrowName like 'Nen Master'; using a single-value dict
        # silently drops one entry on overwrite and produces wrong jobIds.
        self._jobs: dict[str, list[JobInfo]] = {}
        self._jobs_fetched_at: float = 0.0
        self._jobs_lock = threading.Lock()

        # fame search cache: (job_grow_id, fame_lo, fame_hi) -> list[FameCharacter]
        self._fame_cache: dict[tuple[str, int, int], _CacheEntry] = {}
        # detail cache: (server, character_id) -> CharacterDetail | None
        self._detail_cache: dict[tuple[str, str], _CacheEntry] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def jobs(self) -> dict[str, list[JobInfo]]:
        """Return norm(jobGrowName) -> [JobInfo, ...]. Result is cached."""
        if not self._api_key:
            return {}
        with self._jobs_lock:
            if self._jobs and (time.monotonic() - self._jobs_fetched_at) < self._jobs_ttl:
                return self._jobs
        # acquire outside the lock so other callers can use cached data while we
        # block on rate-limited refresh.
        if not self._limiter.acquire(timeout=self._timeout):
            return self._jobs
        url = f"{self._base_url}/df/jobs"
        try:
            resp = requests.get(url, params={"apikey": self._api_key},
                                timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _logger.warning("jobs fetch failed: %s", exc)
            return self._jobs
        index: dict[str, list[JobInfo]] = {}

        def _walk_grow(job_id: str, job_name: str, grow: dict) -> None:
            """Recursively register a jobGrow plus its awakening chain.

            Neople nests the awakening progression under 'next':
                base → next → 1st awak → next → 2nd awak → next → 'Neo: <X>'

            Each level has its own jobGrowId and jobGrowName. The previous
            single-level read silently dropped every Neo-tier entry.
            """
            if not isinstance(grow, dict):
                return
            grow_id = grow.get("jobGrowId") or ""
            grow_name = grow.get("jobGrowName") or ""
            if grow_name:
                info = JobInfo(
                    job_id=job_id, job_name=job_name,
                    grow_id=grow_id, grow_name=grow_name,
                    is_buff=bool(grow.get("isBuffer") or grow.get("isBuff")),
                )
                full_key = _norm_jobname(grow_name)
                index.setdefault(full_key, []).append(info)
                # Also register under the base-name key (Neo: stripped) so
                # OCR that loses the Neo: prefix still finds the awakening.
                base = grow_name[4:].strip() if grow_name.lower().startswith("neo:") else grow_name
                base_key = _norm_jobname(base)
                if base_key and base_key != full_key:
                    index.setdefault(base_key, []).append(info)
            nxt = grow.get("next")
            if isinstance(nxt, dict):
                _walk_grow(job_id, job_name, nxt)
            elif isinstance(nxt, list):
                for n in nxt:
                    _walk_grow(job_id, job_name, n)

        for job in data.get("rows", []):
            job_id = job.get("jobId") or ""
            job_name = job.get("jobName") or ""
            for grow in job.get("rows", []) or []:
                _walk_grow(job_id, job_name, grow)
        # Add base-job aliases without overwriting more specific awakening keys.
        for job in data.get("rows", []):
            job_name = job.get("jobName") or ""
            if not job_name:
                continue
            key = _norm_jobname(job_name)
            # Append base-job alias only if no awakening already claimed
            # this key (e.g. avoid clobbering 'Paramedic' awakening with
            # 'Female Mage' base when those happen to normalize the same).
            if key not in index:
                grows = job.get("rows") or []
                if grows:
                    grow = grows[-1]
                    index[key] = [JobInfo(
                        job_id=job.get("jobId") or "",
                        job_name=job_name,
                        grow_id=grow.get("jobGrowId") or "",
                        grow_name=grow.get("jobGrowName") or "",
                        is_buff=bool(grow.get("isBuffer")),
                    )]
        with self._jobs_lock:
            self._jobs = index
            self._jobs_fetched_at = time.monotonic()
        # Dump every key so we can confirm whether 'neo<X>' awakening keys
        # actually exist in the API's /df/jobs response, or whether the
        # Neo: prefix is purely a display label that we never see here.
        neo_keys = sorted(k for k in index if k.startswith("neo"))
        base_keys = sorted(k for k in index if not k.startswith("neo"))
        _logger.info(
            "jobs index loaded: %d total keys (%d 'neo*', %d base)",
            len(index), len(neo_keys), len(base_keys))
        _logger.info("  neo* keys: %s",
                     ", ".join(neo_keys) if neo_keys else "(none)")
        _logger.info("  sample base keys: %s", ", ".join(base_keys[:20]))
        return index

    def match_jobs(self, ocr_text: str) -> list[JobInfo]:
        """Resolve OCR'd class label to ALL plausible JobInfo entries.

        Returns every JobInfo at the best-matching jobGrowName key — usually
        one entry, but two when male/female class pairs share the awakening
        name (e.g. 'Nen Master' exists for both Fighter F and Fighter M).
        Caller should query each JobInfo until one yields candidates.
        """
        text = _strip_class_prefix(ocr_text)
        if not text:
            return []
        index = self.jobs()
        if not index:
            return []

        # Build search candidates: full text, plus each token long enough to
        # discriminate (>=4 chars). Tokens win when the leading 'Neo:'-shaped
        # garbage drowns out the actual class name in the full string.
        tokens = [t for t in re.split(r"[\s:'\-]+", text) if len(t) >= 4]
        candidates = [text] + tokens
        normalized = [_norm_jobname(c) for c in candidates if _norm_jobname(c)]
        if not normalized:
            return []

        # Direct hit on any candidate: return all entries at that key.
        for n in normalized:
            if n in index:
                return list(index[n])

        best_score = 0.0
        best_key: str | None = None
        for k in index:
            for n in normalized:
                score = SequenceMatcher(None, n, k).ratio()
                if score > best_score:
                    best_score, best_key = score, k
        if best_key is not None and best_score >= 0.6:
            return list(index[best_key])
        return []

    def match_job(self, ocr_text: str) -> JobInfo | None:
        """Backwards-compatible single-best wrapper around match_jobs."""
        matches = self.match_jobs(ocr_text)
        return matches[0] if matches else None

    def search_by_fame(self, *, job_id: str, job_grow_id: str, fame: int,
                       window: int = 1,
                       all_job_grow: bool = False) -> list[FameCharacter]:
        """Fetch all chars on `ALL_SERVERS` at fame in [fame-window, fame+window].

        When `all_job_grow=True`, jobGrowId is omitted and the Neople API
        returns characters at any awakening tier of the given jobId. Use it
        as a fallback when class-OCR maps to the wrong awakening name."""
        if not self._api_key or fame <= 0:
            return []
        lo = max(1, fame - max(0, window))
        hi = max(lo, fame + max(0, window))
        key = (job_id, job_grow_id if not all_job_grow else "*", lo, hi)
        with self._cache_lock:
            entry = self._fame_cache.get(key)
        if entry is not None:
            ttl = self._neg_ttl if not entry.value else self._ttl
            if (time.monotonic() - entry.fetched_at) < ttl:
                return list(entry.value)  # type: ignore[arg-type]

        out = self._fetch_fame(ALL_SERVERS, lo, hi, job_id,
                               "" if all_job_grow else job_grow_id,
                               is_all_job_grow=all_job_grow)
        _logger.debug(
            "  search fame=[%d..%d] jobId=%s growId=%s allGrow=%s → %d rows",
            lo, hi, job_id[:8] if job_id else "-",
            job_grow_id[:8] if job_grow_id else "-",
            all_job_grow, len(out))
        with self._cache_lock:
            self._fame_cache[key] = _CacheEntry(value=out)
        return out

    def expanding_search(self, *, job_id: str, job_grow_id: str, fame: int,
                         steps: tuple[int, ...] = (1, 5, 20, 50, 100),
                         min_results: int = 1) -> list[FameCharacter]:
        """Try increasing fame windows until we get >= min_results, capped at
        the largest step (default ±100)."""
        last: list[FameCharacter] = []
        for window in steps:
            last = self.search_by_fame(job_id=job_id, job_grow_id=job_grow_id,
                                       fame=fame, window=window)
            if len(last) >= min_results:
                return last
        return last

    def resolve_candidates(self, *, fame: int, ocr_class: str,
                           ocr_name: str = "",
                           window: int = 100,
                           name_min_similarity: float = 0.7,
                           ) -> tuple[JobInfo | None,
                                       list[FameCharacter], str]:
        """OCR class+fame → candidate characters.

        Single-shot lookup. Assumes the user is at Neo-tier awakening
        (true for ~all global-server endgame characters): we look up the
        Neo: JobInfo for the OCR'd class and call characters-fame once.
        If the OCR'd name was supplied, we also filter to candidates with
        sufficient name similarity. No digit variants, no expanding
        windows — this stays cheap and predictable, and the network is
        the bottleneck anyway.

        Returns (job, candidates_above_threshold, source). `candidates`
        is the FILTERED list: only entries with sim ≥ `name_min_similarity`
        when an OCR name was provided. `source` is a short label for logs.
        """
        if fame <= 0:
            return None, [], ""
        job = self._lookup_neo_job(ocr_class)
        if job is None:
            return None, [], ""

        rows = self.search_by_fame(job_id=job.job_id,
                                   job_grow_id=job.grow_id,
                                   fame=fame, window=window)
        source = f"{job.job_name}/{job.grow_name} ±{window}"
        if not rows:
            return job, [], source

        if not ocr_name:
            return job, rows, source

        scored = sorted(
            ((name_similarity(ocr_name, c.name), c) for c in rows),
            key=lambda t: t[0], reverse=True)
        accepted = [c for sim, c in scored if sim >= name_min_similarity]
        if not accepted:
            top3 = ", ".join(
                f"{c.name}(fame={c.fame},sim={s:.2f})"
                for s, c in scored[:3])
            _logger.info(
                "  reject %s: %d cands < %.2f sim. top: %s",
                source, len(rows), name_min_similarity, top3)
        return job, accepted, source

    def _lookup_neo_job(self, ocr_text: str) -> JobInfo | None:
        """Resolve OCR'd class label to the Neo-tier JobInfo. Falls back to
        whatever JobInfo the index produced if no Neo variant exists."""
        matches = self.match_jobs(ocr_text)
        if not matches:
            return None
        # Prefer Neo: tier — that's what every endgame character actually
        # has registered in characters-fame.
        for m in matches:
            if m.grow_name.lower().startswith("neo:"):
                return m
        return matches[0]

    def character_detail(self, server_id: str, character_id: str) -> CharacterDetail | None:
        """Fetch /df/servers/<srv>/characters/<id>. Cached."""
        if not self._api_key:
            return None
        key = (server_id, character_id)
        with self._cache_lock:
            entry = self._detail_cache.get(key)
        if entry is not None:
            ttl = self._neg_ttl if entry.value is None else self._ttl
            if (time.monotonic() - entry.fetched_at) < ttl:
                return entry.value  # type: ignore[return-value]

        if not self._limiter.acquire(timeout=self._timeout):
            return None
        url = f"{self._base_url}/df/servers/{server_id}/characters/{character_id}"
        try:
            resp = requests.get(url, params={"apikey": self._api_key},
                                timeout=self._timeout)
        except Exception as exc:
            _logger.warning("character detail %s/%s failed: %s",
                            server_id, character_id, exc)
            return None
        if resp.status_code == 404:
            with self._cache_lock:
                self._detail_cache[key] = _CacheEntry(value=None)
            return None
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _logger.warning("character detail %s/%s parse: %s",
                            server_id, character_id, exc)
            return None
        adventure = data.get("adventureName") or ""
        detail = CharacterDetail(
            server_id=server_id,
            character_id=character_id,
            name=data.get("characterName") or "",
            fame=data.get("fame"),
            adventure_name=adventure,
            guild_name=data.get("guildName") or "",
        )
        with self._cache_lock:
            self._detail_cache[key] = _CacheEntry(value=detail)
        return detail

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch_fame(self, server: str, lo: int, hi: int,
                    job_id: str, job_grow_id: str,
                    is_all_job_grow: bool = False) -> list[FameCharacter]:
        if not self._limiter.acquire(timeout=self._timeout):
            return []
        url = f"{self._base_url}/df/servers/{server}/characters-fame"
        # Param order matches the documented Neople spec:
        # minFame, maxFame, jobId, jobGrowId, isAllJobGrow, isBuff, limit, apikey
        params: dict = {"minFame": lo, "maxFame": hi}
        if job_id:
            params["jobId"] = job_id
        if job_grow_id:
            params["jobGrowId"] = job_grow_id
        params["isAllJobGrow"] = "true" if is_all_job_grow else "false"
        params["limit"] = 200
        params["apikey"] = self._api_key
        try:
            resp = requests.get(url, params=params, timeout=self._timeout)
        except Exception as exc:
            _logger.warning("fame search %s [%d..%d] job=%s grow=%s failed: %s",
                            server, lo, hi, job_id or "-",
                            job_grow_id or "-", exc)
            return []
        if resp.status_code == 404:
            return []
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _logger.warning("fame search %s [%d..%d] parse: %s",
                            server, lo, hi, exc)
            return []
        out: list[FameCharacter] = []
        for item in data.get("rows", []):
            name = item.get("characterName") or ""
            fame = item.get("fame")
            if not name or fame is None:
                continue
            out.append(FameCharacter(
                server_id=item.get("serverId") or server,
                character_id=item.get("characterId") or "",
                name=name,
                fame=int(fame),
                job_id=item.get("jobId") or "",
                job_name=item.get("jobName") or "",
                job_grow_id=item.get("jobGrowId") or "",
                job_grow_name=item.get("jobGrowName") or "",
            ))
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Visual-confusable digit substitutions, ordered by empirical likelihood:
# the FIRST list entry is the most common misread of that digit. This drives
# `digit_variants(...)` so the tightest variants get tried first.
_DIGIT_CONFUSABLES = {
    "0": ["8", "6"],
    "1": ["7"],
    "2": ["7"],
    "3": ["8", "0"],
    "4": ["9"],
    "5": ["6", "8", "3"],
    "6": ["5", "8", "0"],
    "7": ["1", "2"],
    "8": ["3", "0", "5", "6"],
    "9": ["4"],
}


def digit_variants(value: int) -> list[int]:
    """Generate plausible OCR-misread variants of `value` by swapping each
    digit with its visually-confusable alternates (one position at a time).

    Order: leading-digit variants first (most-significant errors are most
    impactful), then less-significant. Within each position, the most
    likely confusable comes first. Duplicates are dropped; the original is
    excluded.
    """
    digits = str(value)
    seen: set[int] = {value}
    out: list[int] = []
    for i, ch in enumerate(digits):
        for sub in _DIGIT_CONFUSABLES.get(ch, ()):
            cand_str = digits[:i] + sub + digits[i + 1:]
            try:
                cand = int(cand_str)
            except ValueError:
                continue
            if cand in seen:
                continue
            seen.add(cand)
            out.append(cand)
    return out


def _norm_jobname(name: str) -> str:
    """Normalize a class name for matching. Lowercases and drops whitespace
    and punctuation, but PRESERVES the 'Neo:' awakening-tier prefix as part
    of the key — in the global server ~all max-level characters are Neo
    tier, and 'Neo: <X>' has a different jobGrowId than the base '<X>'
    subclass. Stripping it would collapse those two into one key and
    silently pick the wrong jobGrowId."""
    s = name.strip().lower()
    return "".join(c for c in s if c.isalnum())


def _strip_class_prefix(ocr_text: str) -> str:
    """Drop awakening-tier badge characters that prefix the OCR'd class
    string. The 'Neo:' tier label itself is KEPT as part of the matcher
    input — in the global server it's a meaningful jobGrowName component
    and stripping it loses the awakening-vs-base distinction."""
    text = ocr_text.strip()
    # Strip a single leading non-Neo letter that's actually the [B]/[D]/[O]
    # awakening-tier badge OCR'd into a glyph (e.g. 'BNeo: Hunter').
    while text and text[0].isalpha() and (len(text) > 1 and text[1] in "Nn"):
        if text[1:].lower().startswith("neo"):
            text = text[1:]
        break
    return text.strip(" :;|")


def name_similarity(ocr_name: str, candidate: str) -> float:
    """Score OCR'd name against an API candidate. Truncated names ending in
    '...' are matched as prefixes."""
    def norm(value: str) -> str:
        v = value.replace("...", "")
        v = v.replace("|", "l")
        return "".join(c for c in v.lower() if c.isalnum())
    o = norm(ocr_name)
    c = norm(candidate)
    if not o or not c:
        return 0.0
    ratio = SequenceMatcher(None, o, c).ratio()
    if ocr_name.endswith("...") and c.startswith(o):
        ratio = max(ratio, min(0.95, len(o) / max(1, len(c)) + 0.25))
    return ratio
