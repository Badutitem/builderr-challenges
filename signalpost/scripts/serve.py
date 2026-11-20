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
import html as html_lib
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

    def list_companies(self) -> list[dict[str, Any]]:
        base = self.root / "snapshots"
        rows: list[dict[str, Any]] = []
        if not base.exists():
            return rows
        for child in sorted(base.iterdir()):
            latest = child / "latest.json"
            if not (child.is_dir() and latest.exists()):
                continue
            try:
                stored = json.loads(latest.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            profile = stored.get("profile") or {}
            envelope = stored.get("envelope") or {}
            rows.append({
                "organisation_number": child.name,
                "name": profile.get("name") or "",
                "terminal_status": (envelope.get("run") or {}).get("terminal_status") or "",
            })
        return rows

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


_PAGE_CSS = (
    "body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}"
    "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}"
    ".na{color:#8a6d00}.ok{color:#0a6b2d}code{background:#f2f2f2;padding:.1rem .3rem}input,button{font-size:1rem;padding:.4rem}"
)


def _esc(value: Any) -> str:
    return html_lib.escape(json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value)


def render_index(companies: list[dict[str, Any]]) -> str:
    rows = "".join(
        f'<tr><td><a href="/view/{html_lib.escape(c["organisation_number"])}">{html_lib.escape(c["organisation_number"])}</a></td>'
        f'<td>{html_lib.escape(c["name"])}</td><td>{html_lib.escape(c["terminal_status"])}</td></tr>'
        for c in companies
    ) or '<tr><td colspan="3">No stored companies yet — look one up above.</td></tr>'
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Signalpost — Norway company research</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        "<h1>Signalpost — Norway company research</h1>"
        "<p>Every value below is backed by claim-level evidence; missing data is labelled, never shown as zero.</p>"
        "<form onsubmit=\"location='/view/'+document.getElementById('org').value;return false\">"
        "<input id='org' pattern='[0-9]{9}' placeholder='Organisation number (9 digits)' required> "
        "<button type='submit'>Find company</button></form>"
        "<h2>Researched companies</h2><table><tr><th>Org number</th><th>Name</th><th>Status</th></tr>"
        f"{rows}</table>"
        "<p>JSON API: <code>/companies/:org</code> · <code>/research?org=…&amp;q=…</code> · <code>/refresh?org=…</code></p>"
        "</body></html>"
    )


def render_company(envelope: dict[str, Any]) -> str:
    org = html_lib.escape(str(envelope.get("organisation_number") or ""))
    evidence_by_id = {item.get("id"): item for item in envelope.get("evidence") or []}
    claim_rows = []
    for claim in envelope.get("claims") or []:
        availability = str(claim.get("availability") or "")
        css = "ok" if availability == "available" else "na"
        links = []
        for ev_id in claim.get("evidence_ids") or []:
            item = evidence_by_id.get(ev_id) or {}
            url = item.get("source_url")
            if url:
                links.append(f'<a href="{html_lib.escape(str(url))}">verify source</a>')
        claim_rows.append(
            f"<tr><td>{html_lib.escape(str(claim.get('field')))}</td>"
            f"<td>{_esc(claim.get('value'))}</td>"
            f"<td class='{css}'>{html_lib.escape(availability)}</td>"
            f"<td>{' · '.join(links)}</td></tr>"
        )
    unknowns = ", ".join(html_lib.escape(str(u)) for u in envelope.get("unknowns") or []) or "none"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{org} — Signalpost</title><style>{_PAGE_CSS}</style></head><body>"
        f"<p><a href='/'>← All companies</a></p><h1>{org}</h1>"
        f"<p>{html_lib.escape(str(envelope.get('summary') or ''))}</p>"
        "<table><tr><th>Field</th><th>Value</th><th>Availability</th><th>Evidence</th></tr>"
        f"{''.join(claim_rows)}</table>"
        f"<p><strong>Unknowns (labelled, not zero):</strong> {unknowns}</p>"
        f"<p>Compare/refresh: <a href='/refresh?org={org}'>refresh against sources</a> · "
        f"<a href='/companies/{org}'>raw JSON envelope</a></p>"
        "</body></html>"
    )


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

        def _send_html(self, status: int, page: str) -> None:
            raw = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
            if len(parts) == 2 and parts[0] == "view":
                org = self._org_or_400(parts[1])
                if org:
                    _profile, envelope = agent.research_company(org, live=False)
                    self._send_html(200, render_company(envelope))
                return
            if parts == ["health"]:
                self._send(200, {"status": "ok", "routes": ["/", "/view/:organisation_number", "/companies/:organisation_number", "/research", "/refresh"]})
                return
            if parts == []:
                self._send_html(200, render_index(agent.store.list_companies()))
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
