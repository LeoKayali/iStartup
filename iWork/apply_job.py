"""Attempt to complete and submit one job application with Playwright.

Contract with the n8n bridge:
  stdout  -> {"status": "submitted"|"skipped", "reason": str, "url": str, ...}
  exit 0  -> an application was actually submitted
  exit 2  -> nothing was submitted (no form, no file input, no submit button)
  exit 1  -> hard failure (bad URL, missing resume, browser crash)

Two rules drive this file:

1. Never claim an application that did not happen. Only a real submit click
   with the resume attached is written to applied_jobs.txt. The previous
   version logged every URL unconditionally, which permanently blacklisted
   jobs it had never actually applied to.

2. Never submit an application without the resume. If the file input rejects
   the upload, we skip instead of sending an incomplete application.
"""

import argparse
import asyncio
import datetime
import hashlib
import json
import os
import sys
from urllib.parse import urlparse

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def log(message):
    print(message, file=sys.stderr, flush=True)


def resolve_resume(resume, resume_name, resume_dir):
    """Return an existing resume path, or raise ValueError.

    resume_name is resolved inside resume_dir and must not escape it -- the
    name arrives from an LLM, so it is untrusted input.
    """
    if resume:
        if not os.path.isfile(resume):
            raise ValueError(f"resume not found: {resume}")
        return resume

    if not resume_name:
        raise ValueError("one of --resume or --resume-name is required")

    if os.path.basename(resume_name) != resume_name:
        raise ValueError(f"--resume-name must be a bare filename, got {resume_name!r}")

    path = os.path.join(resume_dir, resume_name)
    if not os.path.isfile(path):
        raise ValueError(f"resume not found in {resume_dir}: {resume_name}")
    return path


def valid_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def record(log_dir, filename, payload):
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, filename), "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def mark_applied(log_dir, url, job_title):
    """Only ever called after a confirmed submit."""
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "applied_jobs.txt"), "a", encoding="utf-8") as f:
        f.write(f"{url}\n")
    record(
        log_dir,
        "applied_jobs_detailed.jsonl",
        {
            "date": datetime.datetime.now().strftime("%Y-%m-%d"),
            "title": job_title,
            "url": url,
        },
    )


async def find_submit(page):
    """Locate the control that submits the application.

    Requiring [type=submit] is not enough: a <button> inside a form defaults to
    submit and Lever's "Submit application" button carries no type attribute at
    all, which made a fully completed form report "no visible submit control".

    Only submit-like wording is matched. "Apply" is deliberately excluded --
    on many pages that is a navigation link, and clicking it would abandon the
    form we just filled.
    """
    for selector in ('button[type="submit"]', 'input[type="submit"]'):
        for element in await page.query_selector_all(selector):
            if await element.is_visible():
                return element, selector

    for element in await page.query_selector_all("form button, button"):
        try:
            if not await element.is_visible():
                continue
            label = (
                (await element.inner_text())
                or (await element.get_attribute("value"))
                or (await element.get_attribute("aria-label"))
                or ""
            ).strip().lower()
            if any(word in label for word in ("submit", "send application")):
                return element, f"text:{label[:30]}"
        except Exception:
            continue
    return None, None


async def find_file_inputs(page):
    """File inputs on SPA application pages can mount a beat late."""
    inputs = await page.query_selector_all('input[type="file"]')
    if inputs:
        return inputs
    await page.wait_for_timeout(3000)
    return await page.query_selector_all('input[type="file"]')


async def fill_first_visible(page, selector, value):
    """Fill matching inputs; return how many were actually filled."""
    filled = 0
    for element in await page.query_selector_all(selector):
        try:
            if await element.is_visible() and await element.is_editable():
                await element.fill(value)
                filled += 1
        except Exception as e:
            log(f"apply: could not fill {selector}: {type(e).__name__}: {e}")
    return filled


