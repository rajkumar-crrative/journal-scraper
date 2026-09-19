import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, jsonify
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# Headers to bypass basic bot protection
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def clean_and_format_email(email_str):
    """Normalize emails that use [at], (at), [dot], etc."""
    email = email_str.lower().strip()
    email = re.sub(r'\s*[\(\[\{]at[\)\]\}]\s*', '@', email)
    email = re.sub(r'\s*[\(\[\{]dot[\)\]\}]\s*', '.', email)
    return email

def extract_emails_from_text(text):
    """Advanced regex to capture standard and obfuscated emails."""
    # Standard email pattern
    standard_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    # Obfuscated pattern like name[at]domain[dot]com
    obfuscated_pattern = r'[a-zA-Z0-9._%+-]+\s*[\(\[\{]at[\)\]\}]\s*[a-zA-Z0-9.-]+\s*[\(\[\{]dot[\)\]\}]\s*[a-zA-Z]{2,}'
    
    found_emails = re.findall(standard_pattern, text)
    obfuscated_matches = re.findall(obfuscated_pattern, text, re.IGNORECASE)
    
    for match in obfuscated_matches:
        cleaned = clean_and_format_email(match)
        found_emails.append(cleaned)
        
    return set(found_emails)

def fetch_and_extract(url):
    """Fetch single page content and extract emails & sub-links."""
    emails = set()
    links = set()
    try:
        response = requests.get(url, headers=HEADERS, timeout=8)
        if response.status_code == 200:
            # Extract from raw text
            emails.update(extract_emails_from_text(response.text))
            
            # Extract mailto links and sub-links
            soup = BeautifulSoup(response.text, 'html.parser')
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href'].strip()
                if href.startswith('mailto:'):
                    clean_email = href.replace('mailto:', '').split('?')[0].strip()
                    if '@' in clean_email:
                        emails.add(clean_email.lower())
                else:
                    full_url = urljoin(url, href)
                    # Filter same domain links for deep scanning
                    if urlparse(full_url).netloc == urlparse(url).netloc:
                        links.add(full_url)
    except Exception:
        pass
    return emails, links

def fast_deep_scrape(base_url, max_pages=15):
    """Multi-threaded deep scraper for large sites."""
    visited = set()
    to_visit = {base_url}
    all_emails = set()
    
    # ThreadPoolExecutor for fast parallel requests
    with ThreadPoolExecutor(max_workers=10) as executor:
        while to_visit and len(visited) < max_pages:
            current_batch = list(to_visit - visited)[:10]
            visited.update(current_batch)
            to_visit.difference_update(current_batch)
            
            future_to_url = {executor.submit(fetch_and_extract, url): url for url in current_batch}
            
            for future in as_completed(future_to_url):
                emails, links = future.result()
                all_emails.update(emails)
                # Add new discovered links to visit
                for link in links:
                    if link not in visited:
                        to_visit.add(link)
                        
    # Filter unwanted extensions falsely caught as emails
    valid_emails = [
        e for e in all_emails 
        if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.svg'))
    ]
    return sorted(list(set(valid_emails)))

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fast Deep Email Extractor Pro</title>
    <style>
        body { font-family: Arial, sans-serif; background: #0f172a; color: #fff; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: auto; background: #1e293b; padding: 25px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
        h2 { text-align: center; color: #38bdf8; margin-bottom: 20px; }
        .input-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; color: #94a3b8; }
        input[type="url"] { width: 100%; padding: 12px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: #fff; box-sizing: border-box; }
        button { width: 100%; padding: 12px; background: #0284c7; border: none; color: #fff; font-size: 16px; border-radius: 6px; cursor: pointer; font-weight: bold; }
        button:hover { background: #0369a1; }
        .result-box { margin-top: 20px; }
        textarea { width: 100%; height: 250px; background: #0f172a; border: 1px solid #334155; color: #4ade80; padding: 10px; border-radius: 6px; box-sizing: border-box; }
        .status { margin-top: 10px; color: #facc15; font-size: 14px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h2>⚡ High-Speed Deep Email Extractor</h2>
        <form method="POST" action="/extract">
            <div class="input-group">
                <label for="url">Website / Journal URL Enter Karein:</label>
                <input type="url" id="url" name="url" placeholder="https://example-journal.com/issue-1" required>
            </div>
            <button type="submit">Start Fast Deep Extraction</button>
        </form>
        
        {% if emails is not none %}
        <div class="result-box">
            <h3>Extracted Emails (Total: {{ emails|length }}):</h3>
            <textarea readonly>{{ emails | join('\n') }}</textarea>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML_TEMPLATE, emails=None)

@app.route('/extract', methods=['POST'])
def extract():
    target_url = request.form.get('url')
    if target_url:
        found_emails = fast_deep_scrape(target_url, max_pages=15)
        return render_template_string(HTML_TEMPLATE, emails=found_emails)
    return render_template_string(HTML_TEMPLATE, emails=[])

if __name__ == '__main__':
    app.run(debug=True)
