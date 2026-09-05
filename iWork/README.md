# iWork - Autonomous Job Application System

An automation system that scrapes job listings, filters them for relevance,
picks the matching resume, and completes the application with Playwright, all
orchestrated by n8n.

## Architecture

```
n8n (Docker)              host (systemd)
Schedule 8 AM
  -> POST /run  --------->  FastAPI bridge
                              search_jobs.py  -> jobspy      (filter + pick resume)
                              apply_job.py    -> Playwright  (per eligible job)
                            <- {searched, submitted, skipped, errors, by_reason}
Schedule 6 PM
  -> GET /stats ---------->  FastAPI bridge
  -> Resend email
```

**n8n schedules; it does not orchestrate.** An earlier version fanned the job
list out with a Split Out node and made one HTTP request per job, driven by
`$json` expressions. Every one of those was somewhere the plumbing could be
wrong -- Split Out field names, paired-item lookups, expression syntax the UI
rewrites on import -- and several of them were. `/run` does the whole day's
work in one call, so the workflow contains no expressions at all and the logic
lives in Python where it is unit-tested and its failures are loud.

It also means the entire pipeline is testable without n8n:

```bash
curl -s -X POST http://127.0.0.1:8080/run -H "X-IWork-Token: $BRIDGE_TOKEN" -H 'Content-Type: application/json' -d '{"dry_run":true}'
```

`/search` and `/apply` remain as separate endpoints for debugging a single
stage; `/run` is what the schedule calls.

Two external dependencies were deliberately removed from this critical path,
because each one silently took the whole pipeline down.

**Google Drive.** Resumes are read from `RESUME_DIR` on the host rather than
downloaded per run. The PDFs never change, and an OAuth refresh token on a
consent screen in *Testing* status expires after 7 days. Removing it also
removed a `/home/node/.n8n` vs host-path mismatch that meant the resume path
never resolved, so applications went out with nothing attached.

**Gemini.** Resume selection was an LLM agent node whose entire prompt was
"if the description mentions PLC/SCADA use one file, if it mentions PCB/embedded
use the other, otherwise default". That is keyword matching, and running it as
an external API put a retirable model on the critical path -- Gemini 2.0 Flash
was shut down on 2026-06-01 and nodes with a blank model field inherit whatever
default their version carries. It is now `classify_resume()` in
`search_jobs.py`: deterministic, free, unit-tested, and it cannot go offline.
Whichever term list matches more distinct terms wins; ties fall to the default.

## Design rule: failures are loud

Every stage now reports failure instead of returning something that looks like
success.

| Stage | Failure | Result |
|---|---|---|
| `search_jobs.py` | scrape raises | stderr + **exit 1** |
| `server.py` | script exit 1 | **HTTP 500**, n8n node goes red with stderr |
| `server.py` | script exceeds timeout | **HTTP 504** |
| `apply_job.py` | bad URL / missing resume | **exit 1** -> HTTP 500 |
| `apply_job.py` | no form to complete | exit 2 -> HTTP 200 `{"status":"skipped","reason":...}` |

A zero-job search is a normal result (`{"count": 0, "jobs": []}`), so
downstream nodes simply do not run. Nothing is ever fabricated to fill the gap.

`applied_jobs.txt` is written **only** after the resume is attached and a
submit control is actually clicked. Skips go to `skipped_jobs.jsonl` with a
reason and are reported in the daily email.

## Prerequisites

