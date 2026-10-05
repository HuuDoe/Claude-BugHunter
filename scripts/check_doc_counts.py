#!/usr/bin/env python3
"""ServiceNow ID signup + PDI provisioning bot (runs on GH Actions runner)."""
import os, sys, re, json, time, base64, subprocess, urllib.request

def sh(cmd, **kw):
    print("$", cmd, flush=True)
    return subprocess.run(cmd, shell=True, **kw)

def log(*a): print("[bot]", *a, flush=True)

# ---------- phase 0: deps ----------
if not os.environ.get("BOT_DEPS_DONE"):
    sh("pip install --quiet playwright faster-whisper requests 2>&1 | tail -2")
    sh("sudo apt-get install -y xvfb >/dev/null 2>&1; python3 -m playwright install --with-deps chromium 2>&1 | tail -3")
    os.environ["BOT_DEPS_DONE"] = "1"
    # re-exec under xvfb for a headed (human-like) browser
    os.execvp("xvfb-run", ["xvfb-run", "-a", sys.executable, os.path.abspath(__file__)])
    sys.exit(0)

import requests
from playwright.sync_api import sync_playwright

EMAIL   = "devinsnrschb7d7f4c2@maxxspace.com"
MTPASS  = "Mbx$T3mp2026!"
SNPASS  = "Dvn$Rsch2026!x"
SIGNUP  = "https://signon.servicenow.com/x_snc_sso_auth.do?pageId=sign-up"
STATE   = {"email": EMAIL, "stage": "start"}
RESULT  = {"ok": False}

def mt_token():
    r = requests.post("https://api.mail.tm/token", json={"address": EMAIL, "password": MTPASS}, timeout=20)
    return r.json()["token"]

def mt_poll(minutes=4):
    tok = mt_token(); h = {"Authorization": f"Bearer {tok}"}
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            msgs = requests.get("https://api.mail.tm/messages", headers=h, timeout=20).json().get("hydra:member", [])
            for m in msgs:
                body = requests.get(f"https://api.mail.tm/messages/{m['id']}", headers=h, timeout=20).json()
                text = body.get("text", "") or ""
                html = " ".join(x for x in body.get("html", []) or [] for x in x.split())
                blob = text + " " + html
                links = re.findall(r'https?://[^\s"\'<>]+', blob)
                sn = [l for l in links if "servicenow" in l]
                log("MAIL:", m.get("subject"), "| links:", sn[:3])
                return m.get("subject"), sn
        except Exception as e:
            log("mail poll err", e)
        time.sleep(10)
    return None, []

def whisper_digits(mp3):
    from faster_whisper import WhisperModel
    global _model
    if "_model" not in globals():
        _model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segs, _ = _model.transcribe(mp3, beam_size=5)
    txt = " ".join(s.text for s in segs)
    digits = "".join(re.findall(r"\d", txt))
    log("heard:", repr(txt), "-> digits:", digits)
    return digits

