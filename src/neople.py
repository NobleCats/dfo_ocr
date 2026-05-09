"""Neople DFO Open API client.

Wraps the endpoints used for fame-based applicant resolution:

* GET /df/jobs
* GET /df/servers/{server}/characters-fame
* GET /df/servers/{server}/characters/{characterId}

The resolver is intentionally conservative about API usage, but not about
ambiguous class names. Some Neo classes share the same grow name across gendered
jobs, for example Priest (M) / Neo: Crusader and Priest (F) / Neo: Crusader.
Those must be searched independently and merged before name filtering.
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

# Officially documented as ~5 rps for free tier. Stay below to leave headroom
# for parallel callers and to avoid 429s under bursty capture loops.
_DEFAULT_MAX_RPS = 4.0
_DEFAULT_TIMEOUT = 8.0
_DEFAULT_TTL = 600.0       # 10 min positive cache
_DEFAULT_NEG_TTL = 60.0    # 1 min negative cache
_DEFAULT_JOBS_TTL = 24 * 3600

# Neople exposes a synthetic "all" pseudo-server that aggregates the global
# cluster (cain + siroco). One query at "all" returns rows from both servers
# with their real serverId tagged in each row.
ALL_SERVERS = "all"


@dataclass(frozen=True)
class JobInfo:
    job_id: str
    job_name: str
    grow_id: str
    grow_name: str
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
    """Shared token bucket."""

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
                self._tokens = min(
                    self._rate,
                    self._tokens + (now - self._last) * self._rate,
                )
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
    """Thread-safe Neople API client. One instance per process is enough."""

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

        # Cache: jobs[norm(grow_name)] -> list[JobInfo].
        #
        # A list is required because gendered class pairs can share the same
        # grow name. Using a single-value dict silently drops one candidate and
        # causes wrong jobId/growId searches.
        self._jobs: dict[str, list[JobInfo]] = {}
        self._jobs_fetched_at: float = 0.0
        self._jobs_lock = threading.Lock()

        # fame search cache:
        #   (job_id, grow_id_or_star, fame_lo, fame_hi) -> list[FameCharacter]
        self._fame_cache: dict[tuple[str, str, int, int], _CacheEntry] = {}

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

        # Acquire outside the lock so other callers can use cached data while we
        # block on rate-limited refresh.
        if not self._limiter.acquire(timeout=self._timeout):
            return self._jobs

        url = f"{self._base_url}/df/jobs"
        try:
            resp = requests.get(url, params={"apikey": self._api_key}, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _logger.warning("jobs fetch failed: %s", exc)
            return self._jobs

        index: dict[str, list[JobInfo]] = {}

        def _walk_grow(job_id: str, job_name: str, grow: dict) -> None:
            """Recursively register a jobGrow plus its awakening chain."""

            if not isinstance(grow, dict):
                return

            grow_id = grow.get("jobGrowId") or ""
            grow_name = grow.get("jobGrowName") or ""

            if grow_name:
                info = JobInfo(
                    job_id=job_id,
                    job_name=job_name,
                    grow_id=grow_id,
                    grow_name=grow_name,
                    is_buff=bool(grow.get("isBuffer") or grow.get("isBuff")),
                )

                full_key = _norm_jobname(grow_name)
                if full_key:
                    index.setdefault(full_key, []).append(info)

                # Also register under the base-name key (Neo: stripped) so OCR
                # that loses or corrupts the Neo prefix still finds the class.
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
            if key not in index:
                grows = job.get("rows") or []
                if grows:
                    grow = grows[-1]
                    index[key] = [
                        JobInfo(
                            job_id=job.get("jobId") or "",
                            job_name=job_name,
                            grow_id=grow.get("jobGrowId") or "",
                            grow_name=grow.get("jobGrowName") or "",
                            is_buff=bool(grow.get("isBuffer")),
                        )
                    ]

        with self._jobs_lock:
            self._jobs = index
            self._jobs_fetched_at = time.monotonic()

        neo_keys = sorted(k for k in index if k.startswith("neo"))
        base_keys = sorted(k for k in index if not k.startswith("neo"))
        _logger.info(
            "jobs index loaded: %d total keys (%d 'neo*', %d base)",
            len(index),
            len(neo_keys),
            len(base_keys),
        )
        _logger.info("  neo* keys: %s", ", ".join(neo_keys) if neo_keys else "(none)")
        _logger.info("  sample base keys: %s", ", ".join(base_keys[:20]))

        return index

    def match_jobs(self, ocr_text: str) -> list[JobInfo]:
        """Resolve OCR'd class label to all plausible JobInfo entries."""

        text = _strip_class_prefix(ocr_text)
        if not text:
            return []

        index = self.jobs()
        if not index:
            return []

        # Build candidates: full text, plus long tokens. Tokens help when a
        # leading badge or "Neo:" OCR artifact drowns out the actual class name.
        tokens = [t for t in re.split(r"[\s:'\-]+", text) if len(t) >= 4]
        candidates = [text] + tokens
        normalized = [_norm_jobname(c) for c in candidates if _norm_jobname(c)]
        if not normalized:
            return []

        # Direct hit on any candidate: return all entries at that key.
        for n in normalized:
            if n in index:
                return _dedupe_jobs(index[n])

        best_score = 0.0
        best_key: str | None = None

        for key in index:
            for n in normalized:
                score = SequenceMatcher(None, n, key).ratio()
                if score > best_score:
                    best_score, best_key = score, key

        if best_key is not None and best_score >= 0.6:
            return _dedupe_jobs(index[best_key])

        return []

    def match_job(self, ocr_text: str) -> JobInfo | None:
        """Backwards-compatible single-best wrapper around match_jobs()."""

        matches = self.match_jobs(ocr_text)
        return matches[0] if matches else None

    def search_by_fame(
        self,
        *,
        job_id: str,
        job_grow_id: str,
        fame: int,
        window: int = 1,
        all_job_grow: bool = False,
    ) -> list[FameCharacter]:
        """Fetch all chars on ALL_SERVERS at fame in [fame-window, fame+window]."""

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

        out = self._fetch_fame(
            ALL_SERVERS,
            lo,
            hi,
            job_id,
            "" if all_job_grow else job_grow_id,
            is_all_job_grow=all_job_grow,
        )
        _logger.debug(
            "  search fame=[%d..%d] jobId=%s growId=%s allGrow=%s → %d rows",
            lo,
            hi,
            job_id[:8] if job_id else "-",
            job_grow_id[:8] if job_grow_id else "-",
            all_job_grow,
            len(out),
        )

        with self._cache_lock:
            self._fame_cache[key] = _CacheEntry(value=out)

        return out

    def expanding_search(
        self,
        *,
        job_id: str,
        job_grow_id: str,
        fame: int,
        steps: tuple[int, ...] = (1, 5, 20, 50, 100),
        min_results: int = 1,
    ) -> list[FameCharacter]:
        """Try increasing fame windows until we get >= min_results."""

        last: list[FameCharacter] = []
        for window in steps:
            last = self.search_by_fame(
                job_id=job_id,
                job_grow_id=job_grow_id,
                fame=fame,
                window=window,
            )
            if len(last) >= min_results:
                return last
        return last

    def resolve_candidates(
        self,
        *,
        fame: int,
        ocr_class: str,
        ocr_name: str = "",
        window: int = 100,
        fame_range_min: int | None = None,
        fame_range_max: int | None = None,
        name_min_similarity: float = 0.62,
    ) -> tuple[JobInfo | None, list[FameCharacter], str]:
        """OCR class+fame -> candidate characters.

        Important behavior:
        - Ambiguous classes, such as Neo: Crusader, search every matching
          job/grow pair.
        - Partial fame prefix ranges use the exact provided [min..max].
        - Exact fame OCR first searches [fame..fame] before falling back to
          ±window. This avoids rejecting correct candidates solely because a
          name was OCR'd poorly.
        """

        use_range = fame_range_min is not None and fame_range_max is not None
        if not use_range and fame <= 0:
            return None, [], ""

        jobs = self._lookup_neo_jobs(ocr_class)
        if not jobs:
            return None, [], ""

        if len(jobs) > 1:
            candidates_display = ", ".join(f"'{j.job_name}/{j.grow_name}'" for j in jobs)
            _logger.info(
                "class_ambiguous key/class=%r candidates=[%s]",
                ocr_class,
                candidates_display,
            )

        # Partial fame prefix fallback: use only the bounded range.
        if use_range:
            _logger.info(
                "resolve_with_partial_fame_range class=%r name=%r range=[%d..%d]",
                ocr_class,
                ocr_name,
                fame_range_min,
                fame_range_max,
            )
            rows, source = self._search_jobs_by_range(
                jobs,
                fame_min=fame_range_min,
                fame_max=fame_range_max,
            )
            accepted = self._accept_candidates(
                rows=rows,
                jobs=jobs,
                source=source,
                ocr_name=ocr_name,
                fame=fame if fame > 0 else None,
                exact_mode=False,
                bounded_range=True,
                name_min_similarity=name_min_similarity,
            )
            return self._job_for_accepted(jobs, accepted), accepted, source

        # Exact fame fast path. This is the key fix for cases like:
        # OCR name="MeMeSa:", exact fame=74733, class=Neo: Crusader,
        # API top candidate NeNeSan(fame=74733, sim≈0.62).
        _logger.info(
            "resolve_exact_fame_search class=%r name=%r fame=%d",
            ocr_class,
            ocr_name,
            fame,
        )
        exact_rows, exact_source = self._search_jobs_by_range(
            jobs,
            fame_min=fame,
            fame_max=fame,
        )
        exact_accepted = self._accept_candidates(
            rows=exact_rows,
            jobs=jobs,
            source=exact_source,
            ocr_name=ocr_name,
            fame=fame,
            exact_mode=True,
            bounded_range=False,
            name_min_similarity=name_min_similarity,
        )
        if exact_accepted:
            return self._job_for_accepted(jobs, exact_accepted), exact_accepted, exact_source

        # Fallback to the existing ±window behavior when exact fame returns no
        # acceptable candidate. This keeps compatibility with slightly stale or
        # misread fame values.
        _logger.info(
            "fallback_to_fame_window class=%r name=%r fame=%d window=%d",
            ocr_class,
            ocr_name,
            fame,
            window,
        )
        window_rows, window_source = self._search_jobs_by_window(
            jobs,
            fame=fame,
            window=window,
        )
        window_accepted = self._accept_candidates(
            rows=window_rows,
            jobs=jobs,
            source=window_source,
            ocr_name=ocr_name,
            fame=fame,
            exact_mode=False,
            bounded_range=False,
            name_min_similarity=name_min_similarity,
        )
        return self._job_for_accepted(jobs, window_accepted), window_accepted, window_source

    def character_detail(self, server_id: str, character_id: str) -> CharacterDetail | None:
        """Fetch /df/servers/{server_id}/characters/{character_id}. Cached."""

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
            resp = requests.get(url, params={"apikey": self._api_key}, timeout=self._timeout)
        except Exception as exc:
            _logger.warning("character detail %s/%s failed: %s", server_id, character_id, exc)
            return None

        if resp.status_code == 404:
            with self._cache_lock:
                self._detail_cache[key] = _CacheEntry(value=None)
            return None

        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _logger.warning("character detail %s/%s parse: %s", server_id, character_id, exc)
            return None

        detail = CharacterDetail(
            server_id=server_id,
            character_id=character_id,
            name=data.get("characterName") or "",
            fame=data.get("fame"),
            adventure_name=data.get("adventureName") or "",
            guild_name=data.get("guildName") or "",
        )

        with self._cache_lock:
            self._detail_cache[key] = _CacheEntry(value=detail)

        return detail

    # ------------------------------------------------------------------
    # Resolve helpers
    # ------------------------------------------------------------------

    def _lookup_neo_jobs(self, ocr_text: str) -> list[JobInfo]:
        """Return every plausible Neo-tier JobInfo for OCR'd class text."""

        matches = self.match_jobs(ocr_text)
        if not matches:
            return []

        neo = [m for m in matches if m.grow_name.lower().startswith("neo:")]
        return _dedupe_jobs(neo if neo else matches)

    def _lookup_neo_job(self, ocr_text: str) -> JobInfo | None:
        """Backwards-compatible single-result wrapper around _lookup_neo_jobs()."""

        jobs = self._lookup_neo_jobs(ocr_text)
        return jobs[0] if jobs else None

    def _search_jobs_by_window(
        self,
        jobs: list[JobInfo],
        *,
        fame: int,
        window: int,
    ) -> tuple[list[FameCharacter], str]:
        all_rows: list[FameCharacter] = []
        source_parts: list[str] = []

        for job in jobs:
            rows = self.search_by_fame(
                job_id=job.job_id,
                job_grow_id=job.grow_id,
                fame=fame,
                window=window,
            )
            src = f"{job.job_name}/{job.grow_name} ±{window}"
            _logger.debug("  searched %s → %d rows", src, len(rows))
            source_parts.append(src)
            all_rows.extend(rows)

        return _dedupe_characters(all_rows), " | ".join(source_parts)

    def _search_jobs_by_range(
        self,
        jobs: list[JobInfo],
        *,
        fame_min: int,
        fame_max: int,
    ) -> tuple[list[FameCharacter], str]:
        all_rows: list[FameCharacter] = []
        source_parts: list[str] = []

        for job in jobs:
            rows = self._search_by_fame_range(
                job_id=job.job_id,
                job_grow_id=job.grow_id,
                fame_min=fame_min,
                fame_max=fame_max,
            )
            src = f"{job.job_name}/{job.grow_name} [{fame_min}..{fame_max}]"
            _logger.debug("  searched %s → %d rows", src, len(rows))
            source_parts.append(src)
            all_rows.extend(rows)

        return _dedupe_characters(all_rows), " | ".join(source_parts)

    def _accept_candidates(
        self,
        *,
        rows: list[FameCharacter],
        jobs: list[JobInfo],
        source: str,
        ocr_name: str,
        fame: int | None,
        exact_mode: bool,
        bounded_range: bool,
        name_min_similarity: float,
    ) -> list[FameCharacter]:
        """Rank and accept candidates.

        This function deliberately trusts highly constrained fame/class matches
        more than OCR name similarity. Names are short and OCR often confuses
        repeated glyphs, e.g. NeNeSan -> MeMeSa.
        """

        if not rows:
            return []

        if not ocr_name:
            _logger.info("accept_no_ocr_name %s: %d candidates", source, len(rows))
            return rows

        scored = sorted(
            (
                (
                    name_similarity(ocr_name, c.name),
                    abs(c.fame - fame) if fame is not None else 0,
                    c,
                )
                for c in rows
            ),
            key=lambda t: (t[1], -t[0], t[2].name.lower()),
        )

        top_sim, top_dist, top_char = scored[0]
        top3 = ", ".join(
            f"{c.name}(fame={c.fame},sim={sim:.3f},dist={dist})"
            for sim, dist, c in scored[:3]
        )

        _logger.info(
            "candidate_ranking %s: count=%d top=%s",
            source,
            len(rows),
            top3,
        )

        # Partial bounded range: if the range is tight and has exactly one
        # merged candidate, accept it. If multiple remain, use a moderately
        # strict name threshold.
        if bounded_range:
            if len(rows) == 1:
                _logger.info(
                    "accept_partial_fame_unique_candidate %s: name_ocr=%r canonical=%s fame=%d grow=%s",
                    source,
                    ocr_name,
                    top_char.name,
                    top_char.fame,
                    top_char.job_grow_name,
                )
                return [top_char]

            accepted = [c for sim, _dist, c in scored if sim >= max(0.58, name_min_similarity - 0.02)]
            if accepted:
                _logger.info(
                    "accept_partial_fame_name_candidate %s: name_ocr=%r canonical=%s sim=%.3f",
                    source,
                    ocr_name,
                    accepted[0].name,
                    name_similarity(ocr_name, accepted[0].name),
                )
                return accepted

        # Exact fame search: this is highly constrained. If one candidate remains
        # after exact fame + class/grow search, accept it even when the OCR name
        # is poor.
        if exact_mode:
            if len(rows) == 1:
                _logger.info(
                    "accept_exact_fame_unique_candidate %s: name_ocr=%r canonical=%s fame=%d grow=%s sim=%.3f",
                    source,
                    ocr_name,
                    top_char.name,
                    top_char.fame,
                    top_char.job_grow_name,
                    top_sim,
                )
                return [top_char]

            if top_dist == 0 and top_sim >= 0.55:
                _logger.info(
                    "accept_exact_fame_top_candidate %s: name_ocr=%r canonical=%s fame=%d grow=%s sim=%.3f candidates=%d",
                    source,
                    ocr_name,
                    top_char.name,
                    top_char.fame,
                    top_char.job_grow_name,
                    top_sim,
                    len(rows),
                )
                return [top_char]

        # General ±window fallback: keep the normal threshold, but allow a
        # slightly softer accept when fame distance is exact. This catches cases
        # where exact [fame..fame] search returned a broader-than-expected API
        # result set or was skipped by cache/data behavior.
        accepted = [c for sim, _dist, c in scored if sim >= name_min_similarity]
        if accepted:
            return accepted

        if top_dist == 0 and top_sim >= 0.55:
            _logger.info(
                "accept_fame_exact_soft_name %s: name_ocr=%r canonical=%s fame=%d grow=%s sim=%.3f < %.3f candidates=%d",
                source,
                ocr_name,
                top_char.name,
                top_char.fame,
                top_char.job_grow_name,
                top_sim,
                name_min_similarity,
                len(rows),
            )
            return [top_char]

        _logger.info(
            "reject %s: %d cands < %.2f sim. top: %s",
            source,
            len(rows),
            name_min_similarity,
            top3,
        )
        return []

    def _job_for_accepted(self, jobs: list[JobInfo], accepted: list[FameCharacter]) -> JobInfo | None:
        if not jobs:
            return None
        if not accepted:
            return jobs[0]

        c = accepted[0]
        for job in jobs:
            if job.job_id == c.job_id and job.grow_id == c.job_grow_id:
                return job
        for job in jobs:
            if job.grow_id == c.job_grow_id:
                return job
        return jobs[0]

    def _search_by_fame_range(
        self,
        *,
        job_id: str,
        job_grow_id: str,
        fame_min: int,
        fame_max: int,
    ) -> list[FameCharacter]:
        """Search fame in an explicit [fame_min..fame_max] range."""

        if not self._api_key or fame_min <= 0:
            return []

        lo = max(1, min(fame_min, fame_max))
        hi = max(lo, max(fame_min, fame_max))
        key = (job_id, job_grow_id, lo, hi)

        with self._cache_lock:
            entry = self._fame_cache.get(key)
            if entry is not None:
                ttl = self._neg_ttl if not entry.value else self._ttl
                if (time.monotonic() - entry.fetched_at) < ttl:
                    return list(entry.value)  # type: ignore[arg-type]

        out = self._fetch_fame(ALL_SERVERS, lo, hi, job_id, job_grow_id)
        _logger.debug(
            "  range fame=[%d..%d] jobId=%s growId=%s → %d rows",
            lo,
            hi,
            job_id[:8] if job_id else "-",
            job_grow_id[:8] if job_grow_id else "-",
            len(out),
        )

        with self._cache_lock:
            self._fame_cache[key] = _CacheEntry(value=out)

        return out

    # ------------------------------------------------------------------
    # HTTP internals
    # ------------------------------------------------------------------

    def _fetch_fame(
        self,
        server: str,
        lo: int,
        hi: int,
        job_id: str,
        job_grow_id: str,
        is_all_job_grow: bool = False,
    ) -> list[FameCharacter]:
        if not self._limiter.acquire(timeout=self._timeout):
            return []

        url = f"{self._base_url}/df/servers/{server}/characters-fame"
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
            _logger.warning(
                "fame search %s [%d..%d] job=%s grow=%s failed: %s",
                server,
                lo,
                hi,
                job_id or "-",
                job_grow_id or "-",
                exc,
            )
            return []

        if resp.status_code == 404:
            return []

        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _logger.warning("fame search %s [%d..%d] parse: %s", server, lo, hi, exc)
            return []

        out: list[FameCharacter] = []
        for item in data.get("rows", []):
            name = item.get("characterName") or ""
            fame = item.get("fame")
            if not name or fame is None:
                continue

            out.append(
                FameCharacter(
                    server_id=item.get("serverId") or server,
                    character_id=item.get("characterId") or "",
                    name=name,
                    fame=int(fame),
                    job_id=item.get("jobId") or "",
                    job_name=item.get("jobName") or "",
                    job_grow_id=item.get("jobGrowId") or "",
                    job_grow_name=item.get("jobGrowName") or "",
                )
            )

        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Visual-confusable digit substitutions. The first list entry is the most common
