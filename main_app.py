import os
import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

# ==========================================
# 1. ROBUST ENTERPRISE FILTER CONFIGURATION
# ==========================================
BLOCKED_EMAIL_PREFIXES = (
    'info@', 'support@', 'editor@', 'chief@', 'help@', 
    'privacy@', 'contact@', 'admin@', 'queries@', 'editorial@', 
    'nepjol@', 'ubiquitypress@', 'webmaster@', 'noreply@', 'office@',
    'secretary@', 'managing@', 'production@', 'proofreader@'
)

# फालतू या नेविगेशन वाले लिंक्स को ब्लॉक करने की लिस्ट
BLOCKED_URL_KEYWORDS = [
    '/about', '/editorialteam', '/privacy', '/submissions', 
    '/contact', '/announcements', '/indexing', '/search', '/index',
    '/abouttheauthors', '/policies', '/peerreview', '/guidelines',
    '/issue/archive', '/volume/', '/browse'
]

def is_valid_author_email(email):
    """सख्त जाँच: केवल असली ऑथर के ईमेल को पास होने दे, बाकि सब ब्लॉक करें"""
    email_lower = email.lower()
    
    # एक्सटेंशन या फॉर्मेट की जाँच
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email_lower):
        return False
        
    # ब्लॉक किए गए प्रीफिक्स से मिलान
    if any(email_lower.startswith(prefix) for prefix in BLOCKED_EMAIL_PREFIXES):
        return False
        
    # जर्नल्स के अपने डोमेन वाले जेनेरिक ईमेल्स को हटाना (जैसे publisher या journal के खुद के नाम से हों)
    if 'journal' in email_lower or 'press' in email_lower or 'admin' in email_lower or 'nepjol' in email_lower:
        if not ('gmail.com' in email_lower or 'yahoo.com' in email_lower or 'edu' in email_lower):
            return False
            
    return True

