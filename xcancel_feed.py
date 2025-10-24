# xcancel_feed.py — minimal, fast Nitter/Xcancel feed with media, stats, quotes, file-based usernames
#
# Deps:
#   pip install fastapi uvicorn beautifulsoup4 python-dateutil playwright
#   playwright install chromium
#
# Run:
#   uvicorn xcancel_feed:app --host 127.0.0.1 --port 8000
# Open:
#   http://127.0.0.1:8000
#
# Notes:
# - Default base: https://nitter.poast.org (override with XCANCEL_BASE env or ?base=)
# - Usernames file: usernames.txt in CWD or alongside this file (override with XCANCEL_USERNAMES or ?users_file=)
# - We fetch HTML/RSS with Playwright only (we block heavy assets there; the *browser UI* loads images/videos directly)

import os, re, asyncio, pathlib
from typing import List, Optional, Dict, Any, Tuple
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
from playwright.async_api import async_playwright

# ------------------- Config -------------------
DEFAULT_BASE = os.getenv("XCANCEL_BASE", "https://nitter.poast.org").rstrip("/")
DEFAULT_USERNAMES = os.getenv("XCANCEL_USERNAMES", "usernames.txt")
POOL_SIZE = int(os.getenv("PW_POOL", "3"))

# ------------------- FastAPI -------------------
app = FastAPI(title="Nitter/Xcancel Feed (minimal)")

# ------------------- Timestamp utils -------------------
EPOCH_RE = re.compile(r"^\d{10}(?:\d{3})?$")  # 10 or 13 digits
SNOWFLAKE_RE = re.compile(r"/status/(\d+)")

def to_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None: return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def epoch_to_dt_aware(s: str) -> datetime:
    ts = int(s[:10])
    return datetime.utcfromtimestamp(ts).replace(tzinfo=timezone.utc)

def snowflake_to_dt_aware(snowflake: str) -> datetime:
    n = int(snowflake)
    ms = (n >> 22) + 1288834974657
    return datetime.utcfromtimestamp(ms / 1000.0).replace(tzinfo=timezone.utc)

def parse_dt_strict(s: str) -> datetime:
    s = (s or "").strip()
    if not s: raise ValueError("empty")
    if EPOCH_RE.match(s): return epoch_to_dt_aware(s)
    s = re.sub(r"\s+", " ", s.replace("·", " ").replace("\u2022", " ").strip())
    fmts = [
        "%Y-%m-%d %H:%M %Z",
        "%Y-%m-%d %H:%M:%S %Z",
        "%m/%d/%Y, %I:%M:%S %p",
        "%b %d, %Y, %I:%M %p %Z",
        "%b %d, %Y %I:%M %p %Z",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    dt = dateparser.parse(s)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

# ------------------- Playwright pool -------------------
_playwright = None
_browser = None
_context = None
_page_pool: asyncio.Queue | None = None
INIT_LOCK = asyncio.Lock()

async def _launch_browser_once():
    global _playwright, _browser, _context, _page_pool
    async with INIT_LOCK:
        if _browser: return
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
        _context = await _browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/127.0.0.0 Safari/537.36"),
            locale="en-US",
            java_script_enabled=True,
        )
        # block heavy assets for the *server fetches*
        await _context.route("**/*", lambda route: (
            route.abort() if route.request.resource_type in {"image","media","font"} else route.continue_()
        ))
        _page_pool = asyncio.Queue()
        for _ in range(POOL_SIZE):
            page = await _context.new_page()
            await _page_pool.put(page)

async def _with_page(fn):
    await _launch_browser_once()
    page = await _page_pool.get()
    try:
        return await fn(page)
    finally:
        await _page_pool.put(page)

async def fetch_html(url: str, timeout_ms: int = 12000) -> Tuple[int, str]:
    async def run(page):
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        html = await page.content()
        return (resp.status if resp else 200), html
    try:
        return await _with_page(run)
    except Exception:
        return 0, ""

@app.on_event("shutdown")
async def _shutdown():
    global _playwright, _browser, _context
    try:
        if _context: await _context.close()
        if _browser: await _browser.close()
        if _playwright: await _playwright.stop()
    except Exception:
        pass

# ------------------- Helpers -------------------
def looks_like_challenge(body: str) -> bool:
    if not body or not body.strip(): return True
    t = body.lower()
    return any(s in t for s in [
        "just a moment", "checking your browser", "cloudflare",
        "/cdn-cgi/challenge", "verifying your browser", "403 forbidden"
    ])