# misread of that digit.
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
    """Generate plausible OCR-misread variants by swapping one digit."""

    digits = str(value)
    seen: set[int] = {value}
    out: list[int] = []

    for i, ch in enumerate(digits):
        for sub in _DIGIT_CONFUSABLES.get(ch, ()):
            cand_str = digits[:i] + sub + digits[i + 1 :]
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
    """Normalize a class name for matching."""

    s = name.strip().lower()
    return "".join(c for c in s if c.isalnum())


def _strip_class_prefix(ocr_text: str) -> str:
    """Drop a leading OCR'd awakening-tier badge while preserving Neo:."""

    text = ocr_text.strip()

    # Strip a single leading non-Neo letter that's actually the [B]/[D]/[O]
    # awakening-tier badge OCR'd into a glyph, e.g. "BNeo: Crusader".
    while text and text[0].isalpha() and (len(text) > 1 and text[1] in "Nn"):
        if text[1:].lower().startswith("neo"):
            text = text[1:]
        break

    return text.strip(" :;|")


def name_similarity(ocr_name: str, candidate: str) -> float:
    """Score OCR'd name against an API candidate."""

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


def _dedupe_jobs(jobs: list[JobInfo]) -> list[JobInfo]:
    seen: set[tuple[str, str]] = set()
    out: list[JobInfo] = []

    for job in jobs:
        key = (job.job_id, job.grow_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(job)

    return out


def _dedupe_characters(rows: list[FameCharacter]) -> list[FameCharacter]:
    seen: set[tuple[str, str, str, int]] = set()
    out: list[FameCharacter] = []

    for row in rows:
        key = (
            row.character_id or "",
            row.server_id or "",
            row.name or "",
            row.fame,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)

    return out
