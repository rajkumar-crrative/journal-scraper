import os
import re
import io
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, jsonify
import pypdf

app = Flask(__name__)

# ==============================================================================
# ULTRA-PRO CONFIGURATION & STRICT AUTHOR EMAIL FILTER
# ==============================================================================
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
    # बुनियादी ईमेल फॉर्मेट जाँच
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email_lower):
        return False
    # एडमिन प्रीफिक्स ब्लॉक करना
    if any(email_lower.startswith(prefix) for prefix in BLOCKED_EMAIL_PREFIXES):
        return False
    # यूनिवर्सिटी या पब्लिशर डोमेन ब्लॉक करना (जब तक व्यक्तिगत या शैक्षणिक ईमेल न हो)
    if any(domain in email_lower for domain in ['fudutsinma.edu.ng', 'nepjol.info', 'ubiquitypress.com', 'mdpi.com', 'journals.org', 'ojs.', 'scholar.']):
        if not any(edu in email_lower for edu in ['gmail.com', 'yahoo.com', 'edu', 'ac.in', 'outlook.com', 'hotmail.com', 'research', 'proton.me']):
            return False
    return True

def extract_clean_emails(text):
    """टेक्स्ट से सभी संभावित ईमेल निकालकर उन्हें वैलिडेट करना"""
    found_emails = []
    raw_matches = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    for email in raw_matches:
        cleaned = email.lower().strip('.')
        if is_valid_author_email(cleaned):
            if cleaned not in found_emails:
                found_emails.append(cleaned)
    return found_emails

def parse_pdf_stream(pdf_content):
    """PDF बाइट्स को क्रैक करके अंदर के टेक्स्ट से ईमेल निकालना"""
    emails = []
    try:
        pdf_file = io.BytesIO(pdf_content)
        reader = pypdf.PdfReader(pdf_file)
        full_pdf_text = ""
        # रिसर्च पेपर के पहले 2-3 पन्नों में ही कॉरेस्पोंडिंग ऑथर का ईमेल होता है
        for page in reader.pages[:3]:
            text_extracted = page.extract_text()
            if text_extracted:
                full_pdf_text += text_extracted + "\n"
        
        extracted = extract_clean_emails(full_pdf_text)
        for em in extracted:
            if em not in emails:
                emails.append(em)
    except Exception:
        pass
    return emails

def download_and_extract_pdf(pdf_url, base_domain):
    """दिए गए PDF URL से बाइट्स डाउनलोड करना"""
    try:
        full_url = pdf_url if pdf_url.startswith('http') else base_domain.rstrip('/') + '/' + pdf_url.lstrip('/')
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        }
        resp = requests.get(full_url, headers=headers, timeout=20)
        if resp.status_code == 200:
            content_type = resp.headers.get('content-type', '').lower()
            if 'pdf' in content_type or resp.content.startswith(b'%PDF'):
                return parse_pdf_stream(resp.content)
    except Exception:
        pass
    return []