def read_textfile_candidates(path_str: str) -> Optional[str]:
    """
    Try a path, then CWD, then alongside this file.
    """
    p = pathlib.Path(path_str)
    if p.exists(): return p.read_text(encoding="utf-8")
    # CWD
    p2 = pathlib.Path.cwd() / path_str
    if p2.exists(): return p2.read_text(encoding="utf-8")
    # alongside this file
    here = pathlib.Path(__file__).resolve().parent
    p3 = here / path_str
    if p3.exists(): return p3.read_text(encoding="utf-8")
    return None

def load_usernames(path: str) -> List[str]:
    txt = read_textfile_candidates(path) or ""
    lines = [ln.strip() for ln in txt.splitlines()]
    return [ln.lstrip("@") for ln in lines if ln and not ln.startswith("#")]

def _get_int(s: Optional[str]) -> Optional[int]:
    if not s: return None
    s = s.strip()
    # drop non-digits except commas
    s = re.sub(r"[^\d,]", "", s).replace(",", "")
    try:
        return int(s) if s else None
    except Exception:
        return None

def _first_url_from_srcset(srcset: str) -> Optional[str]:
    if not srcset: return None
    part = srcset.split(",")[0].strip()
    return part.split(" ")[0].strip() if part else None

# ------------------- Parsing -------------------
def parse_rss(xml_text: str, username: str, base_host: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")
    out = []
    for it in items:
        title_el = it.find("title")
        link_el  = it.find("link")
        date_el  = it.find("pubDate")
        content = (title_el.text or "").strip() if title_el is not None else ""
        if not content:
            desc_el = it.find("description")
            if desc_el is not None and desc_el.text:
                content = BeautifulSoup(desc_el.text, "html.parser").get_text(" ", strip=True)
        if not content: continue
        ts = parse_dt_strict(date_el.text.strip()) if (date_el is not None and date_el.text) else to_aware_utc(datetime.utcnow())
        out.append({
            "username": username,
            "timestamp": ts.isoformat(),
            "content": content,
            "link": (link_el.text.strip() if (link_el is not None and link_el.text) else None),
            "source": base_host.replace("https://","").replace("http://",""),
            "images": [], "videos": [],
            "replies": None, "reposts": None, "likes": None, "views": None,
            "quote": None
        })
    return out

def parse_html(html: str, username: str, base_host: str) -> List[Dict[str, Any]]:
    if not html or looks_like_challenge(html): return []
    soup = BeautifulSoup(html, "html.parser")
    posts: List[Dict[str, Any]] = []

    containers = (
        soup.select("div.timeline-item") or
        soup.select("article") or
        soup.select("div.tweet, div.status, li.status, div.tweet-container, div.tweet-item, div.tweet-content")
    )
    if not containers:
        containers = [n for t in soup.select("time") for n in (t.parent, getattr(t.parent, "parent", None)) if n]

    for el in containers:
        # ----- timestamp (data-time -> title -> time attrs -> snowflake -> now)
        ts = None
        dt_attr = el.select_one("[data-time]")
        if dt_attr and dt_attr.get("data-time"):
            try: ts = epoch_to_dt_aware(dt_attr["data-time"])
            except: ts = None
        if ts is None:
            t = el.select_one(".tweet-date a[title]")
            if t and t.has_attr("title"):
                try: ts = parse_dt_strict(t["title"])
                except: ts = None
        if ts is None:
            time_tag = el.find("time")
            if time_tag:
                for attr in ("data-time","datetime","title"):
                    if time_tag.has_attr(attr):
                        try: ts = parse_dt_strict(time_tag[attr]); break
                        except: pass
        if ts is None:
            sf = None
            for a in el.find_all("a", href=True):
                m = SNOWFLAKE_RE.search(a["href"])
                if m: sf = m.group(1); break
            if sf:
                try: ts = snowflake_to_dt_aware(sf)
                except: ts = None
        if ts is None: ts = to_aware_utc(datetime.utcnow())

        # ----- content
        text_nodes = el.select(".tweet-content, .content, .status-content, .tweet-text, p, span")
        content = " ".join([n.get_text(" ", strip=True) for n in text_nodes if n.get_text(strip=True)]).strip()
        if not content:
            content = el.get_text(" ", strip=True)
        if not content: continue

        # ----- link
        link = None
        for a in el.find_all("a", href=True):
            href = a["href"]
            if "/status/" in href or href.startswith(f"/{username}/status"):
                link = href if href.startswith("http") else f"{base_host.rstrip('/')}{href}"
                break

        # ----- media
        images, videos = [], []

        # img tags (src, data-src, srcset)
        for img in el.select("img"):
            src = img.get("src") or img.get("data-src") or None
            if not src:
                ss = img.get("srcset")
                if ss: src = _first_url_from_srcset(ss)
            if not src: continue
            if src.startswith("//"): src = "https:" + src
            elif src.startswith("/"): src = base_host.rstrip("/") + src
            if any(src.lower().endswith(ext) for ext in (".jpg",".jpeg",".png",".webp",".gif",".avif",".jfif",".bmp",".svg")):
                if src not in images: images.append(src)
        # <a class="image"> or /pic/ links that are direct images
        for a in el.select("a.image, a[href*='/pic/']"):
            href = a.get("href") or ""
            if href:
                if href.startswith("//"): href = "https:" + href
                elif href.startswith("/"): href = base_host.rstrip("/") + href
                if any(href.lower().endswith(ext) for ext in (".jpg",".jpeg",".png",".webp",".gif",".avif",".jfif",".bmp",".svg")):
                    if href not in images: images.append(href)

        # videos
        for source in el.select("video source[src]"):
            vsrc = source.get("src") or ""
            if vsrc:
                if vsrc.startswith("//"): vsrc = "https:" + vsrc
                elif vsrc.startswith("/"): vsrc = base_host.rstrip("/") + vsrc
                if vsrc not in videos: videos.append(vsrc)
        for a in el.select("a[href$='.mp4'], a[href*='.mp4?']"):
            href = a.get("href") or ""
            if href:
                if href.startswith("//"): href = "https:" + href
                elif href.startswith("/"): href = base_host.rstrip("/") + href
                if href not in videos: videos.append(href)

        # ----- stats (replies, reposts, likes, views)
        replies = reposts = likes = views = None
        # icon-based siblings (common on Nitter)
        def _sibling_text(sel_icon: str) -> Optional[str]:
            icon = el.select_one(sel_icon)
            if icon and icon.next_sibling and getattr(icon.next_sibling, "get_text", None):
                return icon.next_sibling.get_text(strip=True)
            if icon and icon.parent:
                # sometimes wrapped in <a class="stat">
                txt = icon.parent.get_text(" ", strip=True)
                # drop icon text itself (often empty)
                return txt
            return None

        replies = _get_int(_sibling_text(".icon-comment"))
        reposts = _get_int(_sibling_text(".icon-retweet"))
        likes   = _get_int(_sibling_text(".icon-heart"))
        views   = _get_int(_sibling_text(".icon-eye")) or _get_int(_sibling_text(".icon-stats"))

        # also try data-* attributes if present
        replies = replies or _get_int(el.get("data-replies"))
        reposts = reposts or _get_int(el.get("data-retweets"))
        likes   = likes   or _get_int(el.get("data-likes"))
        views   = views   or _get_int(el.get("data-views"))

        # ----- quote / repost (quoted tweet block)
        quote_text = None
        q_el = el.select_one(".quote, .card.quote, .retweet, .repost-container, blockquote")
        if q_el:
            q_txt = q_el.get_text(" ", strip=True)
            # Keep it concise
            quote_text = q_txt[:500] if q_txt else None

        posts.append({
            "username": username,
            "timestamp": ts.isoformat(),
            "content": content,
            "link": link,
            "source": base_host.replace("https://","").replace("http://",""),
            "images": images[:4], "videos": videos[:2],
            "replies": replies, "reposts": reposts, "likes": likes, "views": views,
            "quote": quote_text
        })
    return posts

# ------------------- Fetchers -------------------
async def fetch_user(base_host: str, username: str) -> List[Dict[str, Any]]:
    base = base_host.rstrip("/")
    # RSS first
    s1, t1 = await fetch_html(f"{base}/{username}/rss", timeout_ms=8000)
    if s1 == 200 and t1 and not looks_like_challenge(t1):
        posts = parse_rss(t1, username, base)
        if posts: return posts
    # HTML fallback
    s2, t2 = await fetch_html(f"{base}/{username}", timeout_ms=12000)
    if s2 == 200 and t2 and not looks_like_challenge(t2):
        return parse_html(t2, username, base)
    return []

async def fetch_many(base_host: str, users: List[str], concurrency: int = 24) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)
    async def task(u: str):
        async with sem:
            return await fetch_user(base_host, u)
    results = await asyncio.gather(*[task(u) for u in users], return_exceptions=True)
    out: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception): continue
        out.extend(r or [])
    return out

