#!/usr/bin/env python3
"""HTTP surface for the preferred Signalpost routes (standard library only).

Routes:
  GET  /companies/{organisation_number}  -> contract envelope (stored snapshot
                                            if present, otherwise researched
                                            live and stored)
  GET  /research?org={org}&q={question}  -> evidence-bounded answer
  POST /research {"organisation_number": ..., "question": ...}
  POST /refresh  {"organisation_number": ...}  (or GET /refresh?org=...)
                                            -> live re-research, diff against
                                            the stored snapshot, append the
                                            new snapshot; history files are
                                            never overwritten

Snapshots live under --store/snapshots/{org}/{retrieved-at}.json with a
``latest.json`` copy. Refresh always bypasses the HTTP disk cache so its
timestamps are genuine; company reads use the cache.

Run:
    uv run python scripts/serve.py --store out/server-store --port 8765
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.cachefetch import CachingFetcher  # noqa: E402
from norway_company_agent.contract import build_envelope  # noqa: E402
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.live_profile import build_profile_live  # noqa: E402
from norway_company_agent.research import answer_profile  # noqa: E402

ORG_PATTERN = re.compile(r"^\d{9}$")


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "snapshots").mkdir(parents=True, exist_ok=True)

    def _dir(self, org: str) -> Path:
        path = self.root / "snapshots" / org
        path.mkdir(parents=True, exist_ok=True)
        return path

    def latest(self, org: str) -> dict[str, Any] | None:
        path = self._dir(org) / "latest.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, org: str, payload: dict[str, Any]) -> Path:
        directory = self._dir(org)
        stamp = utc_now().replace(":", "").replace("-", "")
        path = directory / f"{stamp}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        (directory / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return path


class Agent:
    def __init__(self, store: Store, cache_dir: Path) -> None:
        self.store = store
        self.cached_fetcher = CachingFetcher(cache_dir, use_cache=True)
        self.live_fetcher = CachingFetcher(cache_dir, use_cache=False)

    def research_company(self, org: str, *, live: bool) -> tuple[dict[str, Any], dict[str, Any] | None]:
        stored = self.store.latest(org)
        previous_profile = stored["profile"] if stored else None
        if stored and not live:
            return stored["profile"], stored["envelope"]
        fetcher = self.live_fetcher if live else self.cached_fetcher
        profile, metric = build_profile_live(org, fetcher)
        envelope = build_envelope(
            profile,
            run_id=f"server-{utc_now()}",
            started_at=utc_now(),
            completed_at=utc_now(),
            runtime_ms=int(metric.get("runtime_ms") or 0),
            requests=int(metric.get("requests") or 0),
            previous_profile=previous_profile if live else None,
        )
        self.store.save(org, {"profile": profile, "envelope": envelope})
        return profile, envelope


def make_handler(agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # keep stdout clean
            pass

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _org_or_400(self, org: str | None) -> str | None:
            if org and ORG_PATTERN.match(org):
                return org
            self._send(400, {"error": "organisation_number must be exactly 9 digits"})
            return None

        def do_GET(self) -> None:
            try:
                self._get()
            except Exception as exc:  # one bad company must not drop the connection
                self._send(500, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})

        def _get(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 2 and parts[0] == "companies":
                org = self._org_or_400(parts[1])
                if org:
                    _profile, envelope = agent.research_company(org, live=False)
                    self._send(200, envelope)
                return
            if parts == ["research"]:
                org = self._org_or_400((query.get("org") or [None])[0])
                if org:
                    profile, _envelope = agent.research_company(org, live=False)
                    question = (query.get("q") or ["What do we know about this company?"])[0]
                    self._send(200, answer_profile(profile, question))
                return
            if parts == ["refresh"]:
                org = self._org_or_400((query.get("org") or [None])[0])
                if org:
                    self._refresh(org)
                return
            if parts == [] or parts == ["health"]:
                self._send(200, {"status": "ok", "routes": ["/companies/:organisation_number", "/research", "/refresh"]})
                return
            self._send(404, {"error": "unknown route"})

        def do_POST(self) -> None:
            try:
                self._post()
            except Exception as exc:
                self._send(500, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})

        def _post(self) -> None:
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            body = self._body()
            if parts == ["research"]:
                org = self._org_or_400(str(body.get("organisation_number") or ""))
                if org:
                    profile, _envelope = agent.research_company(org, live=False)
                    self._send(200, answer_profile(profile, str(body.get("question") or "What do we know about this company?")))
                return
            if parts == ["refresh"]:
                org = self._org_or_400(str(body.get("organisation_number") or ""))
                if org:
                    self._refresh(org)
                return
            self._send(404, {"error": "unknown route"})

        def _refresh(self, org: str) -> None:
            _profile, envelope = agent.research_company(org, live=True)
            self._send(200, {
                "organisation_number": org,
                "changes": envelope["changes"],
                "material_changes": len(envelope["changes"]),
                "envelope": envelope,
            })

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Signalpost HTTP routes")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--store", default="out/server-store")
    parser.add_argument("--cache-dir", default="cache/brreg")
    args = parser.parse_args()
    store = Store(Path(args.store))
    agent = Agent(store, Path(args.cache_dir))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(agent))
    print(f"Signalpost agent listening on http://{args.host}:{args.port} (store: {args.store})")
    server.serve_forever()


if __name__ == "__main__":
    main()
