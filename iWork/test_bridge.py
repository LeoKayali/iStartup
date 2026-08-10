"""Integration tests for the FastAPI bridge, using stub scripts.

    pip install fastapi httpx
    python test_bridge.py

No network, no browser, no jobspy, no Playwright. The stubs stand in for
search_jobs.py and apply_job.py so the tests can drive exit codes directly --
which is the part that matters, because the whole point of this rewrite is that
a failing script must produce a failing HTTP response.

selftest.py covers the pure logic; this covers the wiring between the two.
"""

import importlib
import json
import os
import shutil
import sys
import tempfile

FAILURES = []
WORKDIRS = []


def check(name, actual, expected):
    if actual == expected:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: expected {expected!r}, got {actual!r}")
        FAILURES.append(name)


def make_scripts(search_body, apply_body):
    """Create a scripts dir with stub search/apply scripts and a log dir."""
    d = tempfile.mkdtemp(prefix="iwork_test_")
    WORKDIRS.append(d)
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    os.makedirs(os.path.join(d, "resumes"), exist_ok=True)
    with open(os.path.join(d, "resumes", "Resume_Electronics.pdf"), "w") as f:
        f.write("pdf")
    for name, body in (("search_jobs.py", search_body), ("apply_job.py", apply_body)):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write("import sys, json\n" + body)
    return d


def load_server(scripts_dir, **extra):
    """Import server.py fresh so it re-reads its module-level config."""
    os.environ.update({
        "PYTHON_PATH": sys.executable,
        "SCRIPTS_DIR": scripts_dir,
        "LOG_DIR": os.path.join(scripts_dir, "logs"),
        "RESUME_DIR": os.path.join(scripts_dir, "resumes"),
        "BRIDGE_TOKEN": "",
        "SEARCH_LOCATIONS": "San Francisco,Remote",
        "SEARCH_KEYWORDS": "test",
    })
    os.environ.update({k: str(v) for k, v in extra.items()})
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if "server" in sys.modules:
        del sys.modules["server"]
    return importlib.import_module("server")


def client_for(mod):
    from fastapi.testclient import TestClient
    return TestClient(mod.app, raise_server_exceptions=False)


# Stub bodies. Each prints the JSON contract and exits with a chosen code.
SEARCH_OK = """
jobs = [
    {"title": "PLC Engineer", "url": "https://x.test/1", "description": "d", "resume": "Resume_Control.pdf"},
    {"title": "Hardware Engineer", "url": "https://x.test/2", "description": "d", "resume": "Resume_Electronics.pdf"},
]
print(json.dumps({"count": len(jobs), "jobs": jobs}))
sys.exit(0)
"""
SEARCH_EMPTY = 'print(json.dumps({"count": 0, "jobs": []}))\nsys.exit(0)\n'
SEARCH_FAILS = 'sys.stderr.write("search: FAILED RuntimeError: boom\\n")\nsys.exit(1)\n'
SEARCH_GARBAGE = 'print("<html>not json</html>")\nsys.exit(0)\n'

APPLY_SUBMITTED = 'print(json.dumps({"status": "submitted", "reason": "ok"}))\nsys.exit(0)\n'
APPLY_SKIPPED = 'print(json.dumps({"status": "skipped", "reason": "no file input found"}))\nsys.exit(2)\n'
APPLY_FAILS = 'sys.stderr.write("apply: FAILED bad url\\n")\nsys.exit(1)\n'
# Fails on the first job, succeeds on the rest -- proves one bad job does not
# abort the batch.
APPLY_FLAKY = """
import os
marker = os.path.join(os.path.dirname(__file__), "logs", "seen.txt")
first = not os.path.exists(marker)
open(marker, "a").close()
if first:
    sys.stderr.write("apply: FAILED bad url\\n")
    sys.exit(1)
print(json.dumps({"status": "submitted", "reason": "ok"}))
sys.exit(0)
"""

try:
    import fastapi  # noqa: F401
    import httpx    # noqa: F401
except ImportError as e:
    print(f"test_bridge.py needs {e.name}: pip install fastapi httpx")
    sys.exit(0)

print("exit-code mapping (the core of the rewrite)")
mod = load_server(make_scripts(SEARCH_OK, APPLY_SUBMITTED))
c = client_for(mod)
check("search exit 0 -> 200", c.post("/search", json={"keyword": "k", "location": "l"}).status_code, 200)
check("search returns the envelope", c.post("/search", json={"keyword": "k", "location": "l"}).json()["count"], 2)