# ------------------- UI -------------------
INDEX_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Feed</title>
  <style>
    :root{ --bg:#0b0f14; --card:#121820; --text:#e5eef5; --muted:#9fb3c8; --accent:#1da1f2; --border:#1b2530; }
    body{ margin:0; background:var(--bg); color:var(--text); font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, Arial; }
    header{ position:sticky; top:0; background:rgba(11,15,20,.75); backdrop-filter: blur(10px); border-bottom:1px solid var(--border); }
    header .wrap{ display:flex; gap:12px; align-items:center; padding:12px 18px; }
    h1{ font-size:16px; margin:0; }
    .container{ max-width:960px; margin:0 auto; padding:16px; }
    .controls{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:12px; }
    input, select{ background:#0f141a; color:var(--text); border:1px solid var(--border); border-radius:10px; padding:8px 10px; }
    button{ background:var(--accent); color:white; border:none; border-radius:10px; padding:8px 12px; cursor:pointer; }
    .feed{ display:flex; flex-direction:column; gap:10px; }
    .post{ background:var(--card); border:1px solid var(--border); border-radius:16px; padding:12px; display:flex; flex-direction:column; gap:8px; cursor:pointer; }
    .meta{ display:flex; gap:12px; align-items:center; font-size:13px; color:var(--muted); }
    .user{ font-weight:600; color:var(--text); }
    .content{ white-space:pre-wrap; line-height:1.45; font-size:15px; }
    .media{ display:grid; grid-template-columns:repeat(2,1fr); gap:6px; }
    .media img, .media video{ width:100%; height:100%; object-fit:cover; border-radius:12px; background:#0f141a; border:1px solid var(--border); }
    .stats{ display:flex; gap:14px; color:#b6c5d6; font-size:13px; }
    .stat{ display:inline-flex; align-items:center; gap:6px; }
    .quote{ border-left:3px solid #2b3440; background:#111821; padding:10px; border-radius:10px; color:#cfe0f1; font-size:14px; }
    .chip{ background:#1b2430; border:1px solid var(--border); color:#9fb3c8; padding:4px 8px; border-radius:999px; font-size:12px; margin-left:auto; }
  </style>
</head>
<body>
  <header><div class="wrap"><h1>Feed</h1><div id="status" class="chip">Idle</div></div></header>
  <div class="container">
    <div class="controls">
      <input id="userInput" placeholder="add username (no @)"/>
      <button id="addBtn">Add</button>
      <label>Poll (ms)</label><input id="pollInterval" value="3000" style="width:90px"/>
      <label>Max Age</label><input id="maxAgeVal" type="number" min="0" value="0" style="width:90px"/>
      <select id="maxAgeUnit"><option value="seconds">seconds</option><option value="minutes" selected>minutes</option><option value="hours">hours</option><option value="days">days</option></select>
      <button id="applyAge">Set</button>
      <label>Base</label><input id="baseHost" placeholder="(default from server)" style="width:260px"/>
      <button id="startBtn">Start</button><button id="stopBtn">Stop</button>
    </div>

    <div><strong>Accounts:</strong> <span id="accounts"></span></div>
    <div id="feed" class="feed"></div>
  </div>

<script>
let accounts = [];
let pollHandle = null;
let maxAgeSeconds = 0;

function renderAccounts(){
  const el = document.getElementById("accounts"); el.innerHTML = "";
  accounts.forEach((a,idx) => {
    const s = document.createElement("span");
    s.textContent = (idx? ", " : "") + a;
    s.title = "Click to remove";
    s.style.cursor = "pointer";
    s.onclick = () => { accounts = accounts.filter(x=>x!==a); renderAccounts(); };
    el.appendChild(s);
  });
}
document.getElementById("addBtn").onclick = () => {
  const v = document.getElementById("userInput").value.trim().replace(/^@/,"");
  if(v && !accounts.includes(v)){ accounts.push(v); renderAccounts(); }
  document.getElementById("userInput").value = "";
};
document.getElementById("applyAge").onclick = () => {
  const val = parseInt(document.getElementById("maxAgeVal").value || "0");
  const unit = document.getElementById("maxAgeUnit").value;
  let s = 0; if(val>0){ if(unit==="seconds") s=val; if(unit==="minutes") s=val*60; if(unit==="hours") s=val*3600; if(unit==="days") s=val*86400; }
  maxAgeSeconds = s;
  document.getElementById("status").textContent = s ? `MaxAge ${s}s` : "No MaxAge";
};

async function fetchOnce(){
  if(accounts.length===0) return;
  document.getElementById("status").textContent = "Fetching…";
  try{
    const q = new URLSearchParams();
    q.set("users", accounts.join(","));
    const base = (document.getElementById("baseHost").value || "").trim();
    if(base) q.set("base", base);
    if(maxAgeSeconds>0) q.set("max_age_seconds", String(maxAgeSeconds));
    const r = await fetch("/api/feed?" + q.toString());
    const j = await r.json();
    renderFeed(j.posts || []);
    document.getElementById("status").textContent = (j.host || "") + " · " + new Date().toLocaleTimeString() + " · " + (j.count||0) + " posts";
  }catch(e){
    console.error(e);
    document.getElementById("status").textContent = "Error";
  }
}

function statSpan(icon, val){
  const s = document.createElement("span");
  s.className = "stat";
  s.textContent = `${icon} ${val ?? 0}`;
  return s;
}

function renderFeed(posts){
  const feed = document.getElementById("feed"); feed.innerHTML = "";
  if(!posts.length){ const d=document.createElement("div"); d.className="post"; d.textContent="No posts."; feed.appendChild(d); return; }
  for(const p of posts){
    const card = document.createElement("div"); card.className="post";
    card.onclick = () => { if(p.link) window.open(p.link,"_blank"); };

    const meta = document.createElement("div"); meta.className="meta";
    const user = document.createElement("span"); user.className="user"; user.textContent = p.username;
    const time = document.createElement("span"); time.textContent = new Date(p.timestamp).toLocaleString();
    const src  = document.createElement("span"); src.style.marginLeft = "auto"; src.textContent = p.source || "";
    meta.appendChild(user); meta.appendChild(time); meta.appendChild(src);

    const content = document.createElement("div"); content.className="content"; content.textContent = p.content;

    card.appendChild(meta); card.appendChild(content);

    if((p.images&&p.images.length)||(p.videos&&p.videos.length)){
      const m=document.createElement("div"); m.className="media";
      (p.images||[]).forEach(src=>{const i=document.createElement("img"); i.loading="lazy"; i.decoding="async"; i.src=src; m.appendChild(i);});
      (p.videos||[]).forEach(src=>{const v=document.createElement("video"); v.controls=true; v.preload="metadata"; const s=document.createElement("source"); s.src=src; s.type="video/mp4"; v.appendChild(s); m.appendChild(v);});
      card.appendChild(m);
    }

    const stats = document.createElement("div"); stats.className = "stats";
    stats.appendChild(statSpan("💬", p.replies));
    stats.appendChild(statSpan("🔁", p.reposts));
    stats.appendChild(statSpan("❤️", p.likes));
    if(p.views != null) stats.appendChild(statSpan("👁️", p.views));
    card.appendChild(stats);

    if(p.quote){
      const q = document.createElement("div"); q.className = "quote"; q.textContent = p.quote;
      card.appendChild(q);
    }

    feed.appendChild(card);
  }
}

document.getElementById("startBtn").onclick = () => {
  if(pollHandle) return;
  const ms = parseInt(document.getElementById("pollInterval").value) || 3000;
  pollHandle = setInterval(fetchOnce, ms); fetchOnce();
};
document.getElementById("stopBtn").onclick = () => { if(!pollHandle) return; clearInterval(pollHandle); pollHandle=null; document.getElementById("status").textContent="Stopped"; };
</script>
</body>
</html>
"""

# ------------------- API -------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)

@app.get("/health", response_class=PlainTextResponse)
async def health():
    return PlainTextResponse("ok")

def _parse_iso_aware(s: str) -> datetime:
    dt = dateparser.parse(s)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _resolve_user_list(users_param: str, users_file_param: Optional[str]) -> List[str]:
    if users_param.strip():
        return [u.strip().lstrip("@") for u in users_param.split(",") if u.strip()]
    path = users_file_param or DEFAULT_USERNAMES
    names = load_usernames(path)
    return names

@app.get("/api/feed")
async def api_feed(
    users: Optional[str] = "",
    base: Optional[str] = None,
    max_age_seconds: Optional[int] = 0,
    users_file: Optional[str] = Query(default=None, description="Optional path to usernames file"),
):
    base_host = (base or DEFAULT_BASE).rstrip("/")
    user_list = _resolve_user_list(users, users_file)
    if not user_list:
        return JSONResponse({"posts": [], "count": 0, "host": base_host, "note": "no users"})

    posts = await fetch_many(base_host, user_list, concurrency=24)

    # newest-first + MaxAge
    for p in posts:
        p["_ts"] = _parse_iso_aware(p["timestamp"])
    posts.sort(key=lambda x: x["_ts"], reverse=True)

    if max_age_seconds and int(max_age_seconds) > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=int(max_age_seconds))
        posts = [p for p in posts if p["_ts"] >= cutoff]

    out = [{k: p.get(k) for k in ("username","timestamp","content","link","source","images","videos","replies","reposts","likes","views","quote")} for p in posts]
    return JSONResponse({"posts": out, "count": len(out), "host": base_host})