- Python 3.10+
- n8n (self-hosted; the bridge is reachable at the host's internal IP)

No LLM API key or Google credential is needed. The only outbound calls are to
the job boards themselves.

## Setup

### 1. Python environment

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt && ./venv/bin/playwright install chromium
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in the paths, the applicant details, and a bridge token:

```bash
openssl rand -hex 32
```

Put the resume PDFs in `RESUME_DIR`:

```bash
mkdir -p resumes && cp Resume_Control.pdf Resume_Electronics.pdf resumes/
```

### 3. Run the bridge as a service

Do not start `uvicorn` by hand -- it dies with your SSH session and does not
come back after a reboot.

```bash
sudo cp iwork-bridge.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now iwork-bridge
```

Verify:

```bash
curl -s -H "X-IWork-Token: $BRIDGE_TOKEN" http://127.0.0.1:8080/health
```

`/health` checks the interpreter, both scripts, the log directory, that
`jobspy` and `playwright` import, and that the resumes are present. It returns
503 if anything is missing, so you can monitor it.

### 4. n8n workflows

Import `workflow_iwork.json`, then replace `[YOUR_BRIDGE_URL]`,
`[YOUR_BRIDGE_TOKEN]`, `[YOUR_RESEND_API_KEY]` and `[YOUR_EMAIL]`. No n8n
credentials need configuring.

It is one workflow, five nodes, two independent triggers:

| Trigger | Cron | Chain |
|---|---|---|
| Daily 8 AM - Apply | `0 8 * * *` | `POST /run` |
| Daily 6 PM - Report | `0 18 * * *` | `GET /stats` -> Resend email |

The `/run` node carries a 15-minute timeout, since one call covers every
location and every eligible job. Per-job cost is bounded by `APPLY_TIMEOUT` and
the total by `MAX_PER_RUN` times the number of locations.

Import as a **new** workflow rather than importing into an existing one. n8n
stores schedule-trigger recurrence state in each workflow's `staticData`, and
importing into an existing workflow keeps that row -- along with any stuck
state. A fresh row starts clean.

Both triggers use a fixed daily cron rather than an "every N hours" interval.
Interval triggers keep their position in `staticData` and can get stuck such
that the workflow reports **Active** while never executing again.

The workflow timezone is pinned in `settings`. Without it a workflow inherits
`GENERIC_TIMEZONE`, so the same cron fires at a different wall-clock time than
your other workflows.

## Verify before trusting it

```bash
python selftest.py
```

33 offline checks: the relevance filter, the already-applied memory bank,
resume resolution and path-traversal rejection, URL validation, and resume
classification. No network or browser needed.

Then a real end-to-end dry run, which completes a form but never clicks submit:

```bash
curl -s -X POST http://127.0.0.1:8080/apply -H "X-IWork-Token: $BRIDGE_TOKEN" -H 'Content-Type: application/json' -d '{"url":"https://example.com/job","jobtitle":"Test","resume_name":"Resume_Electronics.pdf","dry_run":true}'
```

## Tuning

`MAX_PER_RUN` caps applications per run. Without it, one run consumes every
posting in the `HOURS_OLD` window, records them all, and every later run finds
nothing left -- the system starves itself and looks broken.

`MATCH_TITLE_ANY` / `EXCLUDE_TITLE_ANY` filter on the job **title**. Indeed
matches your keywords against the entire posting, so without a title filter a
search for "Electronics Automation" returns things like "Legal Assistant".

`MATCH_CONTROL_ANY` / `MATCH_ELECTRONICS_ANY` decide which resume gets sent.
Whichever list matches more distinct terms in the title plus description wins;
ties and no-match fall to `Resume_Electronics.pdf`.

## Job sources

`JOB_SOURCES` selects where postings come from.

**`ats`** (default) reads company job boards directly: Greenhouse, Lever and
Ashby all publish read-only JSON with no auth. Sources are listed in
`ats_sources.json` as `{"type", "id"}` pairs. Validate a list before trusting
it, since companies rename and delete boards:

```bash
python ats_jobs.py --check
```

**`boards`** uses the jobspy aggregators (Indeed, ZipRecruiter).

The default is `ats` because the two were measured against each other:

| | aggregators | ATS boards |
|---|---|---|
| Postings fetched | 10 | 1792 |
| Time | 75s | 5s |
| Relevant after filtering | 3 | 283 |
| Reached a completable form | **0** | **3 of 5 attempted** |

ZipRecruiter answers every request with `403 forbidden cf-waf`, and Indeed's
`viewjob` pages expose no application form to headless Playwright, so that path
can only ever report `skipped`. ATS application pages carry a real
`input[type=file]`.

ATS boards vary enormously in size -- one source returned 1101 postings and
another returned 1 -- so `search_jobs.py` round-robins by company before
applying `MAX_PER_RUN`. Otherwise the largest board takes the entire batch.

## Known limitation

Aggregators (Indeed's SmartApply, LinkedIn Easy Apply) put their application
flow behind bot detection that headless Playwright cannot clear.
For those, `apply_job.py` correctly reports
`skipped: no file input found; not a completable application form` rather than
claiming a submission. Direct employer/ATS application pages (Greenhouse,
Lever, Workable) are where this pipeline actually completes applications.

## License

MIT