def fetch_page(url):
    """सुरक्षित HTTP गेट रिक्वेस्ट"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
    }
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None

def extract_author_emails_deep(soup, full_text):
    """
    डीप-स्कैन फंक्शन: आर्टिकल के अंदर 'Corresponding Author' या 
    ईमेल पैटर्न्स को सटीकता से ढूंढना
    """
    found_emails = []
    
    # 1. पहले ऑथर / कॉरेस्पोंडिंग सेक्शन या पैराग्राफ्स को ढूंढें
    target_elements = soup.find_all(['p', 'div', 'span', 'li', 'td'], string=re.compile(r'(corresponding|author|email|e-mail|\*)', re.IGNORECASE))
    
    scan_text = full_text
    if target_elements:
        scan_text = " ".join([el.get_text() for el in target_elements]) + " " + full_text
        
    raw_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', scan_text)
    
    for email in raw_emails:
        cleaned_email = email.lower().strip('.')
        if is_valid_author_email(cleaned_email):
            if cleaned_email not in found_emails:
                found_emails.append(cleaned_email)
                
    return found_emails

def deep_scan_article(article_url):
    """हर एक रिसर्च आर्टिकल के पेज के भीतर जाकर डीप-स्कैन करना"""
    html = fetch_page(article_url)
    if not html:
        return {'url': article_url, 'title': 'Unable to access article page', 'emails': []}
        
    soup = BeautifulSoup(html, 'html.parser')
    
    # शीर्षक (Title) निकालना
    title_tag = soup.find('h1') or soup.find('title')
    title = title_tag.get_text().strip() if title_tag else article_url
    # अगर टाइटल बहुत बड़ा हो तो छोटा करें
    title = re.sub(r'\s+', ' ', title)
    
    emails = extract_author_emails_deep(soup, html)
    
    return {
        'url': article_url,
        'title': title,
        'emails': emails
    }

# ==========================================
# 2. FRONTEND GLASSMORPHIC INTERFACE
# ==========================================
@app.route('/')
def home():
    return render_template_string('''
    <!DOCTYPE html>
    <html lang="hi">
    <head>
        <meta charset="UTF-8">
        <title>Galaxy Enterprise Email Tracker & Scraper Engine</title>
        <style>
            :root {
                --bg-color: #0b0f19;
                --card-bg: rgba(23, 32, 54, 0.75);
                --neon-cyan: #22d3ee;
                --neon-pink: #ec4899;
                --glass-border: rgba(255, 255, 255, 0.12);
                --text-muted: #94a3b8;
            }
            body {
                background: var(--bg-color);
                color: #fff;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                margin: 0; padding: 20px;
            }
            .container {
                max-width: 950px;
                margin: 0 auto;
                background: var(--card-bg);
                backdrop-filter: blur(18px);
                border: 1px solid var(--glass-border);
                border-radius: 16px;
                padding: 30px;
                box-shadow: 0 12px 40px 0 rgba(0, 0, 0, 0.45);
            }
            h2 { text-align: center; color: var(--neon-cyan); margin-bottom: 5px; }
            p.sub { text-align: center; color: var(--neon-pink); font-size: 14px; margin-top: 0; }
            .input-group { margin-bottom: 20px; }
            label { display: block; margin-bottom: 8px; font-weight: bold; color: var(--text-muted); }
            input[type="text"] {
                width: 100%; padding: 14px;
                border-radius: 8px; border: 1px solid var(--glass-border);
                background: rgba(15, 23, 42, 0.85); color: #fff;
                box-sizing: border-box; font-size: 14px;
            }
            button.btn {
                width: 100%; padding: 14px;
                background: linear-gradient(135deg, #6366f1, var(--neon-pink));
                border: none; border-radius: 8px; color: #fff;
                font-size: 16px; font-weight: bold; cursor: pointer;
                transition: 0.3s ease;
            }
            button.btn:hover { opacity: 0.92; transform: translateY(-1px); }
            .terminal {
                margin-top: 25px; background: rgba(15, 23, 42, 0.95);
                border-radius: 8px; padding: 20px; border: 1px solid var(--glass-border);
            }
            .stats-box {
                display: flex; justify-content: space-between;
                background: rgba(34, 211, 238, 0.1); border: 1px solid var(--neon-cyan);
                padding: 14px 18px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; flex-wrap: wrap; gap: 10px;
            }
            .article-card {
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid var(--glass-border);
                padding: 14px; border-radius: 8px; margin-bottom: 14px;
            }
            .email-badge {
                display: inline-block; background: rgba(34, 211, 238, 0.2);
                color: var(--neon-cyan); border: 1px solid var(--neon-cyan);
                padding: 5px 12px; border-radius: 20px; font-size: 12px; margin: 4px 6px 0 0;
                font-weight: bold;
            }
            .copy-btn {
                background: #0ea5e9; border: none; padding: 6px 12px; color: #fff;
                border-radius: 6px; cursor: pointer; font-size: 12px; float: right;
            }
            .copy-btn:hover { background: #0284c7; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>🚀 Galaxy Enterprise Email Tracker & Scraper</h2>
            <p class="sub">Advanced Corresponding Author Extraction Engine</p>
            
            <div class="input-group">
                <label>Target Journal Issue / Volume URL:</label>
                <input type="text" id="targetUrl" placeholder="https://www.nepjol.info/index.php/jafu/issue/view/...">
            </div>
            
            <button class="btn" onclick="startScraping()">Launch Deep-Scan Sequence</button>
            
            <div class="terminal" id="terminalBox" style="display:none;">
                <div style="margin-bottom: 15px; overflow: hidden;">
                    <button class="copy-btn" onclick="copyAllEmails()">📋 Copy All Unique Emails</button>
                    <span style="font-weight: bold; color: var(--neon-cyan);">Live Terminal & Extraction Results</span>
                </div>
                <div id="statsHeader" class="stats-box"></div>
                <div id="resultsList"></div>
            </div>
        </div>

        <script>
            let allExtractedEmails = [];

            async function startScraping() {
                const url = document.getElementById('targetUrl').value.trim();
                if(!url) return alert('कृपया जर्नल का URL डालें!');
                
                const term = document.getElementById('terminalBox');
                const stats = document.getElementById('statsHeader');
                const resList = document.getElementById('resultsList');
                
                term.style.display = 'block';
                stats.innerHTML = 'Scanning issue index page and counting research articles...';
                resList.innerHTML = '<p style="color: var(--neon-cyan);">Deep-scanning articles and extracting author emails...</p>';
                allExtractedEmails = [];
                
                try {
                    const resp = await fetch('/scan', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({url: url})
                    });
                    const data = await resp.json();
                    
                    if(!data.success) {
                        stats.innerHTML = `<span style="color: #f87171;">Error: ${data.message}</span>`;
                        resList.innerHTML = '';
                        return;
                    }
                    
                    allExtractedEmails = data.all_unique_emails || [];
                    
                    stats.innerHTML = `
                        <span><b>Total Articles Found:</b> ${data.total_articles_found}</span>
                        <span><b>Articles with Author Emails:</b> ${data.valid_articles_count}</span>
                        <span><b>Total Unique Emails:</b> ${data.total_emails_count}</span>
                    `;
                    
                    resList.innerHTML = '';
                    if(data.records.length === 0) {
                        resList.innerHTML = '<p style="color: #f87171; text-align:center;">इस इशू में कोई भी वैध ऑथर ईमेल नहीं मिला।</p>';
                        return;
                    }
                    
                    data.records.forEach((rec, idx) => {
                        let emailBadges = rec.emails.map(e => `<span class="email-badge">${e}</span>`).join('');
                        resList.innerHTML += `
                            <div class="article-card">
                                <b>[${idx+1}] ${rec.title}</b><br>
                                <a href="${rec.url}" target="_blank" style="color: var(--text-muted); font-size: 11px;">🔗 ${rec.url}</a>
                                <div style="margin-top: 8px;">${emailBadges || '<span style="color: #f87171; font-size:12px;">No author email detected in this article</span>'}</div>
                            </div>
                        `;
                    });
                    
                } catch(err) {
                    stats.innerHTML = `<span style="color: #f87171;">Connection Failed: ${err.message}</span>`;
                }
            }

            function copyAllEmails() {
                if(allExtractedEmails.length === 0) return alert('कॉपी करने के लिए कोई ईमेल नहीं है!');
                navigator.clipboard.writeText(allExtractedEmails.join('\\n'));
                alert(allExtractedEmails.length + ' unique emails copied to clipboard!');
            }
        </script>
    </body>
    </html>
    ''')

# ==========================================
# 3. BACKEND SCAN ROUTE & ENGINE LOGIC
# ==========================================
@app.route('/scan', methods=['POST'])
def scan_journal():
    data = request.get_json()
    issue_url = data.get('url', '').strip()
    
    if not issue_url:
        return jsonify({'success': False, 'message': 'Invalid URL provided'})
        
    issue_html = fetch_page(issue_url)
    if not issue_html:
        return jsonify({'success': False, 'message': 'Target journal issue website is unreachable.'})
        
    soup = BeautifulSoup(issue_html, 'html.parser')
    
    # सटीक आर्टिकल लिंक्स को ढूंढना (बिना किसी मिसिंग काउंट के)
    article_links = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        # OJS और अन्य ग्लोबल जर्नल्स के आर्टिकल लिंक्स पैटर्न
        if any(kw in href.lower() for kw in ['/article/view/', '/articles/', '/pdf/']):
            if not any(blocked in href.lower() for blocked in BLOCKED_URL_KEYWORDS):
                full_url = href if href.startswith('http') else os.path.dirname(issue_url) + '/' + href
                # डुप्लीकेट लिंक्स हटाने के लिए
                if full_url not in article_links:
                    article_links.append(full_url)
                    
    total_found = len(article_links)
    
    # यदि इशू पेज पर डायरेक्ट आर्टिकल लिंक नहीं मिले, तो पूरी लिस्ट से ढूंढें
    if total_found == 0:
        # ओजेएस के सामान्य ढाँचे के लिए दूसरा प्रयास
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if '/view/' in href and href not in article_links:
                full_url = href if href.startswith('http') else os.path.dirname(issue_url) + '/' + href
                if full_url not in article_links:
                    article_links.append(full_url)
        total_found = len(article_links)
        
    if total_found == 0:
        # फॉलबैक: यदि कोई दूसरा तरीका न मिले तो लैंडिंग पेज से ही निकालें
        emails = extract_author_emails_deep(soup, issue_html)
        return jsonify({
            'success': True,
            'total_articles_found': 1,
            'valid_articles_count': 1 if emails else 0,
            'total_emails_count': len(emails),
            'all_unique_emails': emails,
            'records': [{'title': 'Issue Landing Page Scanned', 'url': issue_url, 'emails': emails}]
        })
        
    # Vercel लिमिट के अंदर रहते हुए आर्टिकल्स को स्कैन करना (अधिकतम 40 आर्टिकल्स तक सुरक्षित स्कैन)
    scanned_records = []
    for link in article_links[:40]:
        article_data = deep_scan_article(link)
        scanned_records.append(article_data)
        
    # केवल वे रिकॉर्ड्स जिनमें ऑथर के असली ईमेल मिले हैं
    valid_records = [r for r in scanned_records if len(r['emails']) > 0]
    
    # सभी यूनिक ईमेल्स की लिस्ट एकत्र करना
    all_flat_emails = []
    for r in scanned_records:
        for em in r['emails']:
            if em not in all_flat_emails:
                all_flat_emails.append(em)
                
    return jsonify({
        'success': True,
        'total_articles_found': total_found,
        'valid_articles_count': len(valid_records),
        'total_emails_count': len(all_flat_emails),
        'all_unique_emails': all_flat_emails,
        'records': scanned_records  # सारे आर्टिकल्स दिखाएगा ताकि आप मिलान कर सकें
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
