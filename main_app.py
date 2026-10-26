import os
import re
import io
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, jsonify
import pypdf

app = Flask(__name__)

# ==========================================
# STRICT AUTHOR EMAIL FILTER & PROXIMITY CONFIG
# ==========================================
BLOCKED_EMAIL_PREFIXES = (
    'info@', 'support@', 'editor@', 'chief@', 'help@', 
    'privacy@', 'contact@', 'admin@', 'queries@', 'editorial@', 
    'nepjol@', 'ubiquitypress@', 'webmaster@', 'noreply@', 'office@',
    'secretary@', 'managing@', 'production@', 'proofreader@', 'publisher@',
    'sales@', 'marketing@', 'finance@', 'events@', 'media@', 'permissions@',
    'fudutsinma.edu.ng'
)

def is_valid_author_email(email):
    email_lower = email.lower().strip()
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email_lower):
        return False
    if any(email_lower.startswith(prefix) for prefix in BLOCKED_EMAIL_PREFIXES):
        return False
    if any(domain in email_lower for domain in ['fudutsinma.edu.ng', 'nepjol.info', 'ubiquitypress.com', 'mdpi.com', 'journals.org', 'ojs.']):
        if not any(edu in email_lower for edu in ['gmail.com', 'yahoo.com', 'edu', 'ac.in', 'outlook.com', 'hotmail.com']):
            return False
    return True

