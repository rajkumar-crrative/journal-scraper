#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py - OJS (Open Journal Systems) issue scraper: unique articles + corresponding-author emails.

Install:  pip install Flask requests beautifulsoup4 pypdf
Run:      python app.py            (then open http://localhost:5000)
Prod:     gunicorn -w 1 --threads 16 -b 0.0.0.0:$PORT app:app   (keep 1 worker: jobs live in memory)

How it works
  1. Fetches the OJS issue page and collects article links, de-duplicated by core article ID
     (/article/view/{id}, /article/view/{id}/{galley}, /article/download/{id}/{galley} -> one article).
  2. For every unique article it locates the PDF galley, downloads it, and reads the first 3 pages
     with pypdf (where corresponding-author details normally are).
  3. Extracts emails with regex (after repairing PDF line breaks / [at] obfuscation), then filters out
     role addresses (info@, editor@, support@ ...) and the journal's own host-university domain.
  4. A small embedded UI starts a background job and polls it, so 900+ article issues do not time out.
"""
import io
import ipaddress
import os
import re
import socket
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ───────────────────────────── configuration ─────────────────────────────
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", 1000))
WORKERS = int(os.environ.get("WORKERS", 8))
PDF_PAGES = 3                                  # pages read per PDF
MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_HTML_BYTES = 4 * 1024 * 1024
TIMEOUT = (8, 30)                              # connect, read
MAX_ACTIVE_JOBS = 3
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

app = Flask(__name__)
JOBS = {}
JOBS_LOCK = threading.Lock()

# ───────────────────────────── networking ─────────────────────────────
def make_session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.7, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=40)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def assert_public(url):
    """SSRF guard: only http(s) URLs that resolve exclusively to public IP addresses."""
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
            raise ValueError("Requests to private or internal addresses are blocked.")


def safe_get(session, url, max_bytes, truncate=False, accept="*/*"):
    """GET with manual redirect handling (every hop is SSRF-checked) and a hard size cap."""
    for _ in range(6):
        assert_public(url)
        r = session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=False, headers={"Accept": accept})
        try:
            if r.is_redirect and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > max_bytes:
                    if truncate:
                        break
                    raise ValueError("File is too large.")
            return url, r.headers.get("Content-Type", ""), bytes(buf)
        finally:
            r.close()
    raise ValueError("Too many redirects.")


def decode_html(body, ctype):
    for enc in ("utf-8", (re.search(r"charset=([\w\-]+)", ctype or "", re.I) or [None, "latin-1"])[1], "latin-1"):
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


# ───────────────────── article discovery (ID de-duplication) ─────────────────────
ART_RE = re.compile(r"^(?P<prefix>.*?/article)/(?:view|download|viewFile|viewArticle)/(?P<id>\d+)(?:/(?P<gal>\d+))?/?$", re.I)
GENERIC_LABEL = re.compile(r"^\W*(pdf|html|xml|epub|full\s*text|abstract|download|view|read\s*more|get\s*pdf|download\s*pdf)\W*$", re.I)


def discover_articles(html_text, issue_url):
    """Return (issue_title, ordered list of unique articles keyed by core article ID)."""
    soup = BeautifulSoup(html_text, "html.parser")
    h1 = soup.find("h1")
    issue_title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")
    # drop sidebars/menus: they list "most read" articles from OTHER issues
    for sel in ("nav", "footer", "aside", ".pkp_structure_sidebar", ".pkp_block", "#sidebar", ".pkp_site_nav_menu"):
        for t in soup.select(sel):
            t.decompose()
    scope = soup.select_one(".obj_issue_toc") or soup.select_one("main") or soup
    arts, order = {}, []
    for a in scope.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        p = urlparse(urljoin(issue_url, href.split("#")[0]))
        m = ART_RE.match(p.path)
        if not m or p.netloc != urlparse(issue_url).netloc:
            continue
        aid = m.group("id")
        prefix = urlunparse((p.scheme, p.netloc, m.group("prefix"), "", "", ""))
        rec = arts.get(aid)
        if rec is None:
            rec = arts[aid] = {"id": aid, "prefix": prefix, "title": "", "pdf_urls": [], "other_urls": []}
            order.append(aid)
        text = a.get_text(" ", strip=True)
        gal = m.group("gal")
        if gal:
            dl = f"{prefix}/download/{aid}/{gal}"
            label = (text + " " + " ".join(a.get("class") or [])).lower()
            bucket = rec["pdf_urls"] if "pdf" in label else rec["other_urls"]
            if dl not in bucket:
                bucket.append(dl)
        elif not rec["title"] and len(text) >= 3 and not GENERIC_LABEL.match(text):
            rec["title"] = text
    out = []
    for aid in order[:MAX_ARTICLES]:
        r = arts[aid]
        r["view_url"] = f"{r['prefix']}/view/{aid}"
        out.append(r)
    return issue_title, out


# ───────────────────────── email extraction & filtration ─────────────────────────
ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
DASHES = {0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2212: "-"}
AT_RE = re.compile(r"\s*[\[\(\{]\s*(?:at|@)\s*[\]\)\}]\s*", re.I)
DOT_RE = re.compile(r"\s*[\[\(\{]\s*(?:dot|\.)\s*[\]\)\}]\s*", re.I)
EMAIL_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63})@((?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})")
BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "json", "xml", "html", "htm", "php", "pdf",
            "doc", "docx", "zip", "txt"}
LONG_TLDS = {"info", "online", "tech", "site", "xyz", "academy", "science", "email", "institute", "education",
             "museum", "travel", "network", "global", "company", "community"}

# role / administrative local-parts (compared after removing digits and separators)
BLOCKED_PREFIXES = {
    "info", "information", "support", "editor", "editors", "editorial", "editorialoffice", "editorinchief", "eic",
    "chiefeditor", "managingeditor", "admin", "administrator", "administration", "queries", "query", "webmaster",
    "postmaster", "noreply", "norepl", "donotreply", "contact", "contactus", "office", "journal", "journals",
    "submissions", "submission", "help", "helpdesk", "publisher", "publication", "publications", "press", "media",
    "enquiries", "enquiry", "inquiries", "inquiry", "secretary", "secretariat", "ojs", "feedback", "service",
    "services", "sales", "marketing", "mail", "hello", "team", "peerreview", "reviewers", "review", "permissions",
    "copyright", "privacy", "abuse", "billing", "subscriptions", "subscription", "customerservice", "membership"}
# third-party / boilerplate domains that are never an author's address
BLOCKED_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "email.com", "sentry.io", "w3.org",
                   "pkp.sfu.ca", "sfu.ca", "publicknowledge.ca", "crossref.org", "orcid.org", "creativecommons.org",
                   "wordpress.org", "wixpress.com"}
SLDS = {"ac", "co", "com", "edu", "gov", "net", "org", "or", "sch", "go", "ne"}


def base_domain(host):
    """jaat.fudutsinma.edu.ng -> fudutsinma.edu.ng ; www.mdpi.com -> mdpi.com"""
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in SLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def repair_lines(text):
    """Re-join emails that a PDF broke across lines (after '@', before '@', after '.' or '-')."""
    lines = [l.rstrip() for l in text.split("\n")]
    out, i = [], 0
    while i < len(lines):
        cur = lines[i]
        while i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            lt = (cur.split() or [""])[-1]
            ft = (nxt.split() or [""])[0]
            if not lt or not ft:
                break
            join = False
            if "@" in lt:
                dom = lt.split("@", 1)[1]
                join = (lt.endswith(("@", ".", "-")) or "." not in dom) and re.match(r"^[A-Za-z0-9]", ft) is not None
            elif ft.startswith("@") and re.fullmatch(r"[A-Za-z0-9._%+\-]+", lt):
                join = True
            elif re.fullmatch(r"[A-Za-z0-9._%+\-]{2,}[-.]", lt) and re.match(r"^[A-Za-z0-9._%+\-]*@", ft):
                join = True
            if not join:
                break
            cur += nxt
            i += 1
        out.append(cur)
        i += 1
    text = "\n".join(out)
    return re.sub(r"([A-Za-z0-9._%+\-])[ \t]?@[ \t]?(?=[A-Za-z0-9])", r"\1@", text)


def clean_match(local, domain):
    local = local.strip(".-_+%")
    if not local or ".." in local:
        return None
    labels = domain.strip(".").split(".")
    last = labels[-1]
    for i in range(2, len(last)):                      # "gmail.comReceived" -> "com"
        if last[i].isupper() and last[i - 1].islower():
            last = last[:i]
            break
    low = last.lower()
    if len(low) > 3 and low not in LONG_TLDS and low[:3] in ("com", "org", "net", "edu", "gov"):
        low = low[:3]                                  # "gmail.comabstract" -> "com"
    if len(low) < 2 or low in BAD_TLDS:
        return None
    return f"{local.lower()}@{'.'.join(l.lower() for l in labels[:-1])}.{low}"


def find_emails(text):
    text = unicodedata.normalize("NFKC", text or "").translate(ZW).translate(DASHES)
    text = DOT_RE.sub(".", AT_RE.sub("@", text))
    text = repair_lines(text)
    found = []
    for m in EMAIL_RE.finditer(text):
        e = clean_match(m.group(1), m.group(2))
        if e and e not in found:
            found.append(e)
    return found


def is_allowed(email, journal_base):
    """Strict filtration: no role/admin prefixes, no journal-host domain, no boilerplate domains."""
    local, _, domain = email.partition("@")
    if domain == journal_base or domain.endswith("." + journal_base):
        return False
    if domain in BLOCKED_DOMAINS or any(domain.endswith("." + d) for d in BLOCKED_DOMAINS):
        return False
    norm = re.sub(r"[^a-z]", "", local)
    first = re.sub(r"\d+$", "", re.split(r"[._\-+]+", local)[0])
    return norm not in BLOCKED_PREFIXES and first not in BLOCKED_PREFIXES


# ───────────────────────────── PDF handling ─────────────────────────────
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


def page_info(html_text, base_url):
    """From an OJS article landing page: citation_title and PDF galley candidates."""
    soup = BeautifulSoup(html_text, "html.parser")
    title = None
    m = soup.find("meta", attrs={"name": "citation_title"})
    if m and m.get("content"):
        title = m["content"].strip()
    cands = []

    def add(u):
        u = urljoin(base_url, u.strip())
        mm = ART_RE.match(urlparse(u).path)
        if mm and mm.group("gal") and "/download/" not in u:   # galley viewer -> direct download
            u = f"{urlunparse(urlparse(u)[:2] + (mm.group('prefix'), '', '', ''))}/download/{mm.group('id')}/{mm.group('gal')}"
        if u not in cands:
            cands.append(u)

    for m in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
        if m.get("content"):
            add(m["content"])
    for a in soup.find_all("a", href=True):
        label = (a.get_text(" ", strip=True) + " " + " ".join(a.get("class") or [])).lower()
        if re.search(r"\bpdf\b", label):
            add(a["href"])
    return title, cands


def viewer_pdf_link(html_text, base_url):
    """A galley URL sometimes returns an HTML viewer; find the real PDF inside it."""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag, attr in (("iframe", "src"), ("embed", "src"), ("object", "data")):
        el = soup.find(tag)
        if el and el.get(attr):
            return urljoin(base_url, el[attr])
    m = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if m and m.get("content"):
        return urljoin(base_url, m["content"])
    a = soup.find("a", href=re.compile(r"/article/download/\d+/\d+|\.pdf(?:$|\?)", re.I))
    return urljoin(base_url, a["href"]) if a else None


def fetch_pdf(session, url):
    """Download a galley and return PDF bytes (follows one level of HTML viewer)."""
    final, ctype, body = safe_get(session, url, MAX_PDF_BYTES, accept="application/pdf,*/*;q=0.8")
    if body[:1024].find(b"%PDF-") != -1:
        return body
    nxt = viewer_pdf_link(decode_html(body, ctype), final)
    if nxt and nxt != url:
        _, _, body = safe_get(session, nxt, MAX_PDF_BYTES, accept="application/pdf,*/*;q=0.8")
        if body[:1024].find(b"%PDF-") != -1:
            return body
    raise ValueError("Galley is not a PDF file.")


def process_article(session, art, journal_base):
    """Fill art['emails'], art['status'], art['note']. Never raises."""
    art["emails"], art["status"], art["note"] = [], "error", ""
    candidates = list(art["pdf_urls"])
    page_loaded = False
    errors = []

    def load_page():
        nonlocal page_loaded
        page_loaded = True
        try:
            final, ctype, body = safe_get(session, art["view_url"], MAX_HTML_BYTES, truncate=True,
                                          accept="text/html,*/*;q=0.8")
            title, cands = page_info(decode_html(body, ctype), final)
            if title:
                art["title"] = title
            for c in cands:
                if c not in candidates:
                    candidates.append(c)
        except Exception as e:
            errors.append(f"article page: {e}")

    if not candidates or not art["title"]:
        load_page()
    candidates.extend(u for u in art["other_urls"] if u not in candidates)

    tried, data = set(), None
    while data is None:
        pending = [c for c in candidates if c not in tried][:3]
        if not pending:
            if not page_loaded:
                load_page()
                continue
            break
        for url in pending:
            tried.add(url)
            try:
                data = fetch_pdf(session, url)
                art["pdf_url"] = url
                break
            except Exception as e:
                errors.append(f"{url.rsplit('/article/', 1)[-1]}: {e}")
    if data is None:
        art["note"] = ("PDF not available. " + "; ".join(errors[:2]))[:300]
        return
    try:
        text = pdf_first_pages_text(data)
    except Exception as e:
        art["note"] = f"Could not read PDF: {type(e).__name__}"
        return
    if not text.strip():
        art["status"], art["note"] = "no_text", "PDF has no extractable text (scanned image?)."
        return
    art["emails"] = [e for e in find_emails(text) if is_allowed(e, journal_base)]
    art["status"] = "ok" if art["emails"] else "no_emails"
    if not art["emails"]:
        art["note"] = "No author email found on the first pages."


# ───────────────────────────── background jobs ─────────────────────────────
def new_job(url):
    job = {"id": uuid.uuid4().hex, "url": url, "status": "queued", "issue_title": "", "total": 0, "processed": 0,
           "articles": [], "error": None, "created": time.time()}
    with JOBS_LOCK:
        for jid in [k for k, v in JOBS.items() if time.time() - v["created"] > 3600]:
            JOBS.pop(jid, None)
        if sum(1 for v in JOBS.values() if v["status"] in ("queued", "discovering", "running")) >= MAX_ACTIVE_JOBS:
            return None
        JOBS[job["id"]] = job
    return job


def run_job(job):
    try:
        session = make_session()
        job["status"] = "discovering"
        final, ctype, body = safe_get(session, job["url"], MAX_HTML_BYTES, truncate=True, accept="text/html,*/*;q=0.8")
        title, arts = discover_articles(decode_html(body, ctype), final)
        if not arts:
            raise ValueError("No articles found. Use a direct issue URL such as .../index.php/<journal>/issue/view/<id>.")
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


def public_view(job):
    emails, seen = [], set()
    arts = []
    for a in job["articles"]:
        for e in a["emails"]:
            if e not in seen:
                seen.add(e)
                emails.append(e)
        arts.append({"id": a["id"], "title": a["title"] or f"Article {a['id']}", "url": a["view_url"],
                     "emails": a["emails"], "status": a["status"], "note": a["note"]})
    return {"status": job["status"], "error": job["error"], "issue_title": job["issue_title"],
            "total": job["total"], "processed": job["processed"], "emails": emails, "articles": arts,
            "journal_domain": job.get("journal_domain", "")}


# ───────────────────────────── routes ─────────────────────────────
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
    if job is None:
        return jsonify({"error": "Server is busy with other scans. Try again in a minute."}), 429
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify({"job_id": job["id"]})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    return jsonify(public_view(job))


# ───────────────────────────── embedded frontend ─────────────────────────────
PAGE = r"""{% raw %}<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OJS Issue Email Scraper</title>
<style>
:root{--bg:#eef1f6;--card:#fff;--ink:#14202e;--mut:#5b6b7e;--line:#d9e0ea;--acc:#1f5fbf;--accfg:#fff;--ok:#157347;--bad:#b3261e;--chip:#e6eefb}
@media (prefers-color-scheme:dark){:root{--bg:#0e141c;--card:#161e29;--ink:#e6ecf4;--mut:#93a3b8;--line:#2a3646;--acc:#6ea4ff;--accfg:#0b1320;--ok:#5fd08f;--bad:#ff8f86;--chip:#1f2c42}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:32px 18px 64px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--mut);margin:0 0 22px}
.bar{display:flex;gap:10px}
input[type=text]{flex:1;padding:12px 14px;font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:10px}
button{font:inherit;padding:12px 18px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:var(--accfg);font-weight:600}
button:disabled{opacity:.6;cursor:default}
input:focus-visible,button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
#status{margin:14px 0 6px;color:var(--mut);min-height:22px}
#status.err{color:var(--bad)}
.prog{height:6px;background:var(--line);border-radius:4px;overflow:hidden;visibility:hidden}
.prog i{display:block;height:100%;width:0;background:var(--acc);transition:width .25s}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin:20px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.stat b{display:block;font-size:34px;line-height:1.1;font-variant-numeric:tabular-nums}
.stat span{color:var(--mut);font-size:13px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
.panel h2{font-size:17px;margin:0}
.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
ol.emails{margin:0;padding-left:34px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px;max-height:340px;overflow:auto}
ol.emails li{padding:2px 0;word-break:break-all}
.empty{color:var(--mut)}
.art{padding:12px 0;border-top:1px solid var(--line)}
.art:first-of-type{border-top:0}
.art .t{font-weight:600}.art .t a{color:inherit;text-decoration:none}.art .t a:hover{text-decoration:underline}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:2px 8px;margin:6px 6px 0 0;font:13px ui-monospace,Menlo,Consolas,monospace;word-break:break-all}
.note{color:var(--mut);font-size:13px;margin-top:4px}
.note.bad{color:var(--bad)}
@media(max-width:560px){.bar{flex-direction:column}}
</style></head><body><div class="wrap">
<h1>OJS issue email scraper</h1>
<p class="sub">Paste an OJS issue link. Each unique article's PDF is opened and its first pages are read for author emails.</p>
<div class="bar"><input id="url" type="text" placeholder="https://jaat.fudutsinma.edu.ng/index.php/jaat/issue/view/19" aria-label="OJS issue URL" autocomplete="off">
<button id="go" class="primary">Scrape issue</button></div>
<div id="status"></div><div class="prog" id="prog"><i id="pb"></i></div>
<div class="stats">
 <div class="stat"><b id="nArt">0</b><span>Total unique articles found</span></div>
 <div class="stat"><b id="nMail">0</b><span>Total unique author emails</span></div>
</div>
<div class="panel"><div class="head"><h2>Author emails</h2><button id="copy" disabled>Copy all</button></div>
<ol class="emails" id="emails"></ol><div class="empty" id="emptyMail">No emails yet.</div></div>
<div class="panel"><div class="head"><h2>Article breakdown</h2></div><div id="arts"><div class="empty">Nothing scraped yet.</div></div></div>
</div>
<script>
const $ = s => document.querySelector(s);
let timer = null, current = [];
function status(t, err){ const s = $('#status'); s.textContent = t; s.className = err ? 'err' : ''; }
function busy(b){ $('#go').disabled = b; $('#prog').style.visibility = b ? 'visible' : 'hidden'; }
function el(tag, cls, text){ const e = document.createElement(tag); if(cls) e.className = cls; if(text !== undefined) e.textContent = text; return e; }

function render(s){
  $('#nArt').textContent = s.total;
  $('#nMail').textContent = s.emails.length;
  $('#pb').style.width = s.total ? (100 * s.processed / s.total) + '%' : '0';
  current = s.emails;
  const ol = $('#emails'); ol.textContent = '';
  s.emails.forEach(e => ol.appendChild(el('li', '', e)));
  $('#emptyMail').style.display = s.emails.length ? 'none' : 'block';
  $('#copy').disabled = !s.emails.length;
  const box = $('#arts'); box.textContent = '';
  s.articles.forEach((a, i) => {
    const d = el('div', 'art'), t = el('div', 't');
    const l = el('a', '', (i + 1) + '. ' + a.title); l.href = a.url; l.target = '_blank'; l.rel = 'noopener';
    t.appendChild(l); d.appendChild(t);
    a.emails.forEach(e => d.appendChild(el('span', 'chip', e)));
    if(a.status === 'pending') d.appendChild(el('div', 'note', 'Processing…'));
    else if(a.note) d.appendChild(el('div', 'note' + (a.status === 'error' ? ' bad' : ''), a.note));
    box.appendChild(d);
  });
}

async function poll(id){
  try {
    const r = await fetch('/api/status/' + id); const s = await r.json();
    if(!r.ok){ status(s.error || 'Lost the job.', true); busy(false); return; }
    render(s);
    if(s.status === 'error'){ status(s.error, true); busy(false); return; }
    if(s.status === 'done'){
      status('Done: ' + s.total + ' articles, ' + s.emails.length + ' unique emails' + (s.journal_domain ? ' (excluded @' + s.journal_domain + ')' : '') + '.');
      busy(false); return;
    }
    status(s.status === 'discovering' ? 'Reading the issue page…' : 'Reading PDFs: ' + s.processed + ' of ' + s.total);
  } catch(e){ status('Connection hiccup, retrying…'); }
  timer = setTimeout(() => poll(id), 1200);
}

async function start(){
  const url = $('#url').value.trim();
  if(!url){ status('Enter an OJS issue URL first.', true); return; }
  clearTimeout(timer); busy(true); status('Starting…');
  render({total:0, processed:0, emails:[], articles:[]});
  try {
    const r = await fetch('/api/scrape', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
    const d = await r.json();
    if(!r.ok){ status(d.error || 'Request failed.', true); busy(false); return; }
    poll(d.job_id);
  } catch(e){ status('Could not reach the server.', true); busy(false); }
}

async function copyAll(){
  const text = current.join('\n');
  try { await navigator.clipboard.writeText(text); }
  catch(e){ const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  $('#copy').textContent = 'Copied'; setTimeout(() => $('#copy').textContent = 'Copy all', 1500);
}

$('#go').addEventListener('click', start);
$('#url').addEventListener('keydown', e => { if(e.key === 'Enter') start(); });
$('#copy').addEventListener('click', copyAll);
</script></body></html>{% endraw %}"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
