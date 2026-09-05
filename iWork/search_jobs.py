"""Search job boards for fresh, relevant postings and pick a resume for each.

Contract with the n8n bridge:
  stdout  -> {"count": N, "jobs": [{"title", "url", "description", "resume"}, ...]}
  stderr  -> one-line diagnostics (how many were scraped, dropped, kept)
  exit 0  -> the search ran; N may legitimately be 0
  exit 1  -> the search failed; stderr holds the reason

An empty result is a normal outcome. A failure is never disguised as a
result -- returning a fake "job" on error is what made the previous
version fail silently for months.

Resume selection used to be a Gemini agent node in n8n. The prompt it ran was
literal keyword matching over two possible filenames, so it lived on the
critical path as an external API that could retire a model underneath us --
which it did. It is a local function now: deterministic, free, and it cannot
go offline.
"""

import argparse
import json
import os
import sys

# Titles must contain at least one of these to be worth applying to. Job boards
# match keywords against the whole posting, which is how a "Legal Assistant"
# ended up in the results for "Electronics Automation".
DEFAULT_TITLE_ANY = (
    "electronic,electrical,automation,controls,control system,plc,scada,"
    "embedded,firmware,hardware,pcb,instrumentation,mechatronic,robotic,"
    "test engineer,systems engineer,maintenance technician"
)

DEFAULT_TITLE_NONE = (
    "legal,attorney,paralegal,nurse,teacher,driver,sales,recruiter,"
    "accountant,marketing,barista,cashier,security guard,insurance"
)

# Resume selection. Whichever list matches more distinct terms wins; ties and
# no-match both fall to the electronics resume, matching the original default.
DEFAULT_CONTROL_ANY = (
    "plc,scada,control system,controls,dcs,hmi,ladder logic,motion control,"
    "servo,vfd,allen bradley,rockwell,siemens,beckhoff,codesys,tia portal"
)

DEFAULT_ELECTRONICS_ANY = (
    "hardware,circuit,pcb,embedded,firmware,schematic,analog,rf,fpga,"
    "altium,kicad,signal integrity,soldering,microcontroller"
)

RESUME_CONTROL = "Resume_Control.pdf"
RESUME_ELECTRONICS = "Resume_Electronics.pdf"


def log(message):
    """Diagnostics go to stderr so they never corrupt the JSON contract."""
    print(message, file=sys.stderr, flush=True)


def terms(raw):
    return [t.strip().lower() for t in (raw or "").split(",") if t.strip()]


def load_applied(log_dir):
    path = os.path.join(log_dir, "applied_jobs.txt")
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def title_matches(title, include_any, exclude_any):
    """True if the title looks like a job we actually want."""
    t = (title or "").lower()
    if not t:
        return False
    if any(term in t for term in exclude_any):
        return False
    if not include_any:
        return True
    return any(term in t for term in include_any)


def classify_resume(title, description, control_any, electronics_any):
    """Pick which resume to send. Replaces the Gemini classification node."""
    text = f"{title or ''}\n{description or ''}".lower()
    control_hits = sum(1 for term in control_any if term in text)
    electronics_hits = sum(1 for term in electronics_any if term in text)
    return RESUME_CONTROL if control_hits > electronics_hits else RESUME_ELECTRONICS


def interleave(jobs):
    """Round-robin by company.

    ATS boards differ enormously in size -- one source returned 1101 postings
    while another returned 1 -- so a straight cap hands the entire daily batch
    to whichever large board happens to sort first.
    """
    buckets = {}
    order = []
    for job in jobs:
        key = job.get("company") or ""
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(job)

    out = []
    while len(out) < len(jobs):
        for key in order:
            if buckets[key]:
                out.append(buckets[key].pop(0))
    return out


def select(rows, applied, include_any, exclude_any, limit):
    """Dedupe, drop already-applied and off-target jobs, then cap the batch."""
    seen = set()
    picked = []
    dupes = already = off_target = 0

    for job in rows:
        url = job.get("url", "")
        if not url or url == "nan":
            continue
        if url in seen:
            dupes += 1
            continue
        seen.add(url)
        if url in applied:
            already += 1
            continue
        if not title_matches(job.get("title", ""), include_any, exclude_any):
            off_target += 1
            continue
        picked.append(job)

    log(
        f"search: scraped={len(rows)} dupes={dupes} already_applied={already} "
        f"off_target={off_target} eligible={len(picked)}"
    )

    if limit and len(picked) > limit:
        picked = interleave(picked)
        companies = sorted({j.get("company") or "?" for j in picked[:limit]})
        log(f"search: capping {len(picked)} eligible jobs at limit={limit} across {companies}")
        picked = picked[:limit]

    return picked


