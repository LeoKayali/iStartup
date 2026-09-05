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

print("round-robin across companies")
mixed = ([{"title": "Controls Engineer", "url": f"https://a.test/{i}", "description": "", "company": "big"} for i in range(6)]
         + [{"title": "PCB Engineer", "url": "https://b.test/1", "description": "", "company": "small"}]
         + [{"title": "PLC Engineer", "url": f"https://c.test/{i}", "description": "", "company": "mid"} for i in range(2)])
rr = search_jobs.interleave(mixed)
check("first pass takes one per company", [j["company"] for j in rr[:3]], ["big", "small", "mid"])
check("nothing is lost", len(rr), len(mixed))
check("every url survives", len({j["url"] for j in rr}), len(mixed))
# The real failure this prevents: one 1101-posting board eating the whole batch.
capped = search_jobs.select(mixed, set(), include, exclude, limit=3)
check("a cap spreads across companies", sorted({j["company"] for j in capped}), ["big", "mid", "small"])
check("single-company input still works", len(search_jobs.interleave(mixed[:6])), 6)

print("resume classification (replaces the Gemini agent node)")
ctl = search_jobs.terms(search_jobs.DEFAULT_CONTROL_ANY)
ele = search_jobs.terms(search_jobs.DEFAULT_ELECTRONICS_ANY)
C, E = search_jobs.RESUME_CONTROL, search_jobs.RESUME_ELECTRONICS


def classify(title, desc=""):
    return search_jobs.classify_resume(title, desc, ctl, ele)


check("PLC/SCADA -> control", classify("Controls Engineer", "Program PLC and SCADA systems, ladder logic"), C)
check("PCB/embedded -> electronics", classify("Hardware Engineer", "PCB schematic capture, embedded firmware"), E)
check("empty -> electronics default", classify("", ""), E)
check("neither list -> electronics default", classify("Project Manager", "Coordinate schedules and budgets"), E)
check("title alone counts", classify("PLC Technician"), C)
# Mixed postings: the side with more distinct matches wins, ties go to the default.
check("control-heavy mixed -> control", classify("Automation Engineer", "PLC, SCADA, HMI, VFD, plus some PCB work"), C)
check("electronics-heavy mixed -> electronics", classify("Hardware Lead", "PCB, FPGA, analog, RF, schematic, one PLC"), E)
check("exact tie -> electronics default", classify("Engineer", "PLC work and PCB work"), E)
# The real posting from a live run.
check("maintenance tech with PLC -> control",
      classify("Equipment Maintenance Technician",
               "Basic troubleshooting of PLC-controlled equipment, sensors, relays, HMIs"), C)

print("run summary tally")
# server.py imports fastapi/pydantic, which selftest must not require. Import
# the module only if those are installed; skip cleanly otherwise.
try:
    import server
except ImportError as e:
    print(f"  skip  (server.py needs {e.name}; run this in the venv to cover it)")
else:
    s = server.tally([
        {"status": "submitted", "title": "PLC Engineer"},
        {"status": "submitted", "title": "Controls Engineer"},
        {"status": "skipped", "reason": "no file input found"},
        {"status": "skipped", "reason": "no file input found"},
        {"status": "skipped", "reason": "no visible submit control found"},
        {"status": "error", "reason": "invalid url"},
    ])
    check("counts submissions", s["submitted"], 2)
    check("counts skips", s["skipped"], 3)
    check("counts errors", s["errors"], 1)
    check("groups skip reasons", s["by_reason"]["no file input found"], 2)
    check("records submitted titles", s["titles"], ["PLC Engineer", "Controls Engineer"])
    empty = server.tally([])
    check("empty run tallies to zero", (empty["submitted"], empty["skipped"], empty["errors"]), (0, 0, 0))

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
