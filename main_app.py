"""
Academic Journal Email Scraper & Enterprise Engine
==================================================
Single-file Flask application with:
- Issue / table-of-contents discovery
- Async article processing with aiohttp
- HTML, metadata, Cloudflare and PDF email extraction
- SSRF protection and redirect validation
- robots.txt, per-host concurrency and retry backoff
- Server-Sent Events (SSE) with heartbeats
- JSONL result persistence, resumable jobs, and Web UI Dashboard

Python: 3.11+
Install:
    pip install Flask aiohttp beautifulsoup4 PyPDF2 gunicorn
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

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import aiohttp
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context
from PyPDF2 import PdfReader


# ---------------------------------------------------------------------------
# Configuration & Security Setup
# ---------------------------------------------------------------------------

APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = 64 * 1024

DATA_DIR = Path(os.environ.get("SCRAPER_DATA_DIR", "./scraper_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

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
    "privacy", "copyright", "press", "jobs", "careers",
    "billing", "accounts", "feedback", "postmaster",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
]

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "ref_src",
    "mc_cid", "mc_eid", "igshid", "_ga", "_gl",
}

ARTICLE_HINTS = re.compile(
    r"(article|view|paper|publication|full.?text|abstract|"
    r"download|document|doi|research|contribution|"
    r"article/view|article/download|issue/view)",
    re.I,
)

PDF_HINTS = re.compile(r"\.pdf(?:$|[?#])", re.I)

EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])"
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+",
    re.I,
)

CLOUDFLARE_RE = re.compile(
    r'data-cfemail\s*=\s*["\']([0-9a-fA-F]+)["\']',
    re.I,
)

HOST_SEMAPHORES = {}
ROBOTS_CACHE = {}
HOST_FAILURES = defaultdict(int)
STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Logging and Security Filters
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("academic-scraper")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def security_event(kind, url=None, detail=None):
    entry = {
        "time": utc_now(),
        "event": str(kind)[:100],
        "host": (urlsplit(url).hostname or "")[:255] if url else None,
        "detail": str(detail or "")[:500],
    }
    LOG.warning("Security event: %s", entry)

    try:
        with open(SECURITY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        LOG.exception("Could not persist security event")


def sse_event(event, data):
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


@APP.before_request
def detect_suspicious_activity():
    query_str = request.query_string.decode('utf-8', errors='ignore').lower()
    suspicious_patterns = ['select', 'union', 'drop', '<script', '../', 'etc/passwd', 'eval(']
    for pattern in suspicious_patterns:
        if pattern in query_str:
            client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            user_agent = request.headers.get('User-Agent', 'Unknown')
            security_event("suspicious_query_pattern", detail=f"IP: {client_ip} | Query: {query_str}")
            break


@APP.after_request
def apply_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Server'] = 'Secure-Engine-20X'
    return response


# ---------------------------------------------------------------------------
# URL normalization and SSRF defense
# ---------------------------------------------------------------------------

def normalize_url(raw):
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError("A valid URL of at most 4096 characters is required.")

    raw = raw.strip()
    if not raw:
        raise ValueError("URL is required.")

    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS URLs are accepted.")

    if not parts.hostname:
        raise ValueError("URL must contain a hostname.")

    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs containing credentials are not accepted.")

    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid URL port.") from exc

    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid URL port.")

    host = parts.hostname.rstrip(".").lower()
    if not host or "%" in host or "\\" in host:
        raise ValueError("Invalid hostname.")

    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("Localhost destinations are blocked.")

    if re.fullmatch(r"[0-9.]+", host):
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Ambiguous numeric hostnames are blocked.") from exc

    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid internationalized hostname.") from exc

    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"

    filtered = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in TRACKING_PARAMS:
            continue
        filtered.append((key, value))

    return urlunsplit((
        parts.scheme.lower(),
        netloc,
        parts.path or "/",
        urlencode(filtered, doseq=True),
        "",
    ))


def _is_public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False

    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


async def validate_public_destination(url):
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)

    if not host:
        raise ValueError("Missing destination hostname.")

    try:
        literal_ip = ipaddress.ip_address(host)
        addresses = [str(literal_ip)]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            results = await loop.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError("Destination hostname could not be resolved.") from exc

        addresses = list({
            result[4][0]
            for result in results
            if result and result[4]
        })

    if not addresses:
        raise ValueError("Destination hostname resolved to no addresses.")

    for address in addresses:
        if not _is_public_ip(address):
            security_event("ssrf_block", url, f"Non-public address: {address}")
            raise ValueError("Private or non-public network destinations are blocked.")

    return True


# ---------------------------------------------------------------------------
# Email de-obfuscation
# ---------------------------------------------------------------------------

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

    text = str(text)

    text = (
        text.replace("&commat;", "@")
        .replace("&#64;", "@")
        .replace("&#x40;", "@")
        .replace("&period;", ".")
        .replace("&#46;", ".")
        .replace("&#x2e;", ".")
        .replace("&nbsp;", " ")
    )

    text = re.sub(r"[\u00ad\u200b-\u200f\u2060\ufeff]", "", text)

    text = re.sub(r"\s*(?:\[|\(|\{)\s*at\s*(?:\]|\)|\})\s*", "@", text, flags=re.I)
    text = re.sub(r"\s+(?:at|AT)\s+", "@", text)
    text = re.sub(r"\s*(?:\[|\(|\{)\s*dot\s*(?:\]|\)|\})\s*", ".", text, flags=re.I)
    text = re.sub(r"\s+(?:dot|DOT)\s+", ".", text)

    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = re.sub(r"(?<=\w)\s*\n\s*(?=[@.])", "", text)
    text = re.sub(r"(?<=[@.])\s*\n\s*(?=\w)", "", text)

    text = re.sub(r"([A-Z0-9._%+-]+)\s*@\s*([A-Z0-9.-]+)",
                  r"\1@\2", text, flags=re.I)

    return text


def normalize_email(email):
    email = email.strip().strip(".,;:<>[](){}'\"")
    email = email.replace("mailto:", "")
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        return None

    local, domain = email.rsplit("@", 1)
    if len(local) > 64 or len(domain) > 253:
        return None

    return f"{local}@{domain.lower()}"


def extract_emails(text):
    text = clean_email_text(text)
    found = {}

    for match in EMAIL_PATTERN.finditer(text):
        email = normalize_email(match.group(0))
        if email:
            found[email.casefold()] = email

    return sorted(found.values(), key=str.casefold)


def classify_emails(emails):
    authors, generic = [], []

    for email in emails:
        local = email.split("@", 1)[0].lower()
        normalized_local = re.sub(r"[._+-]+", "-", local)

        if (
            normalized_local in GENERIC_PREFIXES
            or any(normalized_local.startswith(p + "-") for p in GENERIC_PREFIXES)
        ):
            generic.append(email)
        else:
            authors.append(email)

    return {
        "author_emails": authors,
        "generic_emails": generic,
    }


# ---------------------------------------------------------------------------
# HTML extraction and article discovery
# ---------------------------------------------------------------------------

def decode_cfemail_elements(soup):
    for element in soup.select("[data-cfemail]"):
        encoded = element.get("data-cfemail", "")
        decoded = decode_cloudflare(encoded)
        if decoded:
            element.append(" " + decoded)

    for element in soup.select('a[href^="/cdn-cgi/l/email-protection"]'):
        encoded = element.get("data-cfemail", "")
        decoded = decode_cloudflare(encoded)
        if decoded:
            element.append(" " + decoded)


def extract_html_emails(html):
    soup = BeautifulSoup(html, "html.parser")
    decode_cfemail_elements(soup)

    pieces = []

    for tag in soup.find_all(True):
        for key in ("content", "value", "href", "data-email", "aria-label"):
            value = tag.get(key)
            if isinstance(value, str):
                if value.lower().startswith("mailto:"):
                    value = value[7:].split("?", 1)[0]
                pieces.append(value)

    pieces.append(soup.get_text(" ", strip=True))
    pieces.append(html)

    return extract_emails("\n".join(pieces))


def is_same_site(url_a, url_b):
    try:
        return (urlsplit(url_a).hostname or "").lower() == (
            urlsplit(url_b).hostname or ""
        ).lower()
    except ValueError:
        return False


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
        except ValueError:
            continue

        if not is_same_site(issue_url, normalized):
            continue

        if normalized == normalize_url(issue_url):
            continue

        if PDF_HINTS.search(normalized):
            continue

        label = anchor.get_text(" ", strip=True)
        context = " ".join([
            label,
            str(anchor.get("class", "")),
            str(anchor.get("id", "")),
            str(anchor.parent.get("class", "") if anchor.parent else ""),
            normalized,
        ])

        parent = anchor.find_parent(
            class_=re.compile(r"(article|obj_article|title|submission|paper)", re.I)
        )

        likely = bool(ARTICLE_HINTS.search(context) or parent)

        if not likely:
            continue

        if normalized not in seen:
            seen.add(normalized)
            discovered.append({
                "url": normalized,
                "title": label[:500] or normalized,
            })

    return discovered


def extract_pdf_links(page_url, html):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        label = anchor.get_text(" ", strip=True)
        absolute = urljoin(page_url, href)

        if not (
            PDF_HINTS.search(absolute)
            or (
                re.search(r"(pdf|download|full.?text)", label, re.I)
                and re.search(r"(pdf|download)", absolute, re.I)
            )
        ):
            continue

        try:
            normalized = normalize_url(absolute)
        except ValueError:
            continue

        if normalized not in seen:
            seen.add(normalized)
            results.append(normalized)

    for tag in soup.find_all("meta"):
        if tag.get("name", "").lower() == "citation_pdf_url":
            value = tag.get("content", "").strip()
            if value:
                try:
                    normalized = normalize_url(urljoin(page_url, value))
                    if normalized not in seen:
                        seen.add(normalized)
                        results.append(normalized)
                except ValueError:
                    pass

    return results


# ---------------------------------------------------------------------------
# HTTP client, robots.txt, redirects and retries (Scoped Semaphores)
# ---------------------------------------------------------------------------

def get_host_semaphore(host):
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, host)
    with STATE_LOCK:
        if key not in HOST_SEMAPHORES:
            HOST_SEMAPHORES[key] = asyncio.Semaphore(PER_HOST_CONCURRENCY)
        return HOST_SEMAPHORES[key]


def robots_allowed(url):
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"

    entry = ROBOTS_CACHE.get(origin)
    if not entry or time.time() - entry["time"] > ROBOTS_TTL:
        return None

    rules = entry.get("rules", [])
    path = parts.path or "/"

    matching = []
    for rule, allowed in rules:
        if rule and path.startswith(rule):
            matching.append((len(rule), allowed))

    if not matching:
        return True

    matching.sort(key=lambda x: x[0], reverse=True)
    return matching[0][1]


async def load_robots(session, url):
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    robots_url = origin + "/robots.txt"

    cached = ROBOTS_CACHE.get(origin)
    if cached and time.time() - cached["time"] <= ROBOTS_TTL:
        return cached

    rules = []
    crawl_delay = None

    try:
        response = await safe_request(
            session,
            robots_url,
            max_bytes=512 * 1024,
            respect_robots=False,
            max_retries=1,
        )

        if response["status"] == 200:
            text = response["body"].decode("utf-8", "replace")
            active = False

            for raw_line in text.splitlines():
                line = raw_line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue

                key, value = [x.strip() for x in line.split(":", 1)]
                key = key.lower()

                if key == "user-agent":
                    active = value == "*" or "Mozilla" in value

                elif active and key == "disallow" and value:
                    rules.append((value, False))

                elif active and key == "allow" and value:
                    rules.append((value, True))

                elif active and key == "crawl-delay":
                    try:
                        crawl_delay = max(0.0, min(float(value), 30.0))
                    except ValueError:
                        pass

    except Exception as exc:
        LOG.info("robots.txt unavailable for %s: %s", origin, exc)

    cached = {
        "time": time.time(),
        "rules": rules,
        "crawl_delay": crawl_delay,
    }
    ROBOTS_CACHE[origin] = cached
    return cached


async def safe_request(
    session,
    url,
    *,
    max_bytes=MAX_HTML_BYTES,
    respect_robots=True,
    max_retries=3,
):
    url = normalize_url(url)

    for attempt in range(max_retries + 1):
        current_url = url
        try:
            for redirect_number in range(MAX_REDIRECTS + 1):
                await validate_public_destination(current_url)

                if respect_robots:
                    policy = await load_robots(session, current_url)
                    decision = robots_allowed(current_url)

                    if decision is False:
                        raise PermissionError("Blocked by robots.txt")

                    if policy.get("crawl_delay"):
                        await asyncio.sleep(policy["crawl_delay"])

                host = urlsplit(current_url).hostname or ""
                semaphore = get_host_semaphore(host)

                async with semaphore:
                    headers = {
                        "User-Agent": random.choice(USER_AGENTS),
                        "Accept": (
                            "text/html,application/xhtml+xml,application/pdf,"
                            "application/json;q=0.8,*/*;q=0.5"
                        ),
                    }

                    async with session.get(
                        current_url,
                        headers=headers,
                        allow_redirects=False,
                    ) as response:
                        status = response.status

                        if status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                return {
                                    "url": current_url,
                                    "status": status,
                                    "headers": dict(response.headers),
                                    "body": b"",
                                }

                            if redirect_number >= MAX_REDIRECTS:
                                raise ValueError("Too many redirects.")

                            current_url = normalize_url(urljoin(current_url, location))
                            continue

                        if status in {403, 429, 500, 502, 503, 504}:
                            HOST_FAILURES[host] += 1

                            if status in {403, 429} or HOST_FAILURES[host] >= 5:
                                security_event(
                                    "repeated_http_failure",
                                    current_url,
                                    f"status={status}; failures={HOST_FAILURES[host]}",
                                )

                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > max_bytes:
                                raise ValueError("Response exceeded configured size limit.")

                        if status < 400:
                            HOST_FAILURES[host] = 0

                        return {
                            "url": current_url,
                            "status": status,
                            "headers": dict(response.headers),
                            "body": bytes(body),
                        }

            raise ValueError("Redirect limit reached.")

        except PermissionError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            if attempt >= max_retries:
                raise

            delay = min(2 ** attempt, 20) + random.uniform(0.1, 1.0)
            LOG.info(
                "Request retry %s/%s for %s: %s",
                attempt + 1,
                max_retries,
                current_url,
                exc,
            )
            await asyncio.sleep(delay)

    raise RuntimeError("Request failed.")


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------

def extract_pdf_emails(pdf_bytes):
    import io

    found = set()
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)

        if reader.is_encrypted:
            try:
                result = reader.decrypt("")
                if not result:
                    return []
            except Exception:
                return []

        for page in reader.pages[:10]:
            try:
                text = page.extract_text() or ""
                found.update(extract_emails(text))
            except Exception as exc:
                LOG.debug("PDF page extraction failed: %s", exc)

        try:
            metadata = reader.metadata
            if metadata:
                for value in metadata.values():
                    if value:
                        found.update(extract_emails(str(value)))
        except Exception:
            pass

    except Exception as exc:
        LOG.info("PDF parsing failed: %s", exc)

    return sorted(found, key=str.casefold)


# ---------------------------------------------------------------------------
# Article processing
# ---------------------------------------------------------------------------

async def fetch_issue(session, issue_url):
    result = await safe_request(session, issue_url)

    if result["status"] >= 400:
        raise RuntimeError(f"Issue page returned HTTP {result['status']}")

    content_type = result["headers"].get("Content-Type", "").lower()
    if "html" not in content_type and not result["body"].lstrip().startswith(b"<"):
        raise ValueError("The supplied issue URL does not appear to be an HTML page.")

    html = result["body"].decode("utf-8", "replace")
    links = discover_issue_links(result["url"], html)

    if not links:
        raise ValueError(
            "No article links were discovered. The issue may require JavaScript "
            "rendering or a publisher-specific adapter."
        )

    return result["url"], html, links


async def process_article(session, article):
    article_url = article["url"]

    record = {
        "url": article_url,
        "title": article.get("title", ""),
        "status": "ok",
        "author_emails": [],
        "generic_emails": [],
        "all_emails": [],
        "pdf_urls": [],
        "errors": [],
    }

    try:
        response = await safe_request(session, article_url)

        if response["status"] >= 400:
            raise RuntimeError(f"Article returned HTTP {response['status']}")

        content_type = response["headers"].get("Content-Type", "").lower()

        if "pdf" in content_type or response["body"].startswith(b"%PDF-"):
            emails = extract_pdf_emails(response["body"])
            record["pdf_urls"].append(response["url"])
            record["all_emails"] = emails
        else:
            html = response["body"].decode("utf-8", "replace")
            soup = BeautifulSoup(html, "html.parser")

            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            if title:
                record["title"] = title[:500]

            html_emails = set(extract_html_emails(html))
            record["pdf_urls"] = extract_pdf_links(response["url"], html)

            pdf_results = await asyncio.gather(
                *[
                    fetch_pdf_emails(session, pdf_url)
                    for pdf_url in record["pdf_urls"]
                ],
                return_exceptions=True,
            )

            all_emails = set(html_emails)

            for pdf_url, pdf_result in zip(record["pdf_urls"], pdf_results):
                if isinstance(pdf_result, Exception):
                    record["errors"].append(
                        f"PDF {pdf_url}: {str(pdf_result)[:250]}"
                    )
                else:
                    all_emails.update(pdf_result)

            record["all_emails"] = sorted(all_emails, key=str.casefold)

    except Exception as exc:
        record["status"] = "error"
        record["errors"].append(str(exc)[:500])

        if isinstance(exc, (PermissionError, ValueError)):
            security_event("article_processing_error", article_url, str(exc))

    classified = classify_emails(record["all_emails"])
    record.update(classified)
    record["processed_at"] = utc_now()

    return record


async def fetch_pdf_emails(session, pdf_url):
    response = await safe_request(
        session,
        pdf_url,
        max_bytes=MAX_PDF_BYTES,
    )

    if response["status"] >= 400:
        raise RuntimeError(f"PDF returned HTTP {response['status']}")

    if not (
        "pdf" in response["headers"].get("Content-Type", "").lower()
        or response["body"].startswith(b"%PDF-")
    ):
        raise ValueError("Linked document did not return a PDF.")

    return extract_pdf_emails(response["body"])


# ---------------------------------------------------------------------------
# Job persistence and asynchronous execution
# ---------------------------------------------------------------------------

def job_path(job_id):
    if not re.fullmatch(r"[a-f0-9-]{36}", job_id):
        raise ValueError("Invalid jobId.")
    return RESULTS_DIR / f"{job_id}.jsonl"


def append_job_record(job_id, record):
    path = job_path(job_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_job_records(job_id):
    path = job_path(job_id)
    if not path.exists():
        return []

    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return records


async def run_job(job_id, issue_url, skip, output_queue):
    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT,
        connect=10,
        sock_read=REQUEST_TIMEOUT,
    )

    connector = aiohttp.TCPConnector(
        limit=GLOBAL_CONCURRENCY,
        limit_per_host=PER_HOST_CONCURRENCY,
        ttl_dns_cache=0,
        use_dns_cache=False,
    )

    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            trust_env=False,
        ) as session:
            resolved_issue_url, _, articles = await fetch_issue(session, issue_url)

            if len(articles) > MAX_ARTICLES:
                articles = articles[:MAX_ARTICLES]

            start = max(0, min(skip, len(articles)))

            output_queue.put({
                "type": "start",
                "jobId": job_id,
                "issueUrl": resolved_issue_url,
                "total": len(articles),
                "skip": start,
            })

            semaphore = asyncio.Semaphore(GLOBAL_CONCURRENCY)
            counter_lock = asyncio.Lock()
            completed = 0

            async def handle(index, article):
                nonlocal completed

                async with semaphore:
                    record = await process_article(session, article)

                record["jobId"] = job_id
                record["index"] = index

                append_job_record(job_id, record)

                async with counter_lock:
                    completed += 1
                    count = completed

                output_queue.put({
                    "type": "article",
                    "jobId": job_id,
                    "index": index,
                    "completedThisRun": count,
                    "totalThisRun": len(articles) - start,
                    "result": record,
                })

            tasks = [
                asyncio.create_task(handle(index, articles[index]))
                for index in range(start, len(articles))
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            errors = 0
            for result in results:
                if isinstance(result, Exception):
                    errors += 1
                    LOG.exception(
                        "Unexpected article task failure",
                        exc_info=result,
                    )

            output_queue.put({
                "type": "complete",
                "jobId": job_id,
                "totalDiscovered": len(articles),
                "processedThisRun": completed,
                "unexpectedErrors": errors,
                "nextSkip": len(articles),
            })

    except Exception as exc:
        security_event("job_failed", issue_url, str(exc))
        output_queue.put({
            "type": "error",
            "jobId": job_id,
            "error": str(exc)[:1000],
        })

    finally:
        output_queue.put(None)


# ---------------------------------------------------------------------------
# Flask Web UI & API Endpoints
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Enterprise Academic Journal & Email Scraper</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #090d16; color: #e2e8f0; margin: 0; padding: 20px; }
        .container { max-width: 900px; margin: auto; background: #111827; padding: 30px; border-radius: 12px; border: 1px solid #1f2937; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
        h2 { text-align: center; color: #38bdf8; font-size: 24px; margin-bottom: 20px; }
        input[type="url"] { width: 100%; padding: 14px; border-radius: 8px; border: 1px solid #374151; background: #030712; color: #fff; box-sizing: border-box; font-size: 16px; margin-bottom: 15px; }
        button { width: 100%; padding: 14px; background: #2563eb; border: none; color: #fff; font-size: 16px; border-radius: 8px; cursor: pointer; font-weight: 600; }
        button:hover { background: #1d4ed8; }
        #results { width: 100%; height: 380px; background: #030712; border: 1px solid #374151; color: #4ade80; padding: 12px; border-radius: 8px; box-sizing: border-box; margin-top: 15px; font-family: monospace; white-space: pre-wrap; overflow-y: auto; font-size: 13px; }
        .stats { display: flex; justify-content: space-between; margin-top: 15px; background: #1f2937; padding: 10px 15px; border-radius: 8px; font-weight: bold; }
        .counter { color: #facc15; }
        .job-id { color: #38bdf8; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <h2>⚡ Enterprise Academic Journal Email & PDF Extractor</h2>
        <input type="url" id="urlInput" placeholder="Enter Journal Issue URL (e.g. https://journal.org/issue/view/123)..." required>
        <button onclick="startScanning()">Start Secure Async Scrape</button>
        
        <div class="stats">
            <div>Unique Emails Found: <span id="count" class="counter">0</span></div>
            <div class="job-id">Job ID: <span id="jobIdDisplay">None</span></div>
        </div>
        <div id="results">Ready to scan. Enter an issue URL above and click start.</div>
    </div>

    <script>
        let uniqueEmails = new Set();
        let currentJobId = "";

        function startScanning() {
            const url = document.getElementById('urlInput').value.trim();
            if(!url) return alert('कृपया जर्नल इशू का URL दर्ज करें!');
            
            const resultsBox = document.getElementById('results');
            const countBox = document.getElementById('count');
            const jobIdBox = document.getElementById('jobIdDisplay');
            
            resultsBox.innerText = "Initializing secure request and discovering issue links...\n";
            uniqueEmails.clear();
            countBox.innerText = "0";

            fetch('/api/email/scrape', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: url })
            })
            .then(response => {
                if (!response.ok) {
                    return response.json().then(err => { throw new Error(err.error || 'Server error'); });
                }
                
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";

                function readStream() {
                    reader.read().then(({ done, value }) => {
                        if (done) {
                            resultsBox.innerText += "\\n--- Stream Completed ---\\n";
                            return;
                        }
                        
                        buffer += decoder.decode(value, { stream: true });
                        let lines = buffer.split("\\n\\n");
                        buffer = lines.pop(); // Keep incomplete chunk

                        lines.forEach(chunk => {
                            let eventName = "message";
                            let eventData = "";
                            
                            chunk.split("\\n").forEach(line => {
                                if (line.startsWith("event: ")) eventName = line.substring(7).trim();
                                if (line.startsWith("data: ")) eventData = line.substring(6).trim();
                            });

                            if (eventData) {
                                try {
                                    const data = JSON.parse(eventData);
                                    if (eventName === "connected" || eventName === "start") {
                                        if (data.jobId) {
                                            currentJobId = data.jobId;
                                            jobIdBox.innerText = currentJobId;
                                        }
                                        resultsBox.innerText += `[${eventName.toUpperCase()}] ${JSON.stringify(data)}\\n`;
                                    } else if (eventName === "article") {
                                        const res = data.result;
                                        if (res && res.all_emails && res.all_emails.length > 0) {
                                            resultsBox.innerText += `[Article] ${res.title || res.url}\\n`;
                                            res.all_emails.forEach(email => {
                                                if (!uniqueEmails.has(email)) {
                                                    uniqueEmails.add(email);
                                                    resultsBox.innerText += `   -> ${email}\\n`;
                                                }
                                            });
                                            countBox.innerText = uniqueEmails.size;
                                            resultsBox.scrollTop = resultsBox.scrollHeight;
                                        }
                                    } else if (eventName === "complete" || eventName === "stream_end") {
                                        resultsBox.innerText += `[${eventName.toUpperCase()}] ${data.message || 'Done'}\\n`;
                                    } else if (eventName === "error") {
                                        resultsBox.innerText += `[ERROR] ${data.error}\\n`;
                                    }
                                } catch (e) {
                                    // ignore parse errors for heartbeat or plain text
                                }
                            }
                        });

                        readStream();
                    }).catch(err => {
                        resultsBox.innerText += `\\nStream Error: ${err.message}`;
                    });
                }
                readStream();
            })
            .catch(err => {
                resultsBox.innerText += `\\nError: ${err.message}`;
            });
        }
    </script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "academic-journal-email-scraper",
        "time": utc_now(),
    })


@app.get("/api/email/jobs/<job_id>")
def get_job(job_id):
    try:
        records = load_job_records(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "jobId": job_id,
        "count": len(records),
        "results": records,
    })


@app.post("/api/email/scrape")
def scrape_endpoint():
    payload = request.get_json(silent=True) or {}
    raw_url = payload.get("url")

    try:
        issue_url = normalize_url(raw_url)

        job_id = request.args.get("jobId", "").strip()
        if job_id:
            job_path(job_id)
        else:
            job_id = str(uuid.uuid4())

        try:
            skip = int(request.args.get("skip", "0"))
        except ValueError:
            return jsonify({"error": "skip must be a non-negative integer"}), 400

        if skip < 0:
            return jsonify({"error": "skip must be non-negative"}), 400

        if skip > MAX_ARTICLES:
            return jsonify({"error": "skip exceeds the configured article limit"}), 400

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    output_queue = __import__("queue").Queue()

    def worker():
        try:
            asyncio.run(run_job(job_id, issue_url, skip, output_queue))
        except Exception as exc:
            LOG.exception("Worker crashed")
            output_queue.put({
                "type": "error",
                "jobId": job_id,
                "error": str(exc)[:1000],
            })
            output_queue.put(None)

    thread = threading.Thread(
        target=worker,
        name=f"scrape-{job_id[:8]}",
        daemon=True,
    )
    thread.start()

    @stream_with_context
    def generate():
        yield sse_event("connected", {
            "jobId": job_id,
            "message": "Scrape started.",
        })

        last_ping = time.monotonic()

        while True:
            try:
                item = output_queue.get(timeout=1.0)
            except __import__("queue").Empty:
                item = "HEARTBEAT"

            if item == "HEARTBEAT":
                if time.monotonic() - last_ping >= 10:
                    yield ": ping\n\n"
                    last_ping = time.monotonic()
                continue

            if item is None:
                break

            event_type = item.pop("type", "message")
            yield sse_event(event_type, item)
            last_ping = time.monotonic()

        yield sse_event("stream_end", {
            "jobId": job_id,
            "message": "Stream closed.",
        })

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        threaded=True,
    )