mod = load_server(make_scripts(SEARCH_FAILS, APPLY_SUBMITTED))
c = client_for(mod)
r = c.post("/search", json={"keyword": "k", "location": "l"})
check("search exit 1 -> 500", r.status_code, 500)
check("500 body carries stderr", "boom" in json.dumps(r.json()), True)

mod = load_server(make_scripts(SEARCH_GARBAGE, APPLY_SUBMITTED))
c = client_for(mod)
check("non-JSON stdout -> 500", c.post("/search", json={"keyword": "k", "location": "l"}).status_code, 500)

mod = load_server(make_scripts(SEARCH_OK, APPLY_SKIPPED))
c = client_for(mod)
r = c.post("/apply", json={"url": "https://x.test/1", "resume_name": "Resume_Electronics.pdf"})
check("apply exit 2 -> 200 (skipped is not a fault)", r.status_code, 200)
check("skip reason is reported", r.json()["status"], "skipped")

mod = load_server(make_scripts(SEARCH_OK, APPLY_FAILS))
c = client_for(mod)
check("apply exit 1 -> 500", c.post("/apply", json={"url": "https://x.test/1"}).status_code, 500)

print("/run orchestration")
mod = load_server(make_scripts(SEARCH_OK, APPLY_SUBMITTED))
c = client_for(mod)
r = c.post("/run", json={}).json()
check("searches every configured location", r["searched"], 4)          # 2 jobs x 2 locations
check("submits every eligible job", r["submitted"], 4)
check("no skips on the happy path", r["skipped"], 0)
check("no errors on the happy path", r["errors"], 0)
check("reports the locations used", r["locations"], ["San Francisco", "Remote"])
check("collects submitted titles", len(r["titles"]), 4)

mod = load_server(make_scripts(SEARCH_OK, APPLY_SKIPPED))
c = client_for(mod)
r = c.post("/run", json={}).json()
check("skips are counted, not submitted", (r["submitted"], r["skipped"]), (0, 4))
check("skip reasons are grouped", r["by_reason"]["no file input found"], 4)

mod = load_server(make_scripts(SEARCH_EMPTY, APPLY_SUBMITTED))
c = client_for(mod)
r = c.post("/run", json={}).json()
check("zero results is a clean run, not an error", (r["searched"], r["submitted"], r["errors"]), (0, 0, 0))

mod = load_server(make_scripts(SEARCH_FAILS, APPLY_SUBMITTED))
c = client_for(mod)
check("a failed search aborts /run loudly", c.post("/run", json={}).status_code, 500)

mod = load_server(make_scripts(SEARCH_OK, APPLY_FLAKY))
c = client_for(mod)
r = c.post("/run", json={}).json()
check("one failing job does not abort the batch", (r["submitted"], r["errors"]), (3, 1))

mod = load_server(make_scripts(SEARCH_OK, APPLY_SUBMITTED))
c = client_for(mod)
r = c.post("/run", json={"locations": ["Remote"]}).json()
check("request body can override locations", (r["locations"], r["searched"]), (["Remote"], 2))
check("dry_run is echoed back", c.post("/run", json={"dry_run": True}).json()["dry_run"], True)

print("token enforcement")
mod = load_server(make_scripts(SEARCH_OK, APPLY_SUBMITTED), BRIDGE_TOKEN="s3cret")
c = client_for(mod)
check("no token -> 401", c.post("/run", json={}).status_code, 401)
check("wrong token -> 401", c.post("/run", json={}, headers={"X-IWork-Token": "nope"}).status_code, 401)
check("right token -> 200", c.post("/run", json={}, headers={"X-IWork-Token": "s3cret"}).status_code, 200)
check("/search is protected too", c.post("/search", json={"keyword": "k", "location": "l"}).status_code, 401)
# /stats stays open so an existing summary workflow without the header keeps working
check("/stats requires no token", c.get("/stats").status_code, 200)

print("stats")
mod = load_server(make_scripts(SEARCH_OK, APPLY_SUBMITTED))
c = client_for(mod)
s = c.get("/stats").json()
check("stats on an empty log dir", (s["total_applied"], s["today_count"], s["skipped_today"]), (0, 0, 0))
for d in WORKDIRS:
    shutil.rmtree(d, ignore_errors=True)

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
