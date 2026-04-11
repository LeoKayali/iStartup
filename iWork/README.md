# iWork - Autonomous Job Application System

An agentic automation system that scrapes job listings, classifies them using Gemini AI, and automatically applies using Playwright, all orchestrated by n8n.

## Features
- **Job Aggregation**: Scrapes LinkedIn, Indeed, and ZipRecruiter via `jobspy`.
- **AI Classification**: Uses Gemini Pro to analyze job descriptions and select the best resume.
- **Auto-Application**: Automates the application process using Playwright (async).
- **n8n Orchestration**: Fully automated daily workflow.

## Prerequisites
- Python 3.10+
- n8n
- Google Gemini API Key

## Setup

### 1. Python Environment
Install dependencies:
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Environment
Copy `.env.example` to `.env` and configure your paths:
```bash
cp .env.example .env
```

### 3. FastAPI Server
Run the bridge server to allow n8n to trigger local scripts:
```bash
uvicorn server:app --host 0.0.0.0 --port 8080
```

### 4. n8n Workflow
1. Import `workflow_job_automation.json` into n8n.
2. Configure your credentials for:
   - Google Drive (for resumes)
   - Google Gemini (PaLM/Generative AI)
3. Update the HTTP Request nodes to point to your FastAPI server URL.

## Usage
The system is designed to run on a schedule (default: 8 AM daily). It will:
1. Search for jobs matching your keywords.
2. Filter out jobs you've already applied to (tracked in `./logs/applied_jobs.txt`).
3. Use AI to pick a resume.
4. Fill out the application form and submit.

## License
MIT
