import re
import io
import asyncio
import aiohttp
import logging
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, Response, stream_with_context
from urllib.parse import urljoin, urlparse

# Setup Security Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [SECURITY DETECT] - %(message)s')

# PDF Support Check
try:
    import PyPDF2
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

app = Flask(__name__)

# Full Security Headers to protect backend identity & stop hackers + Attack Logging
@app.before_request
def detect_suspicious_activity():
    # Detect common hack/exploit payloads (SQLi, XSS, Path Traversal)
    query_str = request.query_string.decode('utf-8', errors='ignore').lower()
    suspicious_patterns = ['select', 'union', 'drop', '<script', '../', 'etc/passwd', 'eval(']
    
    for pattern in suspicious_patterns:
        if pattern in query_str:
            client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            user_agent = request.headers.get('User-Agent', 'Unknown')
            logging.warning(f"ATTEMPT DETECTED | IP: {client_ip} | Path: {request.path} | UA: {user_agent} | Query: {query_str}")
            break

@app.after_request
def apply_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Server'] = 'Secure-Engine-20X'
    return response

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1'
}

def clean_and_format_email(email_str):
    email = email_str.lower().strip()
    email = re.sub(r'\s*[\(\[\{]at[\)\]\}]\s*', '@', email)
    email = re.sub(r'\s*[\(\[\{]dot[\)\]\}]\s*', '.', email)
    return email

def extract_emails_from_text(text):
    standard_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    obfuscated_pattern = r'[a-zA-Z0-9._%+-]+\s*[\(\[\{]at[\)\]\}]\s*[a-zA-Z0-9.-]+\s*[\(\[\{]dot[\)\]\}]\s*[a-zA-Z]{2,}'
    
    found = re.findall(standard_pattern, text)
    obf_matches = re.findall(obfuscated_pattern, text, re.IGNORECASE)
    
    for match in obf_matches:
        found.append(clean_and_format_email(match))
        
    return set(found)

def read_pdf_bytes(pdf_bytes):
    emails = set()
    if not PDF_SUPPORT:
        return emails
    try:
        pdf_file = io.BytesIO(pdf_bytes)
        reader = PyPDF2.PdfReader(pdf_file)
        for page in reader.pages[:3]:
            txt = page.extract_text()
            if txt:
                emails.update(extract_emails_from_text(txt))
    except Exception:
        pass
    return emails

async def fetch_page(session, url):
    try:
        async with session.get(url, headers=HEADERS, timeout=6, ssl=False) as resp:
            if resp.status == 200:
                c_type = resp.headers.get('Content-Type', '').lower()
                if 'application/pdf' in c_type or url.endswith('.pdf'):
                    pdf_b = await resp.read()
                    return read_pdf_bytes(pdf_b), set()
                
                html = await resp.text()
                emails = extract_emails_from_text(html)
                soup = BeautifulSoup(html, 'html.parser')
                links = set()
                
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if href.startswith('mailto:'):
                        e = href.replace('mailto:', '').split('?')[0].strip()
                        if '@' in e:
                            emails.add(e.lower())
                    else:
                        full_url = urljoin(url, href)
                        if urlparse(full_url).netloc == urlparse(url).netloc or full_url.endswith('.pdf'):
                            links.add(full_url)
                return emails, links
    except Exception:
        pass
    return set(), set()

# 20X Concurrency & High-Speed Async Engine
async def stream_1500_crawler(base_url, max_pages=1500):
    visited = set()
    to_visit = {base_url}
    all_emails = set()
    
    # Upgraded limit to 200 for 20x concurrency boost while remaining Vercel-safe
    connector = aiohttp.TCPConnector(limit=200, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while to_visit and len(visited) < max_pages:
            batch = list(to_visit - visited)[:100]  # Expanded batch size for maximum speed
            if not batch:
                break
                
            visited.update(batch)
            to_visit.difference_update(batch)
            
            tasks = [fetch_page(session, u) for u in batch]
            results = await asyncio.gather(*tasks)
            
            new_found = set()
            for ems, lks in results:
                new_found.update(ems)
                for l in lks:
                    if l not in visited and len(visited) + len(to_visit) < max_pages:
                        to_visit.add(l)
            
            added_emails = new_found - all_emails
            if added_emails:
                all_emails.update(added_emails)
                yield f"data: {list(added_emails)}\n\n"
                
            await asyncio.sleep(0.005)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Enterprise 20X Speed & Secure Email Extractor</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #090d16; color: #e2e8f0; margin: 0; padding: 20px; }
        .container { max-width: 850px; margin: auto; background: #111827; padding: 30px; border-radius: 12px; border: 1px solid #1f2937; }
        h2 { text-align: center; color: #38bdf8; font-size: 24px; margin-bottom: 20px; }
        input[type="url"] { width: 100%; padding: 14px; border-radius: 8px; border: 1px solid #374151; background: #030712; color: #fff; box-sizing: border-box; font-size: 16px; margin-bottom: 15px; }
        button { width: 100%; padding: 14px; background: #2563eb; border: none; color: #fff; font-size: 16px; border-radius: 8px; cursor: pointer; font-weight: 600; }
        button:hover { background: #1d4ed8; }
        #results { width: 100%; height: 350px; background: #030712; border: 1px solid #374151; color: #4ade80; padding: 12px; border-radius: 8px; box-sizing: border-box; margin-top: 15px; font-family: monospace; white-space: pre-wrap; overflow-y: auto; }
        .counter { color: #facc15; font-weight: bold; margin-top: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <h2>⚡ Enterprise Secure 20X Speed 1500+ PDF & HTML Extractor</h2>
        <input type="url" id="urlInput" placeholder="Enter Journal / Website Main Link..." required>
        <button onclick="startScanning()">Start Super-Fast Secure Scan</button>
        
        <div class="counter">Live Emails Extracted: <span id="count">0</span></div>
        <div id="results"></div>
    </div>

    <script>
        let uniqueEmails = new Set();
        function startScanning() {
            const url = document.getElementById('urlInput').value;
            if(!url) return alert('Link daliye!');
            
            const resultsBox = document.getElementById('results');
            const countBox = document.getElementById('count');
            resultsBox.innerText = "Scanning started... 20X Turbo Engine active... Scanning 1500+ pages & PDFs...\n";
            uniqueEmails.clear();
            countBox.innerText = "0";

            const eventSource = new EventSource('/stream?url=' + encodeURIComponent(url));
            eventSource.onmessage = function(event) {
                const emails = JSON.parse(event.data.replace(/'/g, '"'));
                emails.forEach(email => {
                    if(!uniqueEmails.has(email)) {
                        uniqueEmails.add(email);
                        resultsBox.innerText += email + "\n";
                    }
                });
                countBox.innerText = uniqueEmails.size;
            };

            eventSource.onerror = function() {
                eventSource.close();
                resultsBox.innerText += "\n--- Scanning Completed ---";
            };
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/stream')
def stream():
    target_url = request.args.get('url')
    if not target_url:
        return Response("URL required", status=400)

    def generate():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        crawler = stream_1500_crawler(target_url, max_pages=1500)
        try:
            while True:
                data = loop.run_until_complete(crawler.__anext__())
                yield data
        except StopAsyncIteration:
            pass
        finally:
            loop.close()

    return Response(stream_with_context(generate()), content_type='text/event-stream')

if __name__ == '__main__':
    app.run(debug=False)