def process_single_article(article_url, base_domain):
    """आर्टिकल पेज पर जाकर उसके PDF लिंक को ढूंढना और क्रैक करना"""
    emails = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    }
    try:
        resp = requests.get(article_url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
            
        art_soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 1. पहले आर्टिकल पेज के अंदर PDF गैलरी/डाउनलोड बटन ढूँढना
        pdf_links = []
        for a_tag in art_soup.find_all('a', href=True):
            href = a_tag['href']
            href_lower = href.lower()
            link_text = a_tag.get_text().lower()
            
            if ('/pdf' in href_lower) or ('/download' in href_lower) or (href_lower.endswith('.pdf')) or ('pdf' in link_text):
                if href not in pdf_links:
                    pdf_links.append(href)
                    
        # PDF लिंक्स को क्रैक करके ईमेल निकालना
        for p_link in pdf_links:
            pdf_emails = download_and_extract_pdf(p_link, base_domain)
            for pe in pdf_emails:
                if pe not in emails:
                    emails.append(pe)
            if len(emails) > 0:
                break # जैसे ही ईमेल मिला, आगे बढ़ने की ज़रूरत नहीं
                
        # 2. अगर PDF से ईमेल न मिले, तो HTML पेज के मुख्य टेक्स्ट को स्कैन करना (Fallback)
        if len(emails) == 0:
            html_text = art_soup.get_text()
            html_emails = extract_clean_emails(html_text)
            for he in html_emails:
                if he not in emails:
                    emails.append(he)
                    
    except Exception:
        pass
        
    return emails

# ==============================================================================
# FRONTEND USER INTERFACE (ULTRA-PRO DASHBOARD)
# ==============================================================================
@app.route('/')
def home():
    return render_template_string('''
    <!DOCTYPE html>
    <html lang="hi">
    <head>
        <meta charset="UTF-8">
        <title>Ultra-Pro OJS PDF & HTML Email Tracker</title>
        <style>
            :root {
                --bg-color: #07090e;
                --card-bg: rgba(18, 24, 38, 0.95);
                --neon-cyan: #00f2fe;
                --neon-pink: #ff0844;
                --glass-border: rgba(255, 255, 255, 0.1);
                --text-muted: #8b9bb4;
            }
            body { background: var(--bg-color); color: #fff; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 20px; }
            .container { max-width: 950px; margin: 0 auto; background: var(--card-bg); padding: 35px; border-radius: 18px; border: 1px solid var(--glass-border); box-shadow: 0 20px 50px rgba(0,0,0,0.8); }
            h2 { text-align: center; color: var(--neon-cyan); margin-bottom: 5px; font-size: 26px; }
            p.sub { text-align: center; color: var(--neon-pink); font-size: 14px; margin-bottom: 30px; font-weight: 600; letter-spacing: 0.5px; }
            input[type="text"] { width: 100%; padding: 15px; margin-bottom: 15px; border-radius: 10px; border: 1px solid var(--glass-border); background: #0b0f19; color: #fff; box-sizing: border-box; font-size: 15px; outline: none; }
            input[type="text"]:focus { border-color: var(--neon-cyan); }
            button.btn { width: 100%; padding: 15px; background: linear-gradient(135deg, #00f2fe, #4facfe); border: none; border-radius: 10px; color: #000; font-size: 16px; font-weight: bold; cursor: pointer; transition: 0.3s; box-shadow: 0 5px 20px rgba(0,242,254,0.3); }
            button.btn:hover { opacity: 0.9; transform: translateY(-1px); }
            .terminal { margin-top: 30px; background: #0b0f19; padding: 25px; border-radius: 12px; border: 1px solid var(--glass-border); }
            
            .stats-container { display: flex; gap: 20px; margin-bottom: 25px; }
            .stat-box { flex: 1; background: rgba(0, 242, 254, 0.05); border: 1px solid var(--neon-cyan); padding: 18px; border-radius: 10px; text-align: center; }
            .stat-box h3 { margin: 0 0 8px 0; color: var(--text-muted); font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
            .stat-box span { font-size: 28px; font-weight: bold; color: var(--neon-cyan); }
            
            .email-list-box { background: rgba(255, 255, 255, 0.02); border: 1px solid var(--glass-border); padding: 20px; border-radius: 10px; margin-top: 15px; max-height: 420px; overflow-y: auto; }
            ol.numbered-emails { margin: 0; padding-left: 22px; }
            ol.numbered-emails li { padding: 10px 0; font-family: 'Courier New', Courier, monospace; font-size: 16px; color: var(--neon-cyan); border-bottom: 1px solid rgba(255,255,255,0.04); }
            .copy-btn { background: #10b981; border: none; padding: 10px 16px; color: #fff; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: bold; float: right; transition: 0.2s; }
            .copy-btn:hover { background: #059669; }
            .article-card { background: rgba(255,255,255,0.03); border: 1px solid var(--glass-border); padding: 12px 15px; border-radius: 8px; margin-bottom: 10px; font-size: 13px; }
            .article-title { color: #fff; font-weight: 600; margin-bottom: 5px; }
            .article-emails { color: var(--neon-cyan); font-family: monospace; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>⚡ Ultra-Pro OJS PDF Email Tracker</h2>
            <p class="sub">Direct PDF-Parsing Engine & Hybrid Extraction (Boss Edition)</p>
            
            <input type="text" id="targetUrl" placeholder="Enter Journal Issue URL (e.g., .../issue/view/19)">
            <button class="btn" onclick="startScraping()">Launch Ultra-Pro Extraction</button>
            
            <div class="terminal" id="terminalBox" style="display:none;">
                <div class="stats-container">
                    <div class="stat-box">
                        <h3>Articles Scanned</h3>
                        <span id="totalArticles">0</span>
                    </div>
                    <div class="stat-box">
                        <h3>Unique Author Emails</h3>
                        <span id="totalEmails">0</span>
                    </div>
                </div>
                
                <div style="overflow:hidden; margin-bottom: 15px;">
                    <button class="copy-btn" onclick="copyAllEmails()">📋 Copy All Emails</button>
                    <span style="font-weight: bold; line-height: 38px; color: #fff; font-size: 15px;">Extracted Corresponding Author Emails:</span>
                </div>
                
                <div class="email-list-box">
                    <ol class="numbered-emails" id="emailListContainer">
                        <li style="color: var(--text-muted);">Initializing deep PDF parsing sequence...</li>
                    </ol>
                </div>
                
                <div id="articlesDetailContainer" style="margin-top: 20px;"></div>
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
                document.getElementById('emailListContainer').innerHTML = '<li style="color: var(--neon-cyan);">Downloading issue page & cracking PDFs for author emails...</li>';
                document.getElementById('articlesDetailContainer').innerHTML = '';
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
                        listContainer.innerHTML = '<li style="color: #ff0844;">कोई वैध ऑथर ईमेल नहीं मिला।</li>';
                        return;
                    }
                    
                    cachedEmails.forEach(email => {
                        listContainer.innerHTML += `<li>${email}</li>`;
                    });
                    
                    // हर आर्टिकल का विस्तृत विवरण दिखाना
                    if(data.article_details && data.article_details.length > 0) {
                        let detailHTML = '<h4 style="color: var(--text-muted); margin-bottom: 10px; font-size: 13px; text-transform: uppercase;">Article Breakdown:</h4>';
                        data.article_details.forEach((art, idx) => {
                            detailHTML += `
                                <div class="article-card">
                                    <div class="article-title">[${idx+1}] ${art.title}</div>
                                    <div class="article-emails">Emails: ${art.emails.length > 0 ? art.emails.join(', ') : '<span style="color: #ff0844;">No email found</span>'}</div>
                                </div>
                            `;
                        });
                        document.getElementById('articlesDetailContainer').innerHTML = detailHTML;
                    }
                    
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

# ==============================================================================
# BACKEND SCAN ROUTE & OJS CONTAINER PARSER
# ==============================================================================
@app.route('/scan', methods=['POST'])
def scan_journal():
    data = request.get_json()
    issue_url = data.get('url', '').strip()
    
    if not issue_url:
        return jsonify({'success': False, 'message': 'Invalid URL provided'})
        
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    }
    try:
        resp = requests.get(issue_url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return jsonify({'success': False, 'message': 'Target issue page is unreachable.'})
        issue_html = resp.text
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
        
    soup = BeautifulSoup(issue_html, 'html.parser')
    parsed_base = requests.utils.urlparse(issue_url)
    base_domain = f"{parsed_base.scheme}://{parsed_base.netloc}"
    
    # सटीक आर्टिकल लिंक्स ढूँढना (OJS समरी कंटेनर या आर्टिकल व्यू लिंक्स)
    article_links = []
    summary_divs = soup.find_all(class_=re.compile(r'(obj_article_summary|article-summary|row|item)', re.I))
    
    if summary_divs:
        for div in summary_divs:
            for a_tag in div.find_all('a', href=True):
                href = a_tag['href']
                if '/article/view/' in href.lower() and 'issue' not in href.lower():
                    full_url = href if href.startswith('http') else base_domain + ('/' + href.lstrip('/') if not href.startswith('/') else href)
                    if full_url not in article_links:
                        article_links.append(full_url)
                        
    # अगर कंटेनर से नहीं मिले, तो डायरेक्ट आर्टिकल व्यू लिंक खोजें
    if not article_links:
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if '/article/view/' in href.lower() and 'issue' not in href.lower():
                full_url = href if href.startswith('http') else base_domain + ('/' + href.lstrip('/') if not href.startswith('/') else href)
                if full_url not in article_links:
                    article_links.append(full_url)
                    
    # डुप्लीकेट आईडी फिल्टरिंग (ताकि एक ही आर्टिकल के दो अलग लिंक काउंट न हों)
    unique_article_urls = []
    seen_ids = set()
    for link in article_links:
        match = re.search(r'/article/view/(\d+)', link)
        art_id = match.group(1) if match else link
        if art_id not in seen_ids:
            seen_ids.add(art_id)
            unique_article_urls.append(link)
            
    total_articles = len(unique_article_urls)
    if total_articles == 0:
        return jsonify({'success': False, 'message': 'No research articles found in this issue link. Please provide a valid OJS issue view URL.'})
        
    all_extracted_emails = []
    article_details = []
    
    # प्रत्येक आर्टिकल को प्रोसेस करना
    for link in unique_article_urls[:30]:
        try:
            art_resp = requests.get(link, headers=headers, timeout=10)
            art_title = "Research Article"
            if art_resp.status_code == 200:
                art_soup = BeautifulSoup(art_resp.text, 'html.parser')
                title_tag = art_soup.find('h1') or art_soup.find(class_=re.compile('title', re.I))
                if title_tag:
                    art_title = title_tag.get_text(strip=True)
        except Exception:
            art_title = "Research Article"
            
        emails = process_single_article(link, base_domain)
        
        article_details.append({
            'title': art_title,
            'emails': emails
        })
        
        for em in emails:
            if em not in all_extracted_emails:
                all_extracted_emails.append(em)
                
    return jsonify({
        'success': True,
        'total_articles_found': total_articles,
        'total_emails_count': len(all_extracted_emails),
        'all_unique_emails': all_extracted_emails,
        'article_details': article_details
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