async def apply_to_job(url, resume_path, applicant, log_dir, dry_run, headless, shot_dir):
    # Imported lazily so --help and selftest.py work without a browser installed.
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)

            # Open the application form if it sits behind an Apply button.
            for btn in await page.query_selector_all("button, a"):
                try:
                    text = (await btn.inner_text()) or ""
                    if "apply" in text.lower() and await btn.is_visible():
                        await btn.click(timeout=3000)
                        await page.wait_for_timeout(2500)
                        break
                except Exception:
                    continue

            # A page with no file input is not an application form we can
            # complete. Skip rather than pretend.
            file_inputs = await find_file_inputs(page)
            if not file_inputs:
                return {"status": "skipped", "reason": "no file input found; not a completable application form"}

            attached = False
            for f_inp in file_inputs:
                try:
                    await f_inp.set_input_files(resume_path)
                    attached = True
                    break
                except Exception as e:
                    log(f"apply: resume upload rejected: {type(e).__name__}: {e}")

            if not attached:
                return {"status": "skipped", "reason": "resume upload rejected by the page"}

            # ATS platforms name their fields very differently, so match on
            # name/id plus aria-label and placeholder. Ashby in particular
            # exposes almost nothing on name/id alone.
            def any_of(*fragments):
                parts = []
                for fragment in fragments:
                    for attr in ("name", "id", "aria-label", "placeholder"):
                        parts.append(f'input[{attr}*="{fragment}" i]')
                return ", ".join(parts)

            filled = 0
            filled += await fill_first_visible(page, any_of("first"), applicant["firstname"])
            filled += await fill_first_visible(page, any_of("last"), applicant["lastname"])
            filled += await fill_first_visible(page, 'input[type="email"], ' + any_of("email"), applicant["email"])
            filled += await fill_first_visible(page, 'input[type="tel"], ' + any_of("phone", "mobile"), applicant["phone"])
            filled += await fill_first_visible(page, any_of("linkedin"), applicant["linkedin"])
            if applicant.get("password"):
                filled += await fill_first_visible(page, 'input[type="password"]', applicant["password"])

            if filled == 0:
                return {"status": "skipped", "reason": "resume attached but no fillable applicant fields found"}

            await page.wait_for_timeout(2000)

            submit, how = await find_submit(page)
            if submit is not None:
                log(f"apply: submit control matched via {how}")

            if submit is None:
                return {"status": "skipped", "reason": "no visible submit control found", "fields_filled": filled}

            if dry_run:
                return {
                    "status": "skipped",
                    "reason": "dry run: form was completed but submit was not clicked",
                    "fields_filled": filled,
                }

            await submit.click()
            await page.wait_for_timeout(5000)

            # Keep evidence. "Submitted" means we clicked; the screenshot is
            # how you confirm the site accepted it.
            shot = None
            if shot_dir:
                os.makedirs(shot_dir, exist_ok=True)
                shot = os.path.join(shot_dir, hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")
                try:
                    await page.screenshot(path=shot, full_page=False)
                except Exception as e:
                    log(f"apply: screenshot failed: {type(e).__name__}: {e}")
                    shot = None

            return {
                "status": "submitted",
                "reason": "resume attached and submit clicked",
                "fields_filled": filled,
                "final_url": page.url,
                "screenshot": shot,
            }

        finally:
            await context.close()
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description="Submit one job application")
    parser.add_argument("--url", required=True)
    parser.add_argument("--jobtitle", default="Unknown Title")
    parser.add_argument("--resume", help="Absolute path to the resume PDF")
    parser.add_argument("--resume-name", help="Bare filename, resolved inside --resume-dir")
    parser.add_argument("--resume-dir", default=os.getenv("RESUME_DIR", "./resumes"))
    parser.add_argument("--email", required=True)
    parser.add_argument("--firstname", required=True)
    parser.add_argument("--lastname", required=True)
    parser.add_argument("--phone", required=True)
    parser.add_argument("--linkedin", required=True)
    parser.add_argument("--password", default=os.getenv("IWORK_APPLY_PASSWORD", ""),
                        help="Prefer the IWORK_APPLY_PASSWORD env var; argv is visible in ps")
    parser.add_argument("--logdir", default=os.getenv("LOG_DIR", "./logs"))
    parser.add_argument("--dry-run", action="store_true", help="Complete the form but do not submit")
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.set_defaults(headless=True)
    args = parser.parse_args()

    if not valid_url(args.url):
        log(f"apply: FAILED invalid url {args.url!r}")
        return 1

    try:
        resume_path = resolve_resume(args.resume, args.resume_name, args.resume_dir)
    except ValueError as e:
        log(f"apply: FAILED {e}")
        return 1

    applicant = {
        "email": args.email,
        "firstname": args.firstname,
        "lastname": args.lastname,
        "phone": args.phone,
        "linkedin": args.linkedin,
        "password": args.password,
    }

    try:
        result = asyncio.run(
            apply_to_job(
                args.url,
                resume_path,
                applicant,
                args.logdir,
                args.dry_run,
                args.headless,
                os.path.join(args.logdir, "screenshots"),
            )
        )
    except Exception as e:
        log(f"apply: FAILED {type(e).__name__}: {e}")
        print(json.dumps({"status": "error", "reason": str(e), "url": args.url}))
        return 1

    result["url"] = args.url
    result["title"] = args.jobtitle
    result["resume"] = os.path.basename(resume_path)

    if result["status"] == "submitted":
        mark_applied(args.logdir, args.url, args.jobtitle)
    else:
        record(
            args.logdir,
            "skipped_jobs.jsonl",
            {
                "date": datetime.datetime.now().strftime("%Y-%m-%d"),
                "title": args.jobtitle,
                "url": args.url,
                "reason": result.get("reason", ""),
            },
        )

    print(json.dumps(result))
    return 0 if result["status"] == "submitted" else 2


if __name__ == "__main__":
    sys.exit(main())
