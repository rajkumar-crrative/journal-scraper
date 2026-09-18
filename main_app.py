import os
import re
import io
import zipfile
import requests
from bs4 import BeautifulSoup
import pypdf
from flask import Flask, render_template_string, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# Security: Rate Limiter (Protection against Spam/Bots)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# AI-Driven Fake Email Suppressor
FAKE_PATTERNS = {'example.com', 'domain.com', 'email.com', 'png', 'jpg', 'jpeg', 'sentry.io', 'w3.org'}

def clean_and_validate_email(email_set):
    valid_emails = set()
    for email in email_set:
        email_clean = email.lower().strip()
        domain = email_clean.split('@')[-1] if '@' in email_clean else ''
        if not any(fake in domain for fake in FAKE_PATTERNS) and not email_clean.endswith(('.png', '.jpg', '.css', '.js')):
            valid_emails.add(email_clean)
    return valid_emails

def extract_from_pdf_bytes(pdf_bytes):
    emails = set()
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            text = page.extract_text()
            if text:
                found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
                emails.update(found)
    except Exception:
        pass
    return emails

# Executive HTML Template (Royal Blue, Gold, Silver & Saffron Theme)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Academic Email Scraper Pro | Executive Edition</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #f4f6f9; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; }
        .header-card { background: linear-gradient(135deg, #0f2027, #203a43, #2c5364); color: #fff; border-bottom: 4px solid #d4af37; padding: 25px; border-radius: 0 0 15px 15px; }
        .gold-badge { background-color: #d4af37; color: #000; font-weight: bold; padding: 5px 12px; border-radius: 20px; font-size: 0.85rem; }
        .btn-saffron { background-color: #ff6f00; color: #fff; font-weight: bold; border: none; }
        .btn-saffron:hover { background-color: #e66000; color: #fff; }
        .btn-royal { background-color: #0d6efd; color: #fff; font-weight: bold; }
        .main-card { border: 1px solid #e0e0e0; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.05); background: #fff; }
    </style>
</head>
<body>
    <div class="container-fluid header-card text-center mb-4">
        <span class="gold-badge mb-2 d-inline-block">OFFICIAL EXECUTIVE PLATFORM</span>
        <h2 class="fw-bold">Academic Journal Email Scraper Pro</h2>
        <p class="mb-1 text-light">Designed & Developed by <strong>Rajkumar Ahirwar</strong></p>
        <p class="mb-1 text-light"><small>Mother: Smt. Shakun Bai Ahirwar | DOB: 02-01-2001</small></p>
        <small class="text-warning">Support & Inquiries: ahirwarrajkumar518@gmail.com</small>
    </div>

    <div class="container mb-5">
        <div class="row justify-content-center">
            <div class="col-md-10">
                <div class="card main-card p-4">
                    <div class="alert alert-info d-flex justify-content-between align-items-center" role="alert">
                        <span><strong>7-Day Free Trial Active!</strong> Extract unlimited genuine emails from HTML, PDF & ZIP files.</span>
                        <span class="badge bg-dark">v2026.1 Pro</span>
                    </div>

                    <form id="scraperForm">
                        <div class="mb-3">
                            <label class="form-label fw-bold">Journal or Article URL</label>
                            <input type="url" id="url" class="form-control" placeholder="https://example-journal.com/volume-10/issue-2" required>
                        </div>
                        <div class="row">
                            <div class="col-md-6 mb-3">
                                <label class="form-label fw-bold">Volume (Optional)</label>
                                <input type="text" id="volume" class="form-control" placeholder="e.g. 12">
                            </div>
                            <div class="col-md-6 mb-3">
                                <label class="form-label fw-bold">Issue (Optional)</label>
                                <input type="text" id="issue" class="form-control" placeholder="e.g. 4">
                            </div>
                        </div>
                        <button type="submit" class="btn btn-saffron w-100 py-2 fs-5 mt-2" id="submitBtn">Start Extraction (HTML + PDF + ZIP)</button>
                    </form>

                    <div id="statusSection" class="mt-4 d-none">
                        <div class="spinner-border text-primary spinner-border-sm" role="status"></div>
                        <span id="statusText" class="ms-2 fw-bold text-primary">Processing URL... Please wait.</span>
                    </div>

                    <div class="mt-4">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <h5 class="fw-bold mb-0">Extracted Emails (Verified & Clean)</h5>
                            <button class="btn btn-royal btn-sm" onclick="copyEmails()">Copy All Emails</button>
                        </div>
                        <textarea id="outputBox" class="form-control bg-light" rows="10" readonly placeholder="Extracted emails will appear here..."></textarea>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        document.getElementById('scraperForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const url = document.getElementById('url').value;
            const submitBtn = document.getElementById('submitBtn');
            const statusSection = document.getElementById('statusSection');
            const outputBox = document.getElementById('outputBox');

            submitBtn.disabled = true;
            statusSection.classList.remove('d-none');
            outputBox.value = '';

            try {
                const response = await fetch('/api/extract', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url })
                });
                const data = await response.json();
                
                if (data.success) {
                    outputBox.value = data.emails.join('\\n');
                    alert(`Extraction Complete! Found ${data.emails.length} unique genuine email(s).`);
                } else {
                    alert('Error: ' + data.message);
                }
            } catch (err) {
                alert('Server Error. Please try again.');
            } finally {
                submitBtn.disabled = false;
                statusSection.classList.add('d-none');
            }
        });

        function copyEmails() {
            const copyText = document.getElementById("outputBox");
            if (!copyText.value) {
                alert("No emails to copy!");
                return;
            }
            copyText.select();
            navigator.clipboard.writeText(copyText.value);
            alert("All emails copied to clipboard!");
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/extract', methods=['POST'])
@limiter.limit("10 per minute")
def extract_api():
    data = request.get_json()
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'success': False, 'message': 'Invalid URL'}), 400

    found_emails = set()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    try:
        res = requests.get(url, headers=headers, timeout=60)
        soup = BeautifulSoup(res.text, 'html.parser')

        # 1. HTML Extraction
        raw_html_emails = set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', soup.get_text()))
        found_emails.update(raw_html_emails)

        # 2. PDF & ZIP Links Extraction
        for a in soup.find_all('a', href=True):
            href = a['href']
            full_url = href if href.startswith('http') else requests.compat.urljoin(url, href)

            if full_url.lower().endswith('.pdf'):
                try:
                    pdf_res = requests.get(full_url, headers=headers, timeout=10)
                    found_emails.update(extract_from_pdf_bytes(pdf_res.content))
                except:
                    continue
            elif full_url.lower().endswith('.zip'):
                try:
                    zip_res = requests.get(full_url, headers=headers, timeout=10)
                    with zipfile.ZipFile(io.BytesIO(zip_res.content)) as z:
                        for filename in z.namelist():
                            if filename.lower().endswith('.pdf'):
                                found_emails.update(extract_from_pdf_bytes(z.read(filename)))
                except:
                    continue

        cleaned_emails = sorted(list(clean_and_validate_email(found_emails)))
        return jsonify({'success': True, 'emails': cleaned_emails})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