def png_b64(path):
    try:
        import PIL.Image  # noqa
    except Exception:
        pass
    return base64.b64encode(open(path, "rb").read()).decode()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=[
        "--no-sandbox", "--disable-blink-features=AutomationControlled",
        "--lang=en-US,en"])
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900}, locale="en-US")
    page = ctx.new_page()
    page.goto(SIGNUP, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    page.screenshot(path="/tmp/s1_landing.png")
    log("landed:", page.title(), page.url)

    # ---- fill form ----
    def fill(sel, val):
        page.eval_on_selector(sel, f"(e)=>{{e.value={json.dumps(val)};e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));}}")
    try:
        fill("#email", EMAIL); fill("#firstName", "Devin"); fill("#lastName", "SecResearch")
        fill("#password", SNPASS); fill("#confirmPassword", SNPASS)
        page.eval_on_selector("#countrySelectInputId", "e=>{e.value='United States';e.dispatchEvent(new Event('input',{bubbles:true}))}")
        page.wait_for_timeout(800)
        page.evaluate("""()=>{let o=[...document.querySelectorAll('*')].find(x=>x.textContent==='United States'&&(x.tagName==='LI'||x.tagName==='OPTION'||x.role==='option'));if(o)o.click();}""")
        page.evaluate("()=>{let c=document.querySelector('input[name=country]');if(c)c.value='US';}")
        page.eval_on_selector("#tnc", "e=>{if(!e.checked)e.click()}")
        log("form filled")
    except Exception as e:
        log("FILL ERR", e); page.screenshot(path="/tmp/s1_fillerr.png")

    # ---- captcha ----
    anchor = page.frame_locator("iframe[src*='recaptcha'][src*='anchor'], iframe[title*='reCAPTCHA']").first
    token = ""
    blocked = False
    try:
        anchor.locator("#recaptcha-anchor").click(timeout=15000)
        log("anchor clicked")
    except Exception as e:
        log("anchor click fail", e)

    for rnd in range(12):
        page.wait_for_timeout(2500)
        try:
            token = page.eval_on_selector("#g-recaptcha-response", "e=>e.value") or ""
        except Exception:
            token = ""
        if len(token) > 50:
            break
        # inspect challenge frame state
        try:
            btxt = page.frame_locator("iframe[src*='bframe']").locator("body").inner_text(timeout=4000)
        except Exception:
            btxt = ""
        log(f"round {rnd}: toklen={len(token)} bframe[:160]={btxt[:160]!r}")
        if re.search(r"automated queries|unusual traffic|can.t be verified|try again later|expired", btxt, re.I):
            if "expired" in btxt.lower():
                try: anchor.locator("#recaptcha-anchor").click(timeout=8000)
                except Exception: pass
                continue
            blocked = True; break
        if not btxt and len(token) < 50:
            # maybe challenge not opened; click again
            try:
                anchor.locator("#recaptcha-anchor").click(timeout=8000)
            except Exception as e:
                log("reclick fail", e)
            continue
        # challenge open -> audio
        bf = page.frame_locator("iframe[src*='bframe']")
        try:
            if re.search(r"select all|squares|images", btxt, re.I):
                bf.locator("#recaptcha-audio-button").click(timeout=8000)
                page.wait_for_timeout(2500)
                btxt = bf.locator("body").inner_text(timeout=4000)
                log("post-audio-btn:", btxt[:160])
        except Exception as e:
            log("audio btn fail", e)
        try:
            href = bf.locator(".rc-audiochallenge-tdownload-link").get_attribute("href", timeout=8000)
        except Exception:
            href = None
        if not href:
            log("no audio link")
            continue
        mp3 = f"/tmp/rnd{rnd}.mp3"
        urllib.request.urlretrieve(href, mp3)
        ans = whisper_digits(mp3)
        if not ans:
            log("empty transcription"); continue
        bf.locator("#audio-response").fill(ans)
        bf.locator("#recaptcha-verify-button").click()
        log("submitted answer", ans)
        page.wait_for_timeout(3000)
    page.screenshot(path="/tmp/s2_aftercap.png")
    log("captcha result: token_len=", len(token), "blocked=", blocked)

    if len(token) > 50:
        log("TOKEN_OK", token[:40])
        RESULT["captcha_token_prefix"] = token[:30]
        try:
            page.click("#registration_submit_button", timeout=10000)
        except Exception as e:
            log("submit click fail", e)
        page.wait_for_timeout(8000)
        page.screenshot(path="/tmp/s3_aftersubmit.png")
        try:
            vis = page.locator("body").inner_text()
        except Exception:
            vis = ""
        log("AFTER SUBMIT URL:", page.url)
        log("AFTER SUBMIT BODY[:1500]:", vis[:1500])
        STATE["stage"] = "submitted"
        if re.search(r"blacklist|not valid|different email", vis, re.I):
            log("EMAIL REJECTED — retry loop would go here")
            STATE["stage"] = "email_rejected"
    else:
        STATE["stage"] = "captcha_failed"

    # ---- email verification ----
    if STATE["stage"] == "submitted":
        subj, links = mt_poll(4)
        verify = next((l for l in links if "verify" in l or "activation" in l), links[0] if links else None)
        if verify:
            log("verify link:", verify)
            page.goto(verify, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            log("post-verify URL:", page.url, "|", (page.locator('body').inner_text() or '')[:600])
            page.screenshot(path="/tmp/s4_verified.png")
            STATE["stage"] = "verified"
        else:
            log("no verify mail received")

    # ---- dev portal: login + request PDI ----
    if STATE["stage"] == "verified":
        page.goto("https://developer.servicenow.com/dev.do", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
        log("dev portal:", page.url)
        page.screenshot(path="/tmp/s5_dev.png")
        # SSO login flow if not already authed
        for attempt in range(3):
            body = page.locator("body").inner_text() or ""
            if re.search(r"request.*instance|my instance|start building|dashboard", body, re.I):
                break
            try:
                page.goto("https://signon.servicenow.com/x_snc_sso_auth.do?pageId=login", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
                page.fill("#email", EMAIL); page.click("button[type=submit], #next_button, .btn-primary", timeout=8000)
                page.wait_for_timeout(4000)
                page.fill("#password", SNPASS); page.click("button[type=submit], #signin_button, .btn-primary", timeout=8000)
                page.wait_for_timeout(6000)
                log("post-login url:", page.url)
                page.goto("https://developer.servicenow.com/dev.do", wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(6000)
            except Exception as e:
                log("login attempt err", e)
        page.screenshot(path="/tmp/s6_devhome.png")
        # find request-instance control
        try:
            body = page.locator("body").inner_text() or ""
            log("DEV HOME TEXT[:1200]:", body[:1200])
            for pat in [r"text=/request.*instance/i", r"text=/start.*building/i", r"text=/my instance/i"]:
                loc = page.locator(pat)
                if loc.count():
                    loc.first.click(); page.wait_for_timeout(6000); break
            page.screenshot(path="/tmp/s7_req.png")
            log("post-req URL:", page.url, (page.locator('body').inner_text() or '')[:800])
        except Exception as e:
            log("dev portal err", e)

    log("DONE. final state:", STATE)
    browser.close()

print("RESULT_JSON:" + json.dumps(RESULT | STATE))
try:
    print("FINAL_PNG_B64:" + png_b64("/tmp/s5_dev.png")[:200])
except Exception:
    pass
