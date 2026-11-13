"""Disk-caching, rate-aware JSON fetcher with the same contract as ``http.fetch_json``.

Design goals (Brønnøysundregistrene politeness + reproducibility):

- Every network response is stored on disk keyed by the SHA-256 of the URL, so
  reruns and refresh-idempotency checks do not re-hit the public APIs.
- The annual-account-copy endpoint (``/aarsregnskap/kopi/.../aar``) is known to
  allow roughly 30 request starts/minute; network starts for that endpoint are
  spaced at least ``history_interval`` seconds apart process-wide.
- A small global minimum interval keeps overall request starts gentle.
- Retries mirror ``http.fetch_json``: 404/410 return immediately, other
  failures retry with exponential backoff. Attempt/retry counts are exposed in
  ``stats`` so run reports can state errors and retries honestly.
- A fresh cache entry is returned with its original ``retrieved_at`` so
  evidence timestamps always reflect the real retrieval time. Callers that
  need a guaranteed-live read (refresh) construct the fetcher with
  ``use_cache=False``.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .http import FetchResult, _utc_now

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
HISTORY_MARKER = "/regnskapsregisteret/regnskap/aarsregnskap/kopi/"


class CachingFetcher:
    def __init__(
        self,
        cache_dir: str | Path | None,
        *,
        use_cache: bool = True,
        ttl_seconds: float | None = None,
        timeout: float = 20.0,
        attempts: int = 3,
        min_interval: float = 0.05,
        # The filing-copy ("kopi") family is limited to ~30 request starts
        # per minute. 2.1s matches the kit's own client for one process;
        # multi-worker runs raise the per-process value via
        # SIGNALPOST_KOPI_SPACING_SECONDS (workers x 2.1s keeps the
        # aggregate at ~30 starts/minute).
        history_interval: float = float(os.environ.get("SIGNALPOST_KOPI_SPACING_SECONDS", "2.1")),
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.use_cache = use_cache and self.cache_dir is not None
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self.attempts = attempts
        self.min_interval = min_interval
        self.history_interval = history_interval
        self.stats: dict[str, int] = {
            "network_requests": 0,
            "network_attempts": 0,
            "retries": 0,
            "cache_hits": 0,
            "errors": 0,
        }
        self._lock = threading.Lock()
        self._last_start = 0.0
        self._last_history_start = 0.0

    # -- cache ---------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"  # type: ignore[operator]

    def _read_cache(self, url: str) -> FetchResult | None:
        path = self._cache_path(url)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if self.ttl_seconds is not None:
            age = time.time() - float(stored.get("stored_epoch", 0))
            if age > self.ttl_seconds:
                return None
        return FetchResult(
            url=url,
            status=int(stored["status"]),
            elapsed_ms=0,
            bytes_received=int(stored.get("bytes_received", 0)),
            body=stored.get("body"),
            error=stored.get("error"),
            content_sha256=stored.get("content_sha256"),
            retrieved_at=stored.get("retrieved_at"),
            effective_at=stored.get("effective_at"),
        )

    def _write_cache(self, result: FetchResult) -> None:
        if self.cache_dir is None or result.status == 0:
            return
        payload = {
            "url": result.url,
            "status": result.status,
            "body": result.body,
            "error": result.error,
            "bytes_received": result.bytes_received,
            "content_sha256": result.content_sha256,
            "retrieved_at": result.retrieved_at,
            "effective_at": result.effective_at,
            "stored_epoch": time.time(),
        }
        path = self._cache_path(result.url)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    # -- rate limiting ---------------------------------------------------
    def _reserve_slot(self, url: str) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last_start)
            if HISTORY_MARKER in url:
                delay = max(delay, self.history_interval - (now - self._last_history_start))
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._last_start = now
            if HISTORY_MARKER in url:
                self._last_history_start = now

    # -- fetch -----------------------------------------------------------
    def __call__(self, url: str) -> FetchResult:
        if self.use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                with self._lock:
                    self.stats["cache_hits"] += 1
                return cached
        result = self._fetch_network(url)
        if result.status != 0:
            self._write_cache(result)
        return result

    def _fetch_network(self, url: str) -> FetchResult:
        last_error = "request failed"
        for attempt in range(self.attempts):
            self._reserve_slot(url)
            with self._lock:
                self.stats["network_attempts"] += 1
                if attempt == 0:
                    self.stats["network_requests"] += 1
                else:
                    self.stats["retries"] += 1
            started = time.monotonic()
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    elapsed = int((time.monotonic() - started) * 1000)
                    return FetchResult(
                        url,
                        response.status,
                        elapsed,
                        len(raw),
                        json.loads(raw),
                        content_sha256=hashlib.sha256(raw).hexdigest(),
                        retrieved_at=_utc_now(),
                    )
            except urllib.error.HTTPError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                raw = exc.read()
                if exc.code in {404, 410}:
                    return FetchResult(
                        url,
                        exc.code,
                        elapsed,
                        len(raw),
                        error=f"HTTP {exc.code}",
                        content_sha256=hashlib.sha256(raw).hexdigest(),
                        retrieved_at=_utc_now(),
                    )
                last_error = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, http.client.HTTPException, ConnectionError, OSError) as exc:
                # RemoteDisconnected/ConnectionReset are HTTPException/OSError,
                # not URLError; a dropped connection must become a retry, then
                # an honest FetchResult(status=0), never an exception.
                last_error = type(exc).__name__
            if attempt + 1 < self.attempts:
                time.sleep(0.4 * (2**attempt))
        with self._lock:
            self.stats["errors"] += 1
        return FetchResult(url, 0, 0, 0, error=last_error, retrieved_at=_utc_now())
