# iFund - Autonomous Venture Funding Outreach

An n8n workflow designed to automate cold outreach to venture funds. It reads lead data from a Google Sheet, sends personalized emails via Resend, and updates the sheet status.

## Features
- **Google Sheets Integration**: Pulls funding leads dynamically.
- **Resend Emailing**: Sends high-deliverability emails via Resend API.
- **Automated Tracking**: Updates the status of each lead in the sheet after sending.
- **Wait Mechanism**: Built-in delays to ensure natural delivery patterns.

## Prerequisites
- n8n
- Google Cloud Console access (for Google Sheets API)
- Resend API Key

## Setup

### 1. Google Sheet Setup
1. Create a Google Sheet with the following columns:
   - `Contact` (Name)
   - `Email` (Target address)
   - `Status` (Set to empty for new leads)
   - `row_number` (Unique ID for each row)
2. Share the sheet with your Google Service Account email.

### 2. n8n Configuration
1. Import `workflow_funding_outreach.json` into n8n.
2. Set up your **Google Sheets OAuth2** credentials.
3. Set up your **Resend API** credentials.
4. Replace `[YOUR_GOOGLE_SHEET_ID]` in the Google Sheets nodes.
5. Replace `[YOUR_VERIFIED_RESEND_EMAIL]` in the Resend node with your verified sender domain/email.

## License
MIT
