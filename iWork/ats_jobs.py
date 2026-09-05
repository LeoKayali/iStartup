"""Fetch postings directly from ATS job boards.

jobspy scrapes aggregators. Measured against them, that path cannot work:
ZipRecruiter answers every request with 403 forbidden cf-waf, and Indeed's
viewjob pages expose no application form to headless Playwright, so a run
across 10 postings produced 10 x "no file input found" and zero submissions.

ATS boards are the opposite trade. They publish read-only JSON with no auth,
no rate limit worth worrying about, and their application pages carry a real
input[type=file] that apply_job.py can complete.

The cost is that ATS boards are per-company, so you curate a list instead of
running a keyword search across the whole market. For a targeted search that is
an improvement: you choose the employers.

Every fetcher returns the shape search_jobs.py already emits:
    {"title", "url", "description", "company"}
"""

import datetime
import html
import json
import os
import re
import sys

TIMEOUT = 20
TAGS = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")


def log(message):
    print(message, file=sys.stderr, flush=True)


def _get_json(url):
    # Imported lazily so selftest can exercise the parsers without requests.
    import requests

    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "iWork/1.0"})
    r.raise_for_status()
    return r.json()


def _text(raw, limit):
    """ATS descriptions arrive as HTML, escaped HTML, or plain text."""
    if not raw:
        return ""
    s = html.unescape(str(raw))
    s = TAGS.sub(" ", s)
    return WHITESPACE.sub(" ", s).strip()[:limit]


def _fresh(when, cutoff):
    """True if the posting is new enough. Undated postings are kept."""
    if when is None or cutoff is None:
        return True
    try:
        if isinstance(when, (int, float)):
            # Lever uses epoch milliseconds.
            ts = datetime.datetime.fromtimestamp(when / 1000, datetime.timezone.utc)
        else:
            ts = datetime.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
        return ts >= cutoff
    except (ValueError, OSError, OverflowError):
        return True


def fetch_greenhouse(token, cutoff, desc_chars):
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    rows = []
    for j in data.get("jobs", []):
        if not _fresh(j.get("updated_at"), cutoff):
            continue
        rows.append({
            "title": str(j.get("title") or "").strip(),
            "url": str(j.get("absolute_url") or "").strip(),
            "description": _text(j.get("content"), desc_chars),
            "company": token,
        })
    return rows


def fetch_lever(token, cutoff, desc_chars):
    data = _get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    rows = []
    for j in data if isinstance(data, list) else []:
        if not _fresh(j.get("createdAt"), cutoff):
            continue
        rows.append({
            "title": str(j.get("text") or "").strip(),
            # applyUrl lands directly on the form; hostedUrl needs a click first.
            "url": str(j.get("applyUrl") or j.get("hostedUrl") or "").strip(),
            "description": _text(j.get("descriptionPlain") or j.get("description"), desc_chars),
            "company": token,
        })
    return rows


def fetch_ashby(token, cutoff, desc_chars):
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false")
    rows = []
    for j in data.get("jobs", []):
        if not _fresh(j.get("publishedAt"), cutoff):
            continue
        rows.append({
            "title": str(j.get("title") or "").strip(),
            "url": str(j.get("applyUrl") or j.get("jobUrl") or "").strip(),
            "description": _text(j.get("descriptionPlain") or j.get("descriptionHtml"), desc_chars),
            "company": token,
        })
    return rows


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def parse_sources(raw):
    """'greenhouse:acme, lever:widgets' -> [('greenhouse','acme'), ('lever','widgets')]"""
    out = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        kind, _, token = chunk.partition(":")
        kind, token = kind.strip().lower(), token.strip()
        if kind in FETCHERS and token:
            out.append((kind, token))
        else:
            log(f"ats: ignoring unusable source {chunk!r}")
    return out


def load_sources(inline=None, path=None):
    """Sources come from ATS_SOURCES, or a JSON file of [{"type","id"}]."""
    if inline:
        return parse_sources(inline)
    path = path or os.getenv("ATS_SOURCES_FILE", "ats_sources.json")
    if not os.path.exists(path):
        log(f"ats: no source list at {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for entry in data if isinstance(data, list) else data.get("sources", []):
        kind = str(entry.get("type", "")).lower()
        token = str(entry.get("id", ""))
        if kind in FETCHERS and token:
            out.append((kind, token))
        else:
            log(f"ats: ignoring unusable source {entry!r}")
    return out


def fetch_all(sources, hours_old=None, desc_chars=3000):
    """Fetch every source. Returns (rows, failures).

    One dead board must not sink the run -- companies delete boards and rename
    tokens -- but a total wipeout is a real failure and the caller says so.
    """
    cutoff = None
    if hours_old:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_old)

    rows, failures = [], []
    for kind, token in sources:
        try:
            got = FETCHERS[kind](token, cutoff, desc_chars)
            log(f"ats: {kind}:{token} -> {len(got)} posting(s)")
            rows.extend(got)
        except Exception as e:
            log(f"ats: {kind}:{token} FAILED {type(e).__name__}: {e}")
            failures.append(f"{kind}:{token}")
    return rows, failures


def main():
    """--check validates a source list so you can curate it quickly."""
    import argparse

    parser = argparse.ArgumentParser(description="Validate ATS job board sources")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--sources", default=os.getenv("ATS_SOURCES", ""))
    parser.add_argument("--sources-file", default=os.getenv("ATS_SOURCES_FILE", "ats_sources.json"))
    parser.add_argument("--hours-old", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="Emit the working sources as JSON")
    args = parser.parse_args()

    sources = load_sources(args.sources or None, args.sources_file)
    if not sources:
        log("ats: no sources to check")
        return 1

    working = []
    for kind, token in sources:
        try:
            got = FETCHERS[kind](token, None, 200)
            titles = ", ".join(j["title"] for j in got[:2])
            print(f"  OK   {kind}:{token:<24} {len(got):>4} jobs   {titles[:70]}")
            working.append({"type": kind, "id": token})
        except Exception as e:
            print(f"  DEAD {kind}:{token:<24} {type(e).__name__}: {str(e)[:60]}")

    print(f"\n  {len(working)}/{len(sources)} sources resolve")
    if args.json:
        print(json.dumps(working, indent=2))
    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(main())
