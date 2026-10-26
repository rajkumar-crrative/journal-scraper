#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py - Final Bulletproof OJS Scraper (Exact Article ID Deduplication & Direct PDF Cracking)
"""
import io
import os
import re
import socket
import ipaddress
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)
JOBS = {}
JOBS_LOCK = threading.Lock()

MAX_ARTICLES = 1000
WORKERS = 6
PDF_PAGES = 3
MAX_PDF_BYTES = 35 * 1024 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024
TIMEOUT = (15, 45)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=40)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(BROWSER_HEADERS)
    return s

def assert_public(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Only http(s) URLs are allowed.")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve host '{p.hostname}'.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("Private or internal addresses are blocked.")

def safe_get(session, url, max_bytes, accept="*/*"):
    for _ in range(5):
        assert_public(url)
        headers = dict(BROWSER_HEADERS)
        headers["Accept"] = accept
        r = session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True, headers=headers)
        try:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > max_bytes:
                    raise ValueError("File is too large.")
            return r.url, r.headers.get("Content-Type", ""), bytes(buf)
        finally:
            r.close()
    raise ValueError("Too many redirects or failed request.")

def decode_html(body, ctype):
    for enc in ("utf-8", (re.search(r"charset=([\w\-]+)", ctype or "", re.I) or [None, "latin-1"])[1], "latin-1"):
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")

# ==============================================================================
# STRICT ARTICLE DISCOVERY & ID DEDUPLICATION
# ==============================================================================
def discover_articles(html_text, issue_url):
    soup = BeautifulSoup(html_text, "html.parser")
    h1 = soup.find("h1")
    issue_title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")
    
    articles_map = {}
    
    # इशू पेज पर मिलने वाले सभी लिंक्स को स्कैन करें और आर्टिकल ID निकालें
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(issue_url, href)
        
        # OJS आर्टिकल यूआरएल पैटर्न: /article/view/{id} या /article/download/{id}
        match = re.search(r'/article/(?:view|download|viewFile)/(\d+)', full_url)
        if match:
            aid = match.group(1)
            if aid not in articles_map:
                # बेस व्यू यूआरएल बनाएँ
                parsed = urlparse(full_url)
                base_prefix = f"{parsed.scheme}://{parsed.netloc}"
                path_parts = parsed.path.split('/')
                # OJS पाथ से बेस डोमेन और इंडेक्स ढूंढना
                try:
                    art_idx = path_parts.index('article')
                    base_path = "/".join(path_parts[:art_idx+1])
                    view_url = f"{base_prefix}{base_path}/view/{aid}"
                except ValueError:
                    view_url = f"{base_prefix}/index.php/jaat/article/view/{aid}"
                
                articles_map[aid] = {
                    "id": aid,
                    "title": "",
                    "view_url": view_url,
                    "pdf_candidates": []
                }
            
            # अगर लिंक PDF या डाउनलोड का है, तो उसे उम्मीदवार सूची में जोड़ें
            text_lower = a.get_text(" ", strip=True).lower()
            if 'pdf' in text_lower or '/download/' in full_url or '/viewFile/' in full_url:
                if full_url not in articles_map[aid]["pdf_candidates"]:
                    articles_map[aid]["pdf_candidates"].append(full_url)
            elif not articles_map[aid]["title"] and len(text_lower) > 5 and 'issue' not in text_lower:
                articles_map[aid]["title"] = a.get_text(" ", strip=True)

    # अगर कुछ आर्टिकल्स के टाइटल खाली हैं, तो उन्हें डिफ़ॉल्ट नाम दें
    out = []
    for aid, rec in list(articles_map.items()[:MAX_ARTICLES]):
        if not rec["title"]:
            rec["title"] = f"Research Article #{aid}"
        out.append(rec)
        
    return issue_title, out

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

def process_article(session, art, journal_base):
    art["emails"], art["status"], art["note"] = [], "error", ""
    candidates = list(art["pdf_candidates"])
    
    # 1. आर्टिकल का मुख्य पेज खोलकर असली PDF डाउनलोड लिंक ढूँढना
    try:
        final_url, ctype, body = safe_get(session, art["view_url"], MAX_HTML_BYTES, accept="text/html,*/*;q=0.8")
        html_content = decode_html(body, ctype)
        soup = BeautifulSoup(html_content, "html.parser")
        
        # सही टाइटल अपडेट करें
        m_title = soup.find("meta", attrs={"name": "citation_title"})
        if m_title and m_title.get("content"):
            art["title"] = m_title["content"].strip()
            
        # मेटा टैग से सीधा PDF यूआरएल लें
        m_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if m_pdf and m_pdf.get("content"):
            pdf_link = urljoin(final_url, m_pdf["content"])
            if pdf_link not in candidates:
                candidates.insert(0, pdf_link)
                
        # पेज के सभी लिंक्स से PDF / download लिंक ढूँढें
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/download/" in href or "/viewFile/" in href or href.endswith(".pdf"):
                full_u = urljoin(final_url, href)
                if full_u not in candidates:
                    candidates.append(full_u)
                    
        # अगर OJS का स्टैंडर्ड डाउनलोड लिंक बना सकते हैं
        std_download = art["view_url"].replace("/view/", "/download/")
        if std_download not in candidates:
            candidates.append(std_download)
            
    except Exception as e:
        art["note"] = f"Could not load article page: {str(e)}"

    # 2. PDF डाउनलोड करके क्रैक करना
    pdf_data = None
    for url in candidates:
        try:
            _, ctype, body = safe_get(session, url, MAX_PDF_BYTES, accept="application/pdf,*/*;q=0.8")
            if body[:1024].find(b"%PDF-") != -1 or 'pdf' in ctype.lower():
                pdf_data = body
                art["pdf_url"] = url
                break
        except Exception:
            continue
            
    if pdf_data is None:
        art["note"] = "PDF could not be downloaded or blocked."
        return
        
    try:
        text = pdf_first_pages_text(pdf_data)
    except Exception:
        art["note"] = "PDF parsing failed."
        return
        
    if not text.strip():
        art["status"], art["note"] = "no_text", "PDF has no extractable text."
        return
        
    art["emails"] = [e for e in find_emails(text) if is_allowed(e, journal_base)]
    art["status"] = "ok" if art["emails"] else "no_emails"
    if not art["emails"]:
        art["note"] = "No author email found in first pages."

# ==============================================================================
# BACKGROUND JOBS & ROUTES
# ==============================================================================
def new_job(url):
    job = {"id": uuid.uuid4().hex, "url": url, "status": "queued", "issue_title": "", "total": 0, "processed": 0, "articles": [], "error": None, "created": time.time()}
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    return job

def run_job(job):
    try:
        session = make_session()
        job["status"] = "discovering"
        final, ctype, body = safe_get(session, job["url"], MAX_HTML_BYTES, accept="text/html,*/*;q=0.8")
        title, arts = discover_articles(decode_html(body, ctype), final)
        if not arts:
            raise ValueError("No articles found. Check the issue URL.")
        journal_base = base_domain(urlparse(final).hostname or "")
        
        for a in arts:
            a.update(emails=[], status="pending", note="", pdf_url=None)
        job.update(issue_title=title, articles=arts, total=len(arts), status="running", journal_domain=journal_base)

        count_lock = threading.Lock()
        def work(art):
            process_article(session, art, journal_base)
            with count_lock:
                job["processed"] += 1

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(work, arts))
        job["status"] = "done"
    except Exception as e:
        job.update(status="error", error=str(e))

@app.route("/")
def index():
    return render_template_string(PAGE)

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if url and "://" not in url:
        url = "https://" + url
    try:
        assert_public(url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    job = new_job(url)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify({"job_id": job["id"]})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    emails, seen = [], set()
    arts = []
    for a in job["articles"]:
        for e in a["emails"]:
            if e not in seen:
                seen.add(e)
                emails.append(e)
        arts.append({"id": a["id"], "title": a["title"], "url": a["view_url"], "emails": a["emails"], "status": a["status"], "note": a["note"]})
    return jsonify({"status": job["status"], "error": job["error"], "issue_title": job["issue_title"], "total": job["total"], "processed": job["processed"], "emails": emails, "articles": arts, "journal_domain": job.get("journal_domain", "")})

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bulletproof OJS PDF Scraper</title>
<style>
body{font-family:sans-serif;background:#0b0f19;color:#fff;padding:30px;max-width:900px;margin:auto}
.card{background:#172036;padding:25px;border-radius:12px;border:1px solid rgba(255,255,255,0.1);margin-bottom:20px}
input{width:100%;padding:12px;background:#0f172a;border:1px solid rgba(255,255,255,0.2);color:#fff;border-radius:8px;margin-bottom:12px}
button{padding:12px 20px;background:#22d3ee;border:none;color:#000;font-weight:bold;border-radius:8px;cursor:pointer}
.email-box{background:#0f172a;padding:15px;border-radius:8px;font-family:monospace;max-height:250px;overflow-y:auto;color:#22d3ee;margin-top:10px}
.art{padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.05)}
</style></head>
<body>
<div class="card">
<h2>👑 Bulletproof OJS PDF Email Scraper</h2>
<input id="url" type="text" placeholder="Paste Issue URL (e.g., https://jaat.fudutsinma.edu.ng/index.php/jaat/issue/view/19)">
<button onclick="start()">Start Scraping</button>
<div id="status" style="margin-top:15px;color:#94a3b8">Ready</div>
</div>
<div class="card">
<h3>Unique Extracted Emails (<span id="count">0</span>)</h3>
<button onclick="copyAll()" style="background:#10b981;color:#fff;margin-bottom:10px">Copy All Emails</button>
<div class="email-box" id="emails">No emails yet.</div>
</div>
<div class="card">
<h3>Article Breakdown</h3>
<div id="arts">Nothing scanned yet.</div>
</div>
<script>
let currentEmails = [];
async function start(){
  const url = document.getElementById('url').value.trim();
  if(!url) return alert('Enter URL!');
  document.getElementById('status').innerText = 'Starting job...';
  const res = await fetch('/api/scrape', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
  const data = await res.json();
  if(data.error) return alert(data.error);
  poll(data.job_id);
}
async function poll(id){
  const res = await fetch('/api/status/' + id);
  const data = await res.json();
  document.getElementById('status').innerText = `Status: ${data.status} | Processed: ${data.processed}/${data.total}`;
  currentEmails = data.emails;
  document.getElementById('count').innerText = data.emails.length;
  document.getElementById('emails').innerHTML = data.emails.length ? data.emails.map(e => `<div>${e}</div>`).join('') : 'No emails found yet.';
  document.getElementById('arts').innerHTML = data.articles.map(a => `
    <div class="art">
      <b>${a.title}</b><br>
      <span style="color:#22d3ee">Emails: ${a.emails.join(', ') || 'None'}</span><br>
      <small style="color:#ef4444">${a.note || ''}</small>
    </div>
  `).join('');
  if(data.status === 'running' || data.status === 'discovering') {
    setTimeout(() => poll(id), 1500);
  } else {
    document.getElementById('status').innerText = 'Completed!';
  }
}
function copyAll(){
  navigator.clipboard.writeText(currentEmails.join('\n'));
  alert('Copied ' + currentEmails.length + ' emails!');
}
</script>
</body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
