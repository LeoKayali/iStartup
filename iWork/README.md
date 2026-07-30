# iWork - Autonomous Job Application System

An agentic automation system that scrapes job listings, classifies them with
Gemini to pick a resume, and completes the application with Playwright, all
orchestrated by n8n.

## Architecture

```
n8n (Docker)                      host (systemd)
Schedule 8 AM
  -> POST /search   ------------>  FastAPI bridge  -> search_jobs.py  -> jobspy
  -> Split Out "jobs"
  -> Gemini agent (picks a resume filename)
  -> Merge by position (rejoins job url/title with the resume choice)
  -> POST /apply    ------------>  FastAPI bridge  -> apply_job.py    -> Playwright
```

Resumes are read from `RESUME_DIR` on the host. They are deliberately **not**
pulled from Google Drive per run: the PDFs never change, and an OAuth refresh
token on a consent screen in *Testing* status expires after 7 days, which
silently breaks the whole pipeline about a week after setup.

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
- A Google Gemini API key

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

Import `workflow_job_automation.json` and `workflow_daily_summary.json`, then
replace `[YOUR_BRIDGE_URL]`, `[YOUR_BRIDGE_TOKEN]`, `[YOUR_CREDENTIALS_ID]`,
`[YOUR_RESEND_API_KEY]` and `[YOUR_EMAIL]`.

Confirm the Gemini node's **Model** field is a model that currently exists.
It is pinned to `models/gemini-2.5-flash` rather than left blank on purpose:
a blank field inherits the node's default, and when Google retires that
default the workflow dies with a 404 and no code change on your side.

## Verify before trusting it

```bash
python selftest.py
```

Offline checks for the relevance filter, the already-applied memory bank,
resume resolution, and URL validation. No network or browser needed.

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

## Known limitation

Most large boards (Indeed's SmartApply, LinkedIn Easy Apply) put their
application flow behind bot detection that headless Playwright cannot clear.
For those, `apply_job.py` correctly reports
`skipped: no file input found; not a completable application form` rather than
claiming a submission. Direct employer/ATS application pages (Greenhouse,
Lever, Workable) are where this pipeline actually completes applications.

## License

MIT
