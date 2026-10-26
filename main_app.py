#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py - Vercel Serverless-Optimized OJS PDF Email Scraper
"""
import io
import os
import re
import unicodedata
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

MAX_ARTICLES = 50
PDF_PAGES = 3
MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_HTML_BYTES = 4 * 1024 * 1024
TIMEOUT = (10, 30)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def make_session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(BROWSER_HEADERS)
    return s

def decode_html(body, ctype):
    for enc in ("utf-8", (re.search(r"charset=([\w\-]+)", ctype or "", re.I) or [None, "latin-1"])[1], "latin-1"):
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")

# ==============================================================================
# OJS ARTICLE DISCOVERY & ID DEDUPLICATION (4 की जगह 4 ही गिनेगा)
# ==============================================================================
def discover_articles(html_text, issue_url):
    soup = BeautifulSoup(html_text, "html.parser")
    h1 = soup.find("h1")
    issue_title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")
    
    parsed_base = urlparse(issue_url)
    base_domain = f"{parsed_base.scheme}://{parsed_base.netloc}"
    
    articles_map = {}
    
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(issue_url, href)
        
        # सटीक आर्टिकल आईडी निकालना ताकि डुप्लीकेट न हों
        match = re.search(r'/article/(?:view|download|viewFile)/(\d+)', full_url)
        if not match:
            match = re.search(r'/view/(\d+)/\d+', full_url)
            
        if match:
            aid = match.group(1)
            if aid not in articles_map:
                view_url = f"{base_domain}/index.php/jaat/article/view/{aid}"
                articles_map[aid] = {
                    "id": aid,
                    "title": f"Research Article #{aid}",
                    "view_url": view_url,
                    "pdf_candidates": []
                }
            
            text = a.get_text(" ", strip=True)
            text_lower = text.lower()
            
            if 'pdf' in text_lower or '/download/' in full_url or '/viewFile/' in full_url or full_url.endswith('.pdf'):
                if full_url not in articles_map[aid]["pdf_candidates"]:
                    articles_map[aid]["pdf_candidates"].append(full_url)
            elif len(text) > 15 and 'issue' not in text_lower and 'home' not in text_lower and articles_map[aid]["title"].startswith("Research Article"):
                articles_map[aid]["title"] = text

    return issue_title, list(articles_map.values())[:MAX_ARTICLES]

# ==============================================================================
# EMAIL FILTRATION & PDF PARSING
# ==============================================================================
BLOCKED_PREFIXES = {
    "info", "support", "editor", "editors", "editorial", "editorialoffice", "editorinchief",
    "admin", "administrator", "queries", "webmaster", "noreply", "contact", "office",
    "journal", "journals", "submissions", "help", "publisher", "publications", "press",
    "media", "enquiries", "secretary", "secretariat", "ojs", "feedback", "service", "sales", "marketing"
}
BLOCKED_DOMAINS = {"example.com", "pkp.sfu.ca", "sfu.ca", "crossref.org", "orcid.org", "creativecommons.org"}
SLDS = {"ac", "co", "com", "edu", "gov", "net", "org", "sch", "go"}

def base_domain(host):
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in SLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])

EMAIL_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63})@((?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})")

def find_emails(text):
    text = unicodedata.normalize("NFKC", text or "")
    found = []
    for m in EMAIL_RE.finditer(text):
        local, domain = m.group(1).lower().strip(".-"), m.group(2).lower()
        email = f"{local}@{domain}"
        if email not in found:
            found.append(email)
    return found

def is_allowed(email, journal_base):
    local, _, domain = email.partition("@")
    if domain == journal_base or domain.endswith("." + journal_base):
        return False
    if domain in BLOCKED_DOMAINS or any(domain.endswith("." + d) for d in BLOCKED_DOMAINS):
        return False
    norm = re.sub(r"[^a-z]", "", local)
    return norm not in BLOCKED_PREFIXES

def pdf_first_pages_text(data):
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        reader.decrypt("")
    parts = []
    for page in reader.pages[:PDF_PAGES]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    return "\n".join(parts)

def process_single_article(session, art, journal_base):
    emails = []
    status = "error"
    note = ""
    candidates = list(art["pdf_candidates"])
    
    try:
        r = session.get(art["view_url"], timeout=TIMEOUT, headers=BROWSER_HEADERS)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            m_title = soup.find("meta", attrs={"name": "citation_title"})
            if m_title and m_title.get("content"):
                art["title"] = m_title["content"].strip()
                
            m_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
            if m_pdf and m_pdf.get("content"):
                pdf_link = urljoin(art["view_url"], m_pdf["content"])
                if pdf_link not in candidates:
                    candidates.insert(0, pdf_link)
                    
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/download/" in href or "/viewFile/" in href or href.endswith(".pdf"):
                    full_u = urljoin(art["view_url"], href)
                    if full_u not in candidates:
                        candidates.append(full_u)
                        
            std_download = art["view_url"].replace("/view/", "/download/")
            if std_download not in candidates:
                candidates.append(std_download)
    except Exception as e:
        note = f"Page error: {str(e)}"

    pdf_data = None
    for url in candidates:
        try:
            r = session.get(url, timeout=TIMEOUT, headers=BROWSER_HEADERS)
            if r.status_code == 200 and (b"%PDF-" in r.content[:1024] or 'pdf' in r.headers.get('content-type', '').lower()):
                pdf_data = r.content
                break
        except Exception:
            continue
            
    if pdf_data is None:
        return [], "error", note or "PDF download failed."
        
    try:
        text = pdf_first_pages_text(pdf_data)
    except Exception:
        return [], "error", "PDF parsing failed."
        
    if not text.strip():
        return [], "no_text", "PDF text is empty."
        
    extracted = [e for e in find_emails(text) if is_allowed(e, journal_base)]
    status = "ok" if extracted else "no_emails"
    return extracted, status, ("" if extracted else "No author email found.")

# ==============================================================================
# FLASK ROUTES
# ==============================================================================
@app.route("/")
def index():
    return render_template_string(PAGE)

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not url:
        return jsonify({"error": "URL is required."}), 400
    if "://" not in url:
        url = "https://" + url
        
    try:
        session = make_session()
        r = session.get(url, timeout=TIMEOUT, headers=BROWSER_HEADERS)
        r.raise_for_status()
        
        issue_title, arts = discover_articles(r.text, url)
        if not arts:
            return jsonify({"error": "No articles found in this issue URL."}), 400
            
        journal_base = base_domain(urlparse(url).hostname or "")
        
        all_emails = []
        seen_emails = set()
        article_details = []
        
        for art in arts:
            emails, status, note = process_single_article(session, art, journal_base)
            for e in emails:
                if e not in seen_emails:
                    seen_emails.add(e)
                    all_emails.append(e)
                    
            article_details.append({
                "id": art["id"],
                "title": art["title"],
                "url": art["view_url"],
                "emails": emails,
                "status": status,
                "note": note
            })
            
        return jsonify({
            "success": True,
            "issue_title": issue_title,
            "total_articles": len(arts),
            "total_emails": len(all_emails),
            "emails": all_emails,
            "articles": article_details,
            "journal_domain": journal_base
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==============================================================================
# FRONTEND UI
# ==============================================================================
PAGE = r"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Vercel-Optimized OJS Scraper</title>
    <style>
        body { font-family: sans-serif; background: #0b0f19; color: #fff; padding: 30px; max-width: 900px; margin: auto; }
        .card { background: #172036; padding: 25px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.1); margin-bottom: 20px; }
        input { width: 100%; padding: 12px; background: #0f172a; border: 1px solid rgba(255,255,255,0.2); color: #fff; border-radius: 8px; margin-bottom: 12px; box-sizing: border-box; }
        button { padding: 12px 20px; background: #22d3ee; border: none; color: #000; font-weight: bold; border-radius: 8px; cursor: pointer; width: 100%; }
        button:hover { opacity: 0.9; }
        .email-box { background: #0f172a; padding: 15px; border-radius: 8px; font-family: monospace; max-height: 250px; overflow-y: auto; color: #22d3ee; margin-top: 10px; }
        .art { padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .copy-btn { background: #10b981; color: #fff; margin-bottom: 10px; width: auto; padding: 8px 14px; font-size: 13px; }
    </style>
</head>
<body>
    <div class="card">
        <h2>👑 Vercel-Optimized OJS PDF Email Scraper</h2>
        <input id="url" type="text" placeholder="Paste Issue URL (e.g., https://jaat.fudutsinma.edu.ng/index.php/jaat/issue/view/19)">
        <button onclick="startScraping()">Launch Direct Extraction</button>
        <div id="status" style="margin-top:15px;color:#94a3b8">Ready</div>
    </div>

    <div class="card">
        <h3>Unique Extracted Emails (<span id="count">0</span>)</h3>
        <button class="copy-btn" onclick="copyAllEmails()">Copy All Emails</button>
        <div class="email-box" id="emails">No emails yet.</div>
    </div>

    <div class="card">
        <h3>Article Breakdown</h3>
        <div id="arts">Nothing scanned yet.</div>
    </div>

    <script>
        let currentEmails = [];

        async function startScraping() {
            const url = document.getElementById('url').value.trim();
            if(!url) return alert('कृपया OJS Issue URL डालें!');
            
            const statusEl = document.getElementById('status');
            statusEl.innerText = 'Scraping articles and cracking PDFs directly... Please wait.';
            
            try {
                const res = await fetch('/api/scrape', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url: url})
                });
                const data = await res.json();
                
                if(!res.ok || data.error) {
                    statusEl.innerText = 'Error: ' + (data.error || 'Failed');
                    alert(data.error || 'Failed');
                    return;
                }
                
                statusEl.innerText = `Completed! Scanned ${data.total_articles} articles.`;
                currentEmails = data.emails;
                document.getElementById('count').innerText = data.emails.length;
                
                document.getElementById('emails').innerHTML = data.emails.length ? 
                    data.emails.map(e => `<div>${e}</div>`).join('') : 
                    '<span style="color:#ef4444">No author emails found.</span>';
                
                document.getElementById('arts').innerHTML = data.articles.map(a => `
                    <div class="art">
                        <b>${a.title}</b><br>
                        <span style="color:#22d3ee">Emails: ${a.emails.length ? a.emails.join(', ') : 'None'}</span><br>
                        <small style="color:${a.emails.length ? '#10b981' : '#ef4444'}">${a.note || a.status}</small>
                    </div>
                `).join('');
                
            } catch(err) {
                statusEl.innerText = 'Connection Error: ' + err.message;
                alert('Connection Error: ' + err.message);
            }
        }

        function copyAllEmails() {
            if(!currentEmails.length) return alert('कॉपी करने के लिए कोई ईमेल नहीं है!');
            navigator.clipboard.writeText(currentEmails.join('\\n'));
            alert('Copied ' + currentEmails.length + ' emails to clipboard!');
        }
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
