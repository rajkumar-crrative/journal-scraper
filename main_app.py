import os
import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

# फालतू एडमिन/जनरल ईमेल्स को रोकने के लिए ब्लॉक-लिस्ट
BLOCKED_EMAIL_PREFIXES = (
    'info@', 'support@', 'editor@', 'chief@', 'help@', 
    'privacy@', 'contact@', 'admin@', 'queries@', 'editorial@', 'nepjol@', 'ubiquitypress@'
)

# फालतू पेज लिंक्स को रोकने के लिए ब्लॉक-लिस्ट
BLOCKED_URL_KEYWORDS = [
    '/about', '/editorialteam', '/privacy', '/submissions', 
    '/contact', '/announcements', '/indexing', '/search', '/index'
]

def is_valid_author_email(email):
    """सिर्फ असली कॉरेस्पोंडिंग ऑथर के ईमेल को अनुमति दें"""
    email_lower = email.lower()
    if any(email_lower.startswith(prefix) for prefix in BLOCKED_EMAIL_PREFIXES):
        return False
    return True

def fetch_page(url):
    """सुरक्षित तरीके से किसी भी URL का कंटेंट निकालें (Vercel Compatible)"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None

def extract_emails_from_text(text):
    """टेक्स्ट में से केवल सही फॉर्मेट वाले ईमेल्स छांटें"""
    if not text:
        return []
    raw_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    valid_emails = list(set([e.lower() for e in raw_emails if is_valid_author_email(e)]))
    return valid_emails

def scrape_article(article_url):
    """केवल सिंगल रिसर्च आर्टिकल पेज को डीप-स्कैन करें"""
    html = fetch_page(article_url)
    if not html:
        return {'url': article_url, 'title': 'Unable to access article', 'emails': []}
    
    soup = BeautifulSoup(html, 'html.parser')
    
    # शीर्षक निकालें
    title_tag = soup.find('h1') or soup.find('title')
    title = title_tag.get_text().strip() if title_tag else article_url
    
    # ईमेल एक्सट्रैक्शन
    emails = extract_emails_from_text(html)
    
    return {
        'url': article_url,
        'title': title,
        'emails': emails
    }

@app.route('/')
def home():
    return render_template_string('''
    <!DOCTYPE html>
    <html lang="hi">
    <head>
        <meta charset="UTF-8">
        <title>Galaxy Enterprise Email Tracker Engine</title>
        <style>
            :root {
                --bg-color: #0b0f19;
                --card-bg: rgba(23, 32, 54, 0.7);
                --neon-cyan: #22d3ee;
                --neon-pink: #ec4899;
                --glass-border: rgba(255, 255, 255, 0.1);
                --text-muted: #94a3b8;
            }
            body {
                background: var(--bg-color);
                color: #fff;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                margin: 0; padding: 20px;
            }
            .container {
                max-width: 900px;
                margin: 0 auto;
                background: var(--card-bg);
                backdrop-filter: blur(16px);
                border: 1px solid var(--glass-border);
                border-radius: 16px;
                padding: 30px;
                box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
            }
            h2 { text-align: center; color: var(--neon-cyan); margin-bottom: 5px; }
            p.sub { text-align: center; color: var(--neon-pink); font-size: 14px; margin-top: 0; }
            .input-group { margin-bottom: 20px; }
            label { display: block; margin-bottom: 8px; font-weight: bold; color: var(--text-muted); }
            input[type="text"] {
                width: 100%; padding: 12px;
                border-radius: 8px; border: 1px solid var(--glass-border);
                background: rgba(15, 23, 42, 0.8); color: #fff;
                box-sizing: border-box; font-size: 14px;
            }
            button.btn {
                width: 100%; padding: 14px;
                background: linear-gradient(135deg, #6366f1, var(--neon-pink));
                border: none; border-radius: 8px; color: #fff;
                font-size: 16px; font-weight: bold; cursor: pointer;
                transition: 0.3s ease;
            }
            button.btn:hover { opacity: 0.9; transform: translateY(-1px); }
            .terminal {
                margin-top: 25px; background: rgba(15, 23, 42, 0.9);
                border-radius: 8px; padding: 20px; border: 1px solid var(--glass-border);
            }
            .stats-box {
                display: flex; justify-content: space-between;
                background: rgba(34, 211, 238, 0.1); border: 1px solid var(--neon-cyan);
                padding: 12px 18px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; flex-wrap: wrap; gap: 10px;
            }
            .article-card {
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid var(--glass-border);
                padding: 12px; border-radius: 8px; margin-bottom: 12px;
            }
            .email-badge {
                display: inline-block; background: rgba(34, 211, 238, 0.2);
                color: var(--neon-cyan); border: 1px solid var(--neon-cyan);
                padding: 4px 10px; border-radius: 20px; font-size: 12px; margin: 4px 4px 0 0;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>🚀 Galaxy Enterprise Email Tracker</h2>
            <p class="sub">Vercel Optimized Deep-Scan Engine</p>
            
            <div class="input-group">
                <label>Target Journal Issue / Volume URL:</label>
                <input type="text" id="targetUrl" placeholder="https://www.nepjol.info/index.php/jafu/issue/view/...">
            </div>
            
            <button class="btn" onclick="startScraping()">Launch Deep-Scan Engine</button>
            
            <div class="terminal" id="terminalBox" style="display:none;">
                <div id="statsHeader" class="stats-box"></div>
                <div id="resultsList"></div>
            </div>
        </div>

        <script>
            async function startScraping() {
                const url = document.getElementById('targetUrl').value.trim();
                if(!url) return alert('कृपया जर्नल का URL डालें!');
                
                const term = document.getElementById('terminalBox');
                const stats = document.getElementById('statsHeader');
                const resList = document.getElementById('resultsList');
                
                term.style.display = 'block';
                stats.innerHTML = 'Scanning target issue and analyzing research articles...';
                resList.innerHTML = '<p style="color: var(--neon-cyan);">Analyzing articles & extracting author emails...</p>';
                
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
                    
                    stats.innerHTML = `
                        <span><b>Total Articles Found:</b> ${data.total_articles_found}</span>
                        <span><b>Articles with Emails:</b> ${data.valid_articles_count}</span>
                        <span><b>Total Emails Extracted:</b> ${data.total_emails_count}</span>
                    `;
                    
                    resList.innerHTML = '';
                    if(data.records.length === 0) {
                        resList.innerHTML = '<p style="color: #f87171; text-align:center;">कोई भी वैध ऑथर ईमेल नहीं मिला।</p>';
                        return;
                    }
                    
                    data.records.forEach((rec, idx) => {
                        let emailBadges = rec.emails.map(e => `<span class="email-badge">${e}</span>`).join('');
                        resList.innerHTML += `
                            <div class="article-card">
                                <b>[${idx+1}] ${rec.title}</b><br>
                                <a href="${rec.url}" target="_blank" style="color: var(--text-muted); font-size: 11px;">🔗 ${rec.url}</a>
                                <div style="margin-top: 8px;">${emailBadges}</div>
                            </div>
                        `;
                    });
                    
                } catch(err) {
                    stats.innerHTML = `<span style="color: #f87171;">Connection Failed: ${err.message}</span>`;
                }
            }
        </script>
    </body>
    </html>
    ''')

@app.route('/scan', methods=['POST'])
def scan_journal():
    data = request.get_json()
    issue_url = data.get('url', '').strip()
    
    if not issue_url:
        return jsonify({'success': False, 'message': 'Invalid URL'})
        
    issue_html = fetch_page(issue_url)
    if not issue_html:
        return jsonify({'success': False, 'message': 'Target website is unreachable.'})
        
    soup = BeautifulSoup(issue_html, 'html.parser')
    
    article_links = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        if any(keyword in href.lower() for keyword in ['/article/view/', '/articles/', '/pdf/']):
            if not any(blocked in href.lower() for blocked in BLOCKED_URL_KEYWORDS):
                full_url = href if href.startswith('http') else os.path.dirname(issue_url) + '/' + href
                if full_url not in article_links:
                    article_links.append(full_url)
                    
    total_found = len(article_links)
    
    if total_found == 0:
        emails = extract_emails_from_text(issue_html)
        return jsonify({
            'success': True,
            'total_articles_found': 1,
            'valid_articles_count': 1 if emails else 0,
            'total_emails_count': len(emails),
            'records': [{'title': 'Issue Landing Page', 'url': issue_url, 'emails': emails}] if emails else []
        })
        
    # Vercel पर टाइमआउट से बचने के लिए इसे सिंक्रोनस तरीके से प्रोसेस किया गया है
    results = [scrape_article(url) for url in article_links[:30]]
    
    valid_records = [r for r in results if len(r['emails']) > 0]
    all_emails_flat = [e for r in valid_records for e in r['emails']]
    
    return jsonify({
        'success': True,
        'total_articles_found': total_found,
        'valid_articles_count': len(valid_records),
        'total_emails_count': len(set(all_emails_flat)),
        'records': valid_records
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
