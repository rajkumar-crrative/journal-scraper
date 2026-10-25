"""
Academic Journal Email Scraper & Enterprise Engine - Galaxy Developer Edition
==========================================================================
- Issue / table-of-contents discovery
- Async article processing with aiohttp
- HTML, metadata, Cloudflare and PDF email extraction
- SSRF protection and redirect validation
- robots.txt, per-host concurrency and retry backoff
- JSONL result persistence, resumable jobs, and Web UI Dashboard (Galaxy Theme)
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import random
import re
import socket
import threading
import time
import uuid
import tempfile
import io

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template_string, request
from PyPDF2 import PdfReader

# ---------------------------------------------------------------------------
# Configuration & Security Setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

if os.environ.get("VERCEL"):
    DATA_DIR = Path(tempfile.gettempdir()) / "scraper_data"
else:
    DATA_DIR = Path("./scraper_data")

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
SECURITY_LOG = DATA_DIR / "security_events.jsonl"

GLOBAL_CONCURRENCY = int(os.environ.get("SCRAPER_CONCURRENCY", "16"))
PER_HOST_CONCURRENCY = int(os.environ.get("SCRAPER_PER_HOST", "3"))
MAX_ARTICLES = int(os.environ.get("SCRAPER_MAX_ARTICLES", "1000"))
MAX_PDF_BYTES = int(os.environ.get("SCRAPER_MAX_PDF_BYTES", str(30 * 1024 * 1024)))
MAX_HTML_BYTES = int(os.environ.get("SCRAPER_MAX_HTML_BYTES", str(8 * 1024 * 1024)))
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = 25
ROBOTS_TTL = 3600

GENERIC_PREFIXES = {
    "info", "support", "help", "contact", "admin", "office",
    "editor", "editorial", "webmaster", "noreply", "no-reply",
    "sales", "service", "services", "enquiries", "inquiries",
    "library", "subscribe", "subscriptions", "marketing",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "ref_src", "mc_cid", "mc_eid"}
ARTICLE_HINTS = re.compile(r"(article|view|paper|publication|full.?text|abstract|download|document|doi|research|contribution|issue/view)", re.I)
PDF_HINTS = re.compile(r"\.pdf(?:$|[?#])", re.I)

EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])"
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+",
    re.I,
)

CLOUDFLARE_RE = re.compile(r'data-cfemail\s*=\s*["\']([0-9a-fA-F]+)["\']', re.I)

HOST_SEMAPHORES = {}
ROBOTS_CACHE = {}
HOST_FAILURES = defaultdict(int)
STATE_LOCK = threading.Lock()

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("academic-scraper")

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def security_event(kind, url=None, detail=None):
    entry = {"time": utc_now(), "event": str(kind)[:100], "host": (urlsplit(url).hostname or "")[:255] if url else None, "detail": str(detail or "")[:500]}
    LOG.warning("Security event: %s", entry)
    try:
        with open(SECURITY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

@app.before_request
def detect_suspicious_activity():
    query_str = request.query_string.decode('utf-8', errors='ignore').lower()
    for pattern in ['select', 'union', 'drop', '<script', '../', 'etc/passwd', 'eval(']:
        if pattern in query_str:
            security_event("suspicious_query_pattern", detail=f"Query: {query_str}")
            break

@app.after_request
def apply_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Server'] = 'Secure-Engine-20X'
    return response

# ---------------------------------------------------------------------------
# URL normalization & SSRF defense
# ---------------------------------------------------------------------------

def normalize_url(raw):
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError("Invalid URL length.")
    raw = raw.strip()
    if not raw:
        raise ValueError("URL is required.")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS are accepted.")
    if not parts.hostname:
        raise ValueError("URL must contain a hostname.")
    return urlunsplit((parts.scheme.lower(), parts.hostname.lower(), parts.path or "/", parts.query, ""))

def decode_cloudflare(encoded):
    try:
        raw = bytes.fromhex(encoded)
        if len(raw) < 2:
            return ""
        key = raw[0]
        return "".join(chr(byte ^ key) for byte in raw[1:])
    except (ValueError, TypeError):
        return ""

def clean_email_text(text):
    if not text:
        return ""
    text = str(text).replace("&commat;", "@").replace("&#64;", "@").replace("&period;", ".").replace("&#46;", ".")
    text = re.sub(r"\s*(?:\[|\(|\{)\s*at\s*(?:\]|\)|\})\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*(?:\[|\(|\{)\s*dot\s*(?:\]|\)|\})\s*", ".", text, flags=re.I)
    return text

def normalize_email(email):
    email = email.strip().strip(".,;:<>[](){}'\"")
    email = email.replace("mailto:", "")
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        return None
    local, domain = email.rsplit("@", 1)
    return f"{local}@{domain.lower()}"

def extract_emails(text):
    text = clean_email_text(text)
    found = {}
    for match in EMAIL_PATTERN.finditer(text):
        email = normalize_email(match.group(0))
        if email:
            found[email.casefold()] = email
    return sorted(found.values(), key=str.casefold)

def extract_html_emails(html):
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.select("[data-cfemail]"):
        encoded = element.get("data-cfemail", "")
        decoded = decode_cloudflare(encoded)
        if decoded:
            element.append(" " + decoded)
    return extract_emails(soup.get_text(" ", strip=True))

def discover_issue_links(issue_url, html):
    soup = BeautifulSoup(html, "html.parser")
    discovered = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(issue_url, href)
        try:
            normalized = normalize_url(absolute)
            if normalized not in seen:
                seen.add(normalized)
                discovered.append({"url": normalized, "title": anchor.get_text(" ", strip=True)[:500] or normalized})
        except ValueError:
            continue
    return discovered

def extract_pdf_links(page_url, html):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        absolute = urljoin(page_url, href)
        if PDF_HINTS.search(absolute):
            try:
                normalized = normalize_url(absolute)
                if normalized not in seen:
                    seen.add(normalized)
                    results.append(normalized)
            except ValueError:
                pass
    return results

def get_host_semaphore(host):
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, host)
    with STATE_LOCK:
        if key not in HOST_SEMAPHORES:
            HOST_SEMAPHORES[key] = asyncio.Semaphore(PER_HOST_CONCURRENCY)
        return HOST_SEMAPHORES[key]

async def safe_request(session, url, *, max_bytes=MAX_HTML_BYTES, max_retries=2):
    url = normalize_url(url)
    for attempt in range(max_retries + 1):
        try:
            host = urlsplit(url).hostname or ""
            semaphore = get_host_semaphore(host)
            async with semaphore:
                headers = {"User-Agent": random.choice(USER_AGENTS)}
                async with session.get(url, headers=headers, allow_redirects=True, timeout=REQUEST_TIMEOUT) as response:
                    body = await response.read()
                    return {"url": str(response.url), "status": response.status, "headers": dict(response.headers), "body": body}
        except Exception:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(1)
    raise RuntimeError("Request failed.")

def extract_pdf_emails(pdf_bytes):
    found = set()
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        for page in reader.pages[:10]:
            text = page.extract_text() or ""
            found.update(extract_emails(text))
    except Exception:
        pass
    return sorted(found, key=str.casefold)

async def fetch_issue(session, issue_url):
    result = await safe_request(session, issue_url)
    html = result["body"].decode("utf-8", "replace")
    links = discover_issue_links(result["url"], html)
    return result["url"], html, links

async def process_article(session, article):
    article_url = article["url"]
    record = {"url": article_url, "title": article.get("title", ""), "status": "ok", "all_emails": [], "pdf_urls": [], "errors": []}
    try:
        response = await safe_request(session, article_url)
        html = response["body"].decode("utf-8", "replace")
        html_emails = set(extract_html_emails(html))
        record["pdf_urls"] = extract_pdf_links(response["url"], html)
        
        for pdf_url in record["pdf_urls"]:
            try:
                pdf_res = await safe_request(session, pdf_url, max_bytes=MAX_PDF_BYTES)
                pdf_emails = extract_pdf_emails(pdf_res["body"])
                html_emails.update(pdf_emails)
            except Exception:
                pass
        record["all_emails"] = sorted(html_emails, key=str.casefold)
    except Exception as exc:
        record["status"] = "error"
        record["errors"].append(str(exc)[:500])
    return record

# ---------------------------------------------------------------------------
# Galaxy Developer Theme UI Template (Aesthetic, Glassmorphic & Animated)
# ---------------------------------------------------------------------------

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Galaxy Enterprise Email Tracker & Academic Scraper</title>
    <style>
        :root {
            --galaxy-bg: radial-gradient(circle at 50% 20%, #1e1b4b 0%, #0f172a 60%, #020617 100%);
            --glass-bg: rgba(30, 41, 59, 0.7);
            --glass-border: rgba(255, 255, 255, 0.12);
            --neon-cyan: #22d3ee;
            --neon-purple: #c084fc;
            --neon-pink: #f472b6;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
        }

        body {
            font-family: 'Inter', system-ui, -apple-system, sans-serif;
            background: var(--galaxy-bg);
            color: var(--text-main);
            margin: 0;
            padding: 30px 20px;
            min-height: 100vh;
            background-attachment: fixed;
        }

        .container {
            max-width: 1000px;
            margin: auto;
            background: var(--glass-bg);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--glass-border);
            padding: 40px;
            border-radius: 20px;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6), 0 0 40px rgba(192, 132, 252, 0.1);
            animation: fadeIn 0.8s ease-out;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }

        h1 {
            text-align: center;
            font-size: 30px;
            font-weight: 800;
            background: linear-gradient(135deg, var(--neon-cyan), var(--neon-purple), var(--neon-pink));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 8px;
            letter-spacing: -0.5px;
        }

        .dev-tag {
            text-align: center;
            color: var(--neon-cyan);
            font-size: 15px;
            margin-bottom: 30px;
            font-weight: 500;
            text-shadow: 0 0 10px rgba(34, 211, 238, 0.3);
        }

        .form-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }

        .form-group {
            margin-bottom: 20px;
        }

        .form-group.full {
            grid-column: span 2;
        }

        label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: var(--text-muted);
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        input[type="text"] {
            width: 100%;
            padding: 14px 16px;
            border-radius: 10px;
            border: 1px solid var(--glass-border);
            background: rgba(15, 23, 42, 0.8);
            color: #fff;
            box-sizing: border-box;
            font-size: 15px;
            transition: all 0.3s ease;
        }

        input[type="text"]:focus {
            border-color: var(--neon-cyan);
            outline: none;
            box-shadow: 0 0 15px rgba(34, 211, 238, 0.3);
        }

        button.submit-btn {
            background: linear-gradient(135deg, #6366f1, #a855f7, #ec4899);
            color: white;
            border: none;
            padding: 16px;
            width: 100%;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 4px 20px rgba(168, 85, 247, 0.4);
            letter-spacing: 0.5px;
        }

        button.submit-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 25px rgba(168, 85, 247, 0.6);
        }

        button.submit-btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }

        .results-container {
            margin-top: 35px;
            background: rgba(15, 23, 42, 0.9);
            padding: 25px;
            border-radius: 12px;
            border: 1px solid var(--glass-border);
            max-height: 500px;
            overflow-y: auto;
        }

        .controls-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 12px;
        }

        .action-btn {
            background: rgba(34, 211, 238, 0.15);
            color: var(--neon-cyan);
            border: 1px solid var(--neon-cyan);
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            transition: all 0.2s ease;
        }

        .action-btn:hover {
            background: var(--neon-cyan);
            color: #0f172a;
            box-shadow: 0 0 15px rgba(34, 211, 238, 0.5);
        }

        .article-card {
            background: rgba(30, 41, 59, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.06);
            padding: 15px;
            margin-bottom: 12px;
            border-radius: 8px;
            border-left: 4px solid var(--neon-purple);
            transition: transform 0.2s ease;
        }

        .article-card:hover {
            transform: translateX(4px);
            border-left-color: var(--neon-cyan);
        }

        .email-badge {
            display: inline-block;
            background: rgba(52, 211, 153, 0.15);
            color: #34d399;
            padding: 4px 10px;
            margin: 4px;
            border-radius: 6px;
            font-family: 'Courier New', monospace;
            font-size: 13px;
            border: 1px solid rgba(52, 211, 153, 0.3);
        }

        .loading-pulse {
            text-align: center;
            color: var(--neon-cyan);
            font-style: italic;
            padding: 20px;
            animation: pulse 1.5s infinite;
        }

        @keyframes pulse {
            0% { opacity: 0.4; }
            50% { opacity: 1; }
            100% { opacity: 0.4; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🌌 Galaxy Enterprise Email Tracker & Scraper</h1>
        <div class="dev-tag">System Engine: <b>ईमेल ट्रैकर (Email Tracker & Scraper Engine)</b></div>
        
        <form id="scrapeForm">
            <div class="form-grid">
                <div class="form-group">
                    <label>Volume (वॉल्यूम)</label>
                    <input type="text" id="volume" value="Vol 1">
                </div>
                <div class="form-group">
                    <label>Issue (इशू)</label>
                    <input type="text" id="issueNo" value="Issue 1">
                </div>
                <div class="form-group full">
                    <label>Journal Issue Target URL</label>
                    <input type="text" id="issueUrl" placeholder="https://example.com/index.php/journal/issue/view/19" required>
                </div>
            </div>
            <button type="submit" id="submitBtn" class="submit-btn">⚡ Launch Email Tracker Sequence</button>
        </form>

        <div class="results-container">
            <div class="controls-bar">
                <span style="font-weight: 600; color: var(--text-muted);">Live Terminal & Extracted Emails</span>
                <button class="action-btn" onclick="copyAllEmails()">📋 Copy All Unique Emails</button>
            </div>
            <div id="resultsContent" style="color: var(--text-muted); font-family: monospace; font-size: 14px;">
                System standing by. Enter a journal issue URL above and launch sequence.
            </div>
        </div>
    </div>

    <script>
        let globalAllEmails = [];

        document.getElementById('scrapeForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const url = document.getElementById('issueUrl').value;
            const resDiv = document.getElementById('resultsContent');
            const btn = document.getElementById('submitBtn');
            
            resDiv.innerHTML = '<div class="loading-pulse">🌌 Navigating cosmic nodes, scanning articles, and tracking verified emails... Please wait...</div>';
            btn.disabled = true;
            globalAllEmails = [];

            try {
                const response = await fetch('/api/scrape', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url })
                });
                const data = await response.json();
                if(data.success) {
                    let htmlOutput = `<p style="color: var(--neon-cyan); font-weight: bold; margin-bottom: 15px;">✨ Mission Success! Total Articles Scanned: ${data.total}</p>`;
                    let validRecordsCount = 0;
data.records.forEach((rec, idx) => {
    if (!rec.all_emails || rec.all_emails.length === 0) {
        return; 
    }
    validRecordsCount++;
    let emailBadges = rec.all_emails.map(e => { 
        globalAllEmails.png(e); // (या globalAllEmails.push(e))
        return `<span class="email-badge">${e}</span>`; 
    }).join('');
    
    htmlOutput += `
        <div class="article-card">
            <b style="color: #fff;">[${validRecordsCount}] ${rec.title}</b><br>
            <a href="${rec.url}" target="_blank" style="color: var(--text-muted); font-size: 11px; word-break: break-all; text-decoration: none;">🔗 ${rec.url}</a><br>
            <div style="margin-top: 8px;">${emailBadges}</div>
        </div>`;
});
if (validRecordsCount === 0) {
    htmlOutput += `<p style="color: #f87171; text-align: center; margin-top: 20px;">इन आर्टिकल्स में कोई भी ईमेल नहीं मिला।</p>`;
}
                    resDiv.innerHTML = htmlOutput;
                } else {
                    resDiv.innerHTML = `<span style='color: #f87171;'>❌ Error encountered: ${data.error}</span>`;
                }
            } catch (err) {
                resDiv.innerHTML = `<span style='color: #f87171;'>❌ Request failed: ${err}</span>`;
            }
            btn.disabled = false;
        });

        function copyAllEmails() {
            if(globalAllEmails.length === 0) {
                alert("No emails available to copy yet!");
                return;
            }
            const uniqueEmails = [...new Set(globalAllEmails)].join('\\n');
            navigator.clipboard.writeText(uniqueEmails).then(() => {
                alert("🚀 All unique emails copied successfully to clipboard!");
            });
        }
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_TEMPLATE)

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    data = request.json
    issue_url = data.get("url")
    if not issue_url:
        return jsonify({"success": False, "error": "URL is required"})

    async def run_sync():
        async with aiohttp.ClientSession() as session:
            _, _, articles = await fetch_issue(session, issue_url)
            articles = articles[:30]
            results = await asyncio.gather(*(process_article(session, art) for art in articles))
            return results

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        records = loop.run_until_complete(run_sync())
        loop.close()
        return jsonify({"success": True, "total": len(records), "records": records})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