def scrape(keywords, location, results_wanted, hours_old, country, desc_chars):
    # Imported lazily so --help and selftest.py work without the scraper.
    from jobspy import scrape_jobs

    rows = []
    for kw in keywords:
        # NOTE: the parameter is country_indeed. An earlier version passed
        # country_alfa2, which is not part of the jobspy API.
        df = scrape_jobs(
            site_name=["indeed", "zip_recruiter"],
            search_term=kw,
            location=location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            country_indeed=country,
        )
        if df is None or df.empty:
            log(f"search: keyword={kw!r} location={location!r} -> 0 rows")
            continue
        log(f"search: keyword={kw!r} location={location!r} -> {len(df)} rows")
        for _, row in df.iterrows():
            rows.append(
                {
                    "title": str(row.get("title") or "").strip(),
                    "url": str(row.get("job_url") or "").strip(),
                    "description": str(row.get("description") or "")[:desc_chars],
                    "company": str(row.get("company") or "").strip(),
                }
            )
    return rows


def gather(args):
    """Collect postings from the enabled sources.

    Returns (rows, fatal). Aggregators and ATS boards fail in different ways --
    a scraper blowing up is fatal, one dead company board is not -- so this
    only reports fatal when every enabled source produced nothing by failing.
    """
    rows = []
    boards_failed = ats_failed = False

    if args.sources in ("boards", "both"):
        try:
            rows.extend(scrape(
                [k.strip() for k in args.keyword.split(",") if k.strip()],
                args.location,
                args.results_wanted,
                args.hours_old,
                args.country,
                args.desc_chars,
            ))
        except Exception as e:
            log(f"search: aggregator FAILED {type(e).__name__}: {e}")
            boards_failed = True

    if args.sources in ("ats", "both"):
        import ats_jobs

        sources = ats_jobs.load_sources(args.ats_sources or None, args.ats_sources_file)
        if not sources:
            log("search: no ATS sources configured")
            ats_failed = True
        else:
            ats_rows, failures = ats_jobs.fetch_all(sources, args.hours_old, args.desc_chars)
            rows.extend(ats_rows)
            if len(failures) == len(sources):
                log(f"search: every ATS source failed ({len(failures)})")
                ats_failed = True

    if args.sources == "boards":
        return rows, boards_failed
    if args.sources == "ats":
        return rows, ats_failed
    return rows, (boards_failed and ats_failed)


def main():
    parser = argparse.ArgumentParser(description="Search job boards for fresh postings")
    parser.add_argument("--keyword", required=True, help="Comma-separated keyword(s)")
    parser.add_argument("--location", required=True, help="Location, or 'Remote'")
    parser.add_argument("--logdir", default=os.getenv("LOG_DIR", "./logs"))
    parser.add_argument("--results-wanted", type=int, default=int(os.getenv("RESULTS_WANTED", "15")))
    parser.add_argument("--hours-old", type=int, default=int(os.getenv("HOURS_OLD", "168")))
    parser.add_argument("--country", default=os.getenv("COUNTRY_INDEED", "USA"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("MAX_PER_RUN", "5")),
                        help="Max jobs returned per run; 0 for no cap")
    parser.add_argument("--match-title-any", default=os.getenv("MATCH_TITLE_ANY", DEFAULT_TITLE_ANY))
    parser.add_argument("--exclude-title-any", default=os.getenv("EXCLUDE_TITLE_ANY", DEFAULT_TITLE_NONE))
    parser.add_argument("--control-terms", default=os.getenv("MATCH_CONTROL_ANY", DEFAULT_CONTROL_ANY))
    parser.add_argument("--electronics-terms", default=os.getenv("MATCH_ELECTRONICS_ANY", DEFAULT_ELECTRONICS_ANY))
    parser.add_argument("--desc-chars", type=int, default=int(os.getenv("DESC_CHARS", "3000")))
    parser.add_argument("--sources", default=os.getenv("JOB_SOURCES", "both"),
                        choices=["boards", "ats", "both"],
                        help="boards = jobspy aggregators, ats = Greenhouse/Lever/Ashby")
    parser.add_argument("--ats-sources", default=os.getenv("ATS_SOURCES", ""),
                        help="Inline list, e.g. 'greenhouse:acme,lever:widgets'")
    parser.add_argument("--ats-sources-file", default=os.getenv("ATS_SOURCES_FILE", "ats_sources.json"))
    args = parser.parse_args()

    if not [k.strip() for k in args.keyword.split(",") if k.strip()]:
        log("search: --keyword produced no usable terms")
        return 1

    rows, fatal = gather(args)
    if fatal:
        # Loud and non-zero. The bridge turns this into an HTTP 500 so the
        # n8n node goes red instead of quietly processing a fake job.
        log("search: FAILED every enabled source errored")
        return 1

    jobs = select(
        rows,
        load_applied(args.logdir),
        terms(args.match_title_any),
        terms(args.exclude_title_any),
        args.limit,
    )

    control_any = terms(args.control_terms)
    electronics_any = terms(args.electronics_terms)
    for job in jobs:
        job["resume"] = classify_resume(
            job["title"], job["description"], control_any, electronics_any
        )

    counts = {}
    for job in jobs:
        counts[job["resume"]] = counts.get(job["resume"], 0) + 1
    if counts:
        log("search: resume split " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    print(json.dumps({"count": len(jobs), "jobs": jobs}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
