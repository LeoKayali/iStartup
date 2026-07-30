"""Offline checks for the pure decision logic. No network, no browser, no jobspy.

    python selftest.py

These cover the three bugs that made the pipeline lie about what it was doing:
relevance filtering, the already-applied memory bank, and resume resolution.
"""

import os
import sys
import tempfile

import apply_job
import search_jobs

FAILURES = []


def check(name, actual, expected):
    if actual == expected:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: expected {expected!r}, got {actual!r}")
        FAILURES.append(name)


def raises(name, fn):
    try:
        fn()
    except ValueError:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: expected ValueError, none raised")
        FAILURES.append(name)


print("title relevance filter")
include = search_jobs.terms(search_jobs.DEFAULT_TITLE_ANY)
exclude = search_jobs.terms(search_jobs.DEFAULT_TITLE_NONE)

# These five titles are the real output of an "Electronics Automation" search.
check("keeps maintenance technician", search_jobs.title_matches("Tecnico en Mantenimiento con VISA TN / Equipment Maintenance Technician", include, exclude), True)
check("drops legal assistant", search_jobs.title_matches("Legal Assistant", include, exclude), False)
check("drops monitoring engineer", search_jobs.title_matches("Sr. Monitoring Engineer (VA ESOM)", include, exclude), False)
check("drops APM manager", search_jobs.title_matches("Remote Application Performance Monitoring Manager (VA ESOM)", include, exclude), False)
check("keeps controls engineer", search_jobs.title_matches("Controls Engineer - PLC/SCADA", include, exclude), True)
check("keeps embedded role", search_jobs.title_matches("Embedded Firmware Engineer", include, exclude), True)
check("drops empty title", search_jobs.title_matches("", include, exclude), False)

print("selection: dedupe, memory bank, cap")
rows = [
    {"title": "Controls Engineer", "url": "https://x.com/1", "description": "d"},
    {"title": "Controls Engineer", "url": "https://x.com/1", "description": "d"},   # dupe
    {"title": "Legal Assistant", "url": "https://x.com/2", "description": "d"},     # off target
    {"title": "PLC Technician", "url": "https://x.com/3", "description": "d"},      # already applied
    {"title": "PCB Design Engineer", "url": "https://x.com/4", "description": "d"},
    {"title": "Automation Engineer", "url": "https://x.com/5", "description": "d"},
    {"title": "Robotics Technician", "url": "", "description": "d"},                # no url
    {"title": "Hardware Engineer", "url": "nan", "description": "d"},               # jobspy NaN
]
picked = search_jobs.select(rows, {"https://x.com/3"}, include, exclude, limit=0)
check("keeps only eligible jobs", [j["url"] for j in picked], ["https://x.com/1", "https://x.com/4", "https://x.com/5"])

capped = search_jobs.select(rows, set(), include, exclude, limit=2)
check("respects the per-run cap", len(capped), 2)

check("empty input yields empty output", search_jobs.select([], set(), include, exclude, 0), [])

print("resume resolution")
with tempfile.TemporaryDirectory() as tmp:
    real = os.path.join(tmp, "Resume_Control.pdf")
    with open(real, "w", encoding="utf-8") as f:
        f.write("pdf")

    check("resolves a bare name", apply_job.resolve_resume(None, "Resume_Control.pdf", tmp), real)
    check("accepts an explicit path", apply_job.resolve_resume(real, None, tmp), real)
    raises("rejects a missing file", lambda: apply_job.resolve_resume(None, "Resume_Nope.pdf", tmp))
    raises("rejects path traversal", lambda: apply_job.resolve_resume(None, "../../etc/passwd", tmp))
    raises("rejects a nested path", lambda: apply_job.resolve_resume(None, "sub/Resume_Control.pdf", tmp))
    raises("rejects no resume at all", lambda: apply_job.resolve_resume(None, None, tmp))
    raises("rejects a bad explicit path", lambda: apply_job.resolve_resume(os.path.join(tmp, "ghost.pdf"), None, tmp))

print("url validation")
check("accepts https", apply_job.valid_url("https://www.indeed.com/viewjob?jk=abc"), True)
check("accepts http", apply_job.valid_url("http://example.com/job"), True)
# The old pipeline fed the literal string "N/A" straight into page.goto().
check("rejects N/A", apply_job.valid_url("N/A"), False)
check("rejects empty", apply_job.valid_url(""), False)
check("rejects a bare host", apply_job.valid_url("example.com/job"), False)
check("rejects a file url", apply_job.valid_url("file:///etc/passwd"), False)

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