def fetch_page(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None

def extract_emails_with_proximity(text, source_url):
    valid_emails = []
    raw_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    
    for email in raw_emails:
        cleaned = email.lower().strip('.')
        if not is_valid_author_email(cleaned):
            continue
            
        email_idx = text.lower().find(cleaned)
        if email_idx != -1:
            start_window = max(0, email_idx - 150)
            end_window = min(len(text), email_idx + 150)
            window_text = text[start_window:end_window].lower()
            
            if any(kw in window_text for kw in ['corresponding', 'author', 'e-mail', 'email', '*', 'correspondence', 'mailto']):
                if cleaned not in valid_emails:
                    valid_emails.append(cleaned)
            else:
                if any(dom in cleaned for dom in ['gmail.com', 'yahoo.com', 'edu', 'ac.in', 'outlook.com']):
                    if cleaned not in valid_emails:
                        valid_emails.append(cleaned)
        else:
            if cleaned not in valid_emails:
                valid_emails.append(cleaned)
                
    return valid_emails

def parse_pdf_for_author_emails(pdf_url, base_url):
    try:
        full_pdf_url = pdf_url if pdf_url.startswith('http') else base_url.rstrip('/') + '/' + pdf_url.lstrip('/')
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(full_pdf_url, headers=headers, timeout=20)
        if resp.status_code == 200:
            pdf_file = io.BytesIO(resp.content)
            reader = pypdf.PdfReader(pdf_file)
            full_text = ""
            for page in reader.pages[:2]:
                full_text += page.extract_text() + "\n"
            return extract_emails_with_proximity(full_text, full_pdf_url)
    except Exception:
        pass
    return []

def process_article_page(article_url):
    html = fetch_page(article_url)
    if not html:
        return []
        
    soup = BeautifulSoup(html, 'html.parser')
    page_text = soup.get_text()
    
    emails = extract_emails_with_proximity(page_text, article_url)
    
    if len(emails) == 0:
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            href_lower = href.lower()
            if ('/pdf/' in href_lower) or (href_lower.endswith('.pdf')) or ('/download/' in href_lower) or (re.search(r'/view/\d+/\d+', href_lower)):
                parsed_url = requests.utils.urlparse(article_url)
                base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
                pdf_emails = parse_pdf_for_author_emails(href, base_domain)
                for pe in pdf_emails:
                    if pe not in emails:
                        emails.append(pe)
                        if len(emails) > 0:
                            break
    return emails

# ==========================================
# FRONTEND UI
# ==========================================
@app.route('/')
def home():
    return render_template_string('''
    <!DOCTYPE html>
    <html lang="hi">
    <head>
        <meta charset="UTF-8">
        <title>Masterclass Enterprise Corresponding Author Email Tracker</title>
        <style>
            :root {
                --bg-color: #0b0f19;
                --card-bg: rgba(23, 32, 54, 0.9);
                --neon-cyan: #22d3ee;
                --neon-pink: #ec4899;
                --glass-border: rgba(255, 255, 255, 0.12);
                --text-muted: #94a3b8;
            }
            body { background: var(--bg-color); color: #fff; font-family: sans-serif; padding: 20px; }
            .container { max-width: 950px; margin: 0 auto; background: var(--card-bg); padding: 30px; border-radius: 16px; border: 1px solid var(--glass-border); box-shadow: 0 15px 40px rgba(0,0,0,0.6); }
            h2 { text-align: center; color: var(--neon-cyan); margin-bottom: 5px; }
            p.sub { text-align: center; color: var(--neon-pink); font-size: 14px; margin-bottom: 25px; }
            input[type="text"] { width: 100%; padding: 14px; margin-bottom: 15px; border-radius: 8px; border: 1px solid var(--glass-border); background: #0f172a; color: #fff; box-sizing: border-box; font-size: 14px; }
            button.btn { width: 100%; padding: 14px; background: linear-gradient(135deg, #6366f1, var(--neon-pink)); border: none; border-radius: 8px; color: #fff; font-size: 16px; font-weight: bold; cursor: pointer; transition: 0.3s; }
            button.btn:hover { opacity: 0.9; }
            .terminal { margin-top: 25px; background: #0f172a; padding: 20px; border-radius: 8px; border: 1px solid var(--glass-border); }
            
            .stats-container { display: flex; gap: 15px; margin-bottom: 20px; }
            .stat-box { flex: 1; background: rgba(34, 211, 238, 0.08); border: 1px solid var(--neon-cyan); padding: 15px; border-radius: 8px; text-align: center; }
            .stat-box h3 { margin: 0 0 5px 0; color: var(--text-muted); font-size: 13px; text-transform: uppercase; }
            .stat-box span { font-size: 26px; font-weight: bold; color: var(--neon-cyan); }
            
            .email-list-box { background: rgba(255, 255, 255, 0.03); border: 1px solid var(--glass-border); padding: 15px; border-radius: 8px; margin-top: 15px; max-height: 400px; overflow-y: auto; }
            ol.numbered-emails { margin: 0; padding-left: 20px; }
            ol.numbered-emails li { padding: 8px 0; font-family: monospace; font-size: 16px; color: var(--neon-cyan); border-bottom: 1px solid rgba(255,255,255,0.05); }
            .copy-btn { background: #0ea5e9; border: none; padding: 8px 14px; color: #fff; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: bold; float: right; margin-bottom: 10px; }
            .copy-btn:hover { background: #0284c7; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>👑 Masterclass Enterprise Email Tracker</h2>
            <p class="sub">Precision Corresponding Author Extraction Engine (Boss Edition)</p>
            
            <input type="text" id="targetUrl" placeholder="Enter Journal Issue / Volume URL">
            <button class="btn" onclick="startScraping()">Launch Masterclass Tracker</button>
            
            <div class="terminal" id="terminalBox" style="display:none;">
                <div class="stats-container">
                    <div class="stat-box">
                        <h3>Total Articles Found</h3>
                        <span id="totalArticles">0</span>
                    </div>
                    <div class="stat-box">
                        <h3>Total Unique Emails</h3>
                        <span id="totalEmails">0</span>
                    </div>
                </div>
                
                <div style="overflow:hidden;">
                    <button class="copy-btn" onclick="copyAllEmails()">📋 Copy All Unique Emails</button>
                    <span style="font-weight: bold; line-height: 32px; color: #fff; font-size: 15px;">Extracted Corresponding Author Emails:</span>
                </div>
                
                <div class="email-list-box">
                    <ol class="numbered-emails" id="emailListContainer">
                        <li style="color: var(--text-muted);">Scanning issue container and extracting emails...</li>
                    </ol>
                </div>
            </div>
        </div>

        <script>
            let cachedEmails = [];

            async function startScraping() {
                const url = document.getElementById('targetUrl').value.trim();
                if(!url) return alert('कृपया जर्नल का URL डालें!');
                
                document.getElementById('terminalBox').style.display = 'block';
                document.getElementById('totalArticles').innerText = '...';
                document.getElementById('totalEmails').innerText = '...';
                document.getElementById('emailListContainer').innerHTML = '<li style="color: var(--neon-cyan);">Analyzing container & parsing PDFs for author emails...</li>';
                cachedEmails = [];
                
                try {
                    const resp = await fetch('/scan', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({url: url})
                    });
                    const data = await resp.json();
                    
                    if(!data.success) {
                        alert(data.message);
                        return;
                    }
                    
                    document.getElementById('totalArticles').innerText = data.total_articles_found;
                    document.getElementById('totalEmails').innerText = data.total_emails_count;
                    
                    cachedEmails = data.all_unique_emails;
                    const listContainer = document.getElementById('emailListContainer');
                    listContainer.innerHTML = '';
                    
                    if(cachedEmails.length === 0) {
                        listContainer.innerHTML = '<li style="color: #f87171;">कोई वैध ऑथर ईमेल नहीं मिला।</li>';
                        return;
                    }
                    
                    cachedEmails.forEach(email => {
                        listContainer.innerHTML += `<li>${email}</li>`;
                    });
                    
                } catch(err) {
                    alert('Connection Error: ' + err.message);
                }
            }

            function copyAllEmails() {
                if(cachedEmails.length === 0) return alert('कॉपी करने के लिए कोई ईमेल नहीं है!');
                navigator.clipboard.writeText(cachedEmails.join('\\n'));
                alert(cachedEmails.length + ' unique emails copied to clipboard!');
            }
        </script>
    </body>
    </html>
    ''')

# ==========================================
# BACKEND SCAN ENGINE & ROUTE
# ==========================================
@app.route('/scan', methods=['POST'])
def scan_journal():
    data = request.get_json()
    issue_url = data.get('url', '').strip()
    
    if not issue_url:
        return jsonify({'success': False, 'message': 'Invalid URL provided'})
        
    issue_html = fetch_page(issue_url)
    if not issue_html:
        return jsonify({'success': False, 'message': 'Target journal page is unreachable.'})
        
    soup = BeautifulSoup(issue_html, 'html.parser')
    
    article_links = []
    
    summary_divs = soup.find_all(class_=re.compile(r'(obj_article_summary|article-summary|row|item|title)', re.I))
    if summary_divs:
        for div in summary_divs:
            for a_tag in div.find_all('a', href=True):
                href = a_tag['href']
                if '/article/view/' in href.lower() or '/view/' in href.lower():
                    parsed_url = requests.utils.urlparse(issue_url)
                    base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
                    full_url = href if href.startswith('http') else base_domain + ('/' + href.lstrip('/') if not href.startswith('/') else href)
                    if full_url not in article_links and 'issue' not in full_url.lower():
                        article_links.append(full_url)
                        
    if not article_links:
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if '/article/view/' in href.lower() and 'issue' not in href.lower():
                parsed_url = requests.utils.urlparse(issue_url)
                base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
                full_url = href if href.startswith('http') else base_domain + ('/' + href.lstrip('/') if not href.startswith('/') else href)
                if full_url not in article_links:
                    article_links.append(full_url)
                    
    total_found = len(article_links)
    
    if total_found == 0:
        return jsonify({'success': False, 'message': 'No research articles detected in this issue link. Please provide a valid issue/volume view URL.'})
        
    all_extracted_emails = []
    for link in article_links[:45]:
        emails = process_article_page(link)
        for em in emails:
            if em not in all_extracted_emails:
                all_extracted_emails.append(em)
                
    return jsonify({
        'success': True,
        'total_articles_found': total_found,
        'total_emails_count': len(all_extracted_emails),
        'all_unique_emails': all_extracted_emails
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
