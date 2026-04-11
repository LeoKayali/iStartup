from fastapi import FastAPI, Request
from fastapi.responses import Response
import subprocess
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Configuration from environment variables
PYTHON_PATH = os.getenv("PYTHON_PATH", "python")
SCRIPTS_DIR = os.getenv("SCRIPTS_DIR", "./scripts")
LOG_DIR = os.getenv("LOG_DIR", "./logs")

@app.post("/search")
async def run_search(request: Request):
    info = await request.json()
    script_path = os.path.join(SCRIPTS_DIR, "search_jobs.py")
    cmd = [
        PYTHON_PATH, 
        script_path, 
        "--keyword", info.get("keyword"), 
        "--location", info.get("location")
    ]
    # Run the script and capture its JSON output
    result = subprocess.run(cmd, capture_output=True, text=True)
    return Response(content=result.stdout, media_type="application/json")

@app.post("/apply")
async def run_apply(request: Request):
    info = await request.json()
    script_path = os.path.join(SCRIPTS_DIR, "apply_job.py")
    cmd = [
        PYTHON_PATH, 
        script_path, 
        "--url", info.get("url"), 
        "--jobtitle", info.get("jobtitle", "Unknown Title"),
        "--resume", info.get("resume"), 
        "--email", info.get("email"), 
        "--password", info.get("password"), 
        "--firstname", info.get("firstname"), 
        "--lastname", info.get("lastname"), 
        "--phone", info.get("phone"), 
        "--linkedin", info.get("linkedin")
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return Response(content=result.stdout, media_type="application/json")

@app.get("/stats")
async def get_stats():
    import json
    from datetime import datetime
    
    log_file = os.path.join(LOG_DIR, "applied_jobs.txt")
    detailed_file = os.path.join(LOG_DIR, "applied_jobs_detailed.jsonl")
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    total_applied = 0
    today_count = 0
    today_jobs = []

    try:
        # Get total historical count
        if os.path.exists(log_file):
            with open(log_file, "r") as f:
                lines = [line for line in f if line.strip()]
                total_applied = len(lines)
                
        # Get today's detailed context
        if os.path.exists(detailed_file):
            with open(detailed_file, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            data = json.loads(line)
                            if data.get("date") == today_str:
                                today_count += 1
                                today_jobs.append(data.get("title", "Unknown Title"))
                        except:
                            pass
                            
        return {
            "total_applied": total_applied,
            "today_count": today_count,
            "today_jobs": today_jobs
        }
    except Exception as e:
        return {"error": str(e), "total_applied": 0, "today_count": 0, "today_jobs": []}
