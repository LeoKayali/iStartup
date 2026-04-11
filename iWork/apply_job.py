import asyncio
from playwright.async_api import async_playwright
import argparse
import json
import os
import sys
import datetime

async def apply_to_job(url, resume_path, email, password, first_name, last_name, phone, linkedin_url, job_title, log_dir):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            await page.goto(url)
            await page.wait_for_timeout(3000)

            # 1. Click "Apply" if there's a button opening the form
            apply_buttons = await page.query_selector_all('button, a')
            for btn in apply_buttons:
                text = await btn.inner_text()
                if text and 'apply' in text.lower():
                    try:
                        if await btn.is_visible():
                            await btn.click(timeout=2000)
                            await page.wait_for_timeout(2000)
                            break
                    except:
                        pass

            # 2. Fill generic input fields
            inputs_fn = await page.query_selector_all('input[name*="first" i], input[id*="first" i]')
            for inp in inputs_fn:
                if await inp.is_visible() and await inp.is_editable():
                    await inp.fill(first_name)
            
            inputs_ln = await page.query_selector_all('input[name*="last" i], input[id*="last" i]')
            for inp in inputs_ln:
                if await inp.is_visible() and await inp.is_editable():
                    await inp.fill(last_name)

            inputs_email = await page.query_selector_all('input[type="email"], input[name*="email" i], input[id*="email" i]')
            for inp in inputs_email:
                if await inp.is_visible() and await inp.is_editable():
                    await inp.fill(email)

            inputs_phone = await page.query_selector_all('input[type="tel"], input[name*="phone" i], input[id*="phone" i]')
            for inp in inputs_phone:
                if await inp.is_visible() and await inp.is_editable():
                    await inp.fill(phone)

            inputs_li = await page.query_selector_all('input[name*="linkedin" i], input[name*="url" i]')
            for inp in inputs_li:
                if await inp.is_visible() and await inp.is_editable():
                    await inp.fill(linkedin_url)

            inputs_pass = await page.query_selector_all('input[type="password"]')
            for inp in inputs_pass:
                if await inp.is_visible() and await inp.is_editable():
                    await inp.fill(password)

            # 3. File upload for resume
            file_inputs = await page.query_selector_all('input[type="file"]')
            for f_inp in file_inputs:
                try:
                    await f_inp.set_input_files(resume_path)
                except:
                    pass

            await page.wait_for_timeout(2000)

            # 4. Submission
            submit_btns = await page.query_selector_all('button[type="submit"]')
            for btn in submit_btns:
                if await btn.is_visible():
                    await btn.click()
                    break
            
            # Application successful! Log it.
            try:
                if not os.path.exists(log_dir):
                    os.makedirs(log_dir)
                    
                log_file = os.path.join(log_dir, "applied_jobs.txt")
                detailed_file = os.path.join(log_dir, "applied_jobs_detailed.jsonl")
                
                with open(log_file, "a") as f:
                    f.write(f"{url}\n")
                    
                today_str = datetime.datetime.now().strftime("%Y-%m-%d")
                detailed_log = {"date": today_str, "title": job_title, "url": url}
                
                with open(detailed_file, "a") as f:
                    f.write(json.dumps(detailed_log) + "\n")
            except:
                pass

            return {"status": "success", "url": url}
        
        except Exception as e:
            return {"status": "error", "message": str(e), "url": url}
            
        finally:
            await browser.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Automated Job Application')
    parser.add_argument('--url', type=str, required=True, help='Application URL')
    parser.add_argument('--resume', type=str, required=True, help='Resume PDF path')
    parser.add_argument('--email', type=str, required=True, help='Email address')
    parser.add_argument('--password', type=str, required=True, help='Account creation password')
    parser.add_argument('--firstname', type=str, required=True, help='First Name')
    parser.add_argument('--lastname', type=str, required=True, help='Last Name')
    parser.add_argument('--phone', type=str, required=True, help='Phone Number')
    parser.add_argument('--linkedin', type=str, required=True, help='LinkedIn URL')
    parser.add_argument('--jobtitle', type=str, default="Unknown Title", help='Title of the Job')
    parser.add_argument('--logdir', type=str, default="./logs", help='Directory to store logs')
    
    args = parser.parse_args()
    
    result = asyncio.run(apply_to_job(
        args.url, args.resume, args.email, args.password, 
        args.firstname, args.lastname, args.phone, args.linkedin,
        args.jobtitle, args.logdir
    ))
    
    print(json.dumps(result))
