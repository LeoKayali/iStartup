import json
import argparse
import os
from jobspy import scrape_jobs

def fetch_aggregated_jobs(keyword_string, location, log_dir):
    keywords = [k.strip() for k in keyword_string.split(',')]
    all_jobs = []
    
    # Load up the internal memory bank of jobs we applied to already
    applied_urls = set()
    log_file = os.path.join(log_dir, "applied_jobs.txt")
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            applied_urls = set(line.strip() for line in f)

    try:
        for kw in keywords:
            jobs_df = scrape_jobs(
                site_name=["indeed", "zip_recruiter"], 
                search_term=kw,
                location=location,
                results_wanted=5, 
                hours_old=168,   
                country_alfa2="US"
            )
            
            if not jobs_df.empty:
                for index, row in jobs_df.iterrows():
                    all_jobs.append({
                        "title": str(row.get("title", "Unknown Title")),
                        "url": str(row.get("job_url", "")),
                        "description": str(row.get("description", ""))[:3000]
                    })
                    
        # Deduplicate and check memory bank simultaneously
        unique_jobs = {}
        for job in all_jobs:
            current_url = job["url"]
            if current_url and current_url not in unique_jobs and current_url != "nan":
                if current_url not in applied_urls:
                    unique_jobs[current_url] = job
                
        final_list = list(unique_jobs.values())
        
        if len(final_list) == 0:
            return [{
                "title": f"Debug: ZERO fresh matching jobs",
                "url": "N/A",
                "description": "Either zero jobs exist in the last 7 days, or you have already successfully applied to all of them!"
            }]
            
        return final_list

    except Exception as e:
        return [{"title": "Aggregator Error", "url": "N/A", "description": str(e)}]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Search Jobs via Aggregator')
    parser.add_argument('--keyword', type=str, required=True, help='Job keyword(s)')
    parser.add_argument('--location', type=str, required=True, help='Location')
    parser.add_argument('--logdir', type=str, default="./logs", help='Directory to store logs')
    args = parser.parse_args()
    
    results = fetch_aggregated_jobs(args.keyword, args.location, args.logdir)
    print(json.dumps(results))
