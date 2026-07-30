"""FastAPI bridge that lets n8n run the local job-automation scripts.

Design rule: a failing script must produce a failing HTTP response. The
previous version returned subprocess stdout verbatim without checking the
exit code, so a crashed scraper looked like an HTTP 200 with a valid body
and every n8n node stayed green while nothing happened.

Exit-code mapping:
  0 -> 200  the work was done
  2 -> 200  legitimately nothing to do (no application form on the page)
  1 -> 500  real failure; the n8n node goes red and shows stderr
"""

import json
import logging
import os
import subprocess

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iwork")

PYTHON_PATH = os.getenv("PYTHON_PATH", "python")
SCRIPTS_DIR = os.getenv("SCRIPTS_DIR", ".")
LOG_DIR = os.getenv("LOG_DIR", "./logs")
RESUME_DIR = os.getenv("RESUME_DIR", "./resumes")
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")
SEARCH_TIMEOUT = int(os.getenv("SEARCH_TIMEOUT", "300"))
APPLY_TIMEOUT = int(os.getenv("APPLY_TIMEOUT", "240"))

app = FastAPI(title="iWork bridge")

if not BRIDGE_TOKEN:
    log.warning(
        "BRIDGE_TOKEN is unset: this service executes subprocesses and is "
        "listening without authentication. Set BRIDGE_TOKEN in .env."
    )


def require_token(x_iwork_token: str = Header(default="")):
    if BRIDGE_TOKEN and x_iwork_token != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-IWork-Token")


def script(name):
    return os.path.join(SCRIPTS_DIR, name)


def run(cmd, timeout):
    """Run a script and return (returncode, stdout, stderr)."""
    log.info("run: %s", " ".join(cmd[:3]) + " ...")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"script exceeded {timeout}s: {cmd[1]}")
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"cannot execute {cmd[0]}: {e}")

    if proc.stderr:
        for line in proc.stderr.strip().splitlines():
            log.info("script: %s", line)
    return proc.returncode, proc.stdout, proc.stderr


def parse_json(stdout, stderr):
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(
            status_code=500,
            detail={"error": "script did not emit valid JSON", "stdout": stdout[:500], "stderr": stderr[-1000:]},
        )


class SearchRequest(BaseModel):
    keyword: str
    location: str
    limit: int | None = None
    hours_old: int | None = None
    results_wanted: int | None = None


class ApplyRequest(BaseModel):
    url: str
    jobtitle: str = "Unknown Title"
    resume_name: str | None = None
    resume: str | None = None
    dry_run: bool = False


@app.post("/search", dependencies=[Depends(require_token)])
def run_search(req: SearchRequest):
    cmd = [
        PYTHON_PATH,
        script("search_jobs.py"),
        "--keyword", req.keyword,
        "--location", req.location,
        "--logdir", LOG_DIR,
    ]
    if req.limit is not None:
        cmd += ["--limit", str(req.limit)]
    if req.hours_old is not None:
        cmd += ["--hours-old", str(req.hours_old)]
    if req.results_wanted is not None:
        cmd += ["--results-wanted", str(req.results_wanted)]

    rc, stdout, stderr = run(cmd, SEARCH_TIMEOUT)
    if rc != 0:
        raise HTTPException(status_code=500, detail={"error": "search failed", "stderr": stderr[-1000:]})

    payload = parse_json(stdout, stderr)
    log.info("search: returning %s job(s)", payload.get("count"))
    return payload


@app.post("/apply", dependencies=[Depends(require_token)])
def run_apply(req: ApplyRequest):
    # Applicant identity and the account password come from the environment,
    # not from the n8n workflow -- argv is world-readable via ps.
    cmd = [
        PYTHON_PATH,
        script("apply_job.py"),
        "--url", req.url,
        "--jobtitle", req.jobtitle,
        "--email", os.getenv("APPLICANT_EMAIL", ""),
        "--firstname", os.getenv("APPLICANT_FIRSTNAME", ""),
        "--lastname", os.getenv("APPLICANT_LASTNAME", ""),
        "--phone", os.getenv("APPLICANT_PHONE", ""),
        "--linkedin", os.getenv("APPLICANT_LINKEDIN", ""),
        "--resume-dir", RESUME_DIR,
        "--logdir", LOG_DIR,
    ]
    if req.resume:
        cmd += ["--resume", req.resume]
    elif req.resume_name:
        cmd += ["--resume-name", req.resume_name]
    if req.dry_run:
        cmd += ["--dry-run"]

    rc, stdout, stderr = run(cmd, APPLY_TIMEOUT)
    if rc == 1:
        raise HTTPException(status_code=500, detail={"error": "apply failed", "stderr": stderr[-1000:]})

    # rc 0 (submitted) and rc 2 (nothing to submit) are both valid outcomes.
    payload = parse_json(stdout, stderr)
    log.info("apply: %s -> %s (%s)", req.url, payload.get("status"), payload.get("reason"))
    return payload


@app.get("/health")
def health():
    """Cheap readiness check: everything the pipeline needs, verified."""
    checks = {
        "python": os.path.isfile(PYTHON_PATH) or PYTHON_PATH == "python",
        "search_script": os.path.isfile(script("search_jobs.py")),
        "apply_script": os.path.isfile(script("apply_job.py")),
        "resume_dir": os.path.isdir(RESUME_DIR),
        "log_dir_writable": os.access(LOG_DIR, os.W_OK) if os.path.isdir(LOG_DIR) else False,
        "token_configured": bool(BRIDGE_TOKEN),
    }
    rc, _, stderr = run([PYTHON_PATH, "-c", "import jobspy, playwright"], 60)
    checks["deps_importable"] = rc == 0
    if rc != 0:
        checks["deps_error"] = stderr.strip().splitlines()[-1:] or ["unknown"]

    resumes = sorted(os.listdir(RESUME_DIR)) if checks["resume_dir"] else []
    # token_configured is advisory: running without a token is a warning, not a
    # broken pipeline, so it must not turn /health into a 503.
    informational = ("deps_error", "token_configured")
    ok = all(v for k, v in checks.items() if k not in informational) and bool(resumes)
    body = {"ok": ok, "checks": checks, "resumes": resumes}
    if not ok:
        raise HTTPException(status_code=503, detail=body)
    return body


@app.get("/stats")
def get_stats():
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    applied_file = os.path.join(LOG_DIR, "applied_jobs.txt")
    detailed_file = os.path.join(LOG_DIR, "applied_jobs_detailed.jsonl")
    skipped_file = os.path.join(LOG_DIR, "skipped_jobs.jsonl")

    total_applied = 0
    today_count = 0
    today_jobs = []
    skipped_today = 0
    skip_reasons = {}

    if os.path.exists(applied_file):
        with open(applied_file, "r", encoding="utf-8") as f:
            total_applied = sum(1 for line in f if line.strip())

    if os.path.exists(detailed_file):
        with open(detailed_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("date") == today:
                    today_count += 1
                    today_jobs.append(data.get("title", "Unknown Title"))

    if os.path.exists(skipped_file):
        with open(skipped_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("date") == today:
                    skipped_today += 1
                    reason = data.get("reason", "unknown")
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    return {
        "total_applied": total_applied,
        "today_count": today_count,
        "today_jobs": today_jobs,
        "skipped_today": skipped_today,
        "skip_reasons": skip_reasons,
    }
