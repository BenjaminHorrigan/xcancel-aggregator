# xcancel_feed.py
#
# Usage:
#   1. pip install fastapi uvicorn httpx beautifulsoup4 python-dateutil jinja2
#   2. python -m uvicorn xcancel_feed:app --host 127.0.0.1 --port 8000
#   3. Open http://127.0.0.1:8000 in your browser
#
# Notes:
# - This scrapes xcancel.com user pages (or other front-ends with similar HTML structure).
# - It intentionally prioritizes speed / concurrency. Adjust poll interval from the UI.
# - No authentication required. You're responsible for local use only.

import asyncio
import html
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from datetime import datetime, timezone

app = FastAPI(title="XCancel Feed Aggregator (local)")

# Simple HTML page served at /
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>XCancel Aggregated Feed</title>
  <style>
    body { font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; margin: 0; padding: 0; background:#f6f7fb; color:#111;}
    header { background: #0f172a; color: #fff; padding: 14px 20px; display:flex; align-items:center; gap:12px; }
    header h1 { font-size:18px; margin:0; }
    .container { padding: 16px; max-width: 1100px; margin: 0 auto;}
    .controls { display:flex; gap:8px; margin-bottom:12px; align-items: center; flex-wrap:wrap;}
    input[type="text"]{ padding:8px 10px; border-radius:8px; border:1px solid #ddd; min-width:220px;}
    button{ padding:8px 12px; border-radius:8px; border: none; cursor:pointer; background:#111827; color:#fff;}
    button.secondary{ background:#e5e7eb; color:#111; }
    .feed { margin-top:12px; display:flex; flex-direction:column; gap:10px; }
    .post { background:#fff; border-radius:10px; padding:12px; box-shadow:0 2px 6px rgba(16,24,40,0.05); }
    .meta { font-size:13px; color:#374151; margin-bottom:6px; display:flex; gap:12px; align-items:center;}
    .content { white-space:pre-wrap; font-size:15px; color:#0f172a; }
    .account-list { display:flex; gap:6px; flex-wrap:wrap; margin-left:8px;}
    .acct { background:#eef2ff; padding:6px 8px; border-radius:999px; font-size:13px; }
    .small { font-size:13px; color:#6b7280; }
    .controls-right { margin-left:auto; display:flex; gap:8px; align-items:center;}
    label.small { font-size:13px; color:#6b7280; margin-right:6px;}
    #status { font-size:13px; color:#374151; margin-left:8px; }
    a.link { color:#0ea5a4; text-decoration:none; }
  </style>
</head>
<body>
  <header>
    <h1>XCancel Aggregated Feed</h1>
    <div class="small">Local, no X account required</div>
  </header>

  <div class="container">
    <div class="controls">
      <input id="userInput" placeholder="add username (no @). Example: elonmusk"/>
      <button id="addBtn">Add</button>
      <button id="clearBtn" class="secondary">Clear List</button>

      <div class="controls-right">
        <label class="small">Poll (ms)</label>
        <input id="pollInterval" value="2000" style="width:90px; padding:6px; border-radius:6px; border:1px solid #ddd;" />
        <button id="startBtn">Start</button>
        <button id="stopBtn" class="secondary">Stop</button>
        <div id="status">Idle</div>
      </div>
    </div>

    <div>
      <strong>Accounts:</strong>
      <div id="accounts" class="account-list"></div>
    </div>

    <div style="margin-top:12px;">
      <strong>Feed (merged, newest first)</strong>
      <div class="small" style="margin-top:4px;">Click a post's timestamp to open the original link in a new tab (if available).</div>
    </div>

    <div id="feed" class="feed"></div>
  </div>

<script>
let accounts = [];
let polling = false;
let pollHandle = null;

function renderAccounts(){
  const el = document.getElementById("accounts");
  el.innerHTML = "";
  accounts.forEach(a => {
    const chip = document.createElement("span");
    chip.className = "acct";
    chip.textContent = a;
    chip.onclick = () => {
      accounts = accounts.filter(x=>x!==a);
      renderAccounts();
    };
    el.appendChild(chip);
  });
}

document.getElementById("addBtn").onclick = () => {
  const v = document.getElementById("userInput").value.trim();
  if(!v) return;
  const clean = v.replace(/^@/, "");
  if(!accounts.includes(clean)){
    accounts.push(clean);
    renderAccounts();
  }
  document.getElementById("userInput").value = "";
};

document.getElementById("clearBtn").onclick = () => { accounts = []; renderAccounts(); };

async function fetchFeedOnce(){
  if(accounts.length === 0) return;
  document.getElementById("status").textContent = `Fetching ${accounts.length} accounts...`;
  try {
    const q = new URLSearchParams();
    q.set("users", accounts.join(","));
    const resp = await fetch("/api/feed?" + q.toString());
    const json = await resp.json();
    renderFeed(json.posts || []);
    document.getElementById("status").textContent = `Last: ${new Date().toLocaleTimeString()}`;
  } catch (err){
    console.error(err);
    document.getElementById("status").textContent = "Error fetching";
  }
}

function renderFeed(posts){
  const feed = document.getElementById("feed");
  feed.innerHTML = "";
  for(const p of posts){
    const div = document.createElement("div");
    div.className = "post";
    const meta = document.createElement("div");
    meta.className = "meta";
    const acc = document.createElement("strong");
    acc.textContent = p.username;
    const timeA = document.createElement("a");
    timeA.href = p.link || "#";
    timeA.target = "_blank";
    timeA.className = "link small";
    const dt = new Date(p.timestamp);
    timeA.textContent = dt.toLocaleString();
    meta.appendChild(acc);
    meta.appendChild(timeA);
    if(p.source) {
      const src = document.createElement("span");
      src.className = "small";
      src.textContent = " · " + p.source;
      meta.appendChild(src);
    }
    const content = document.createElement("div");
    content.className = "content";
    content.textContent = p.content;
    div.appendChild(meta);
    div.appendChild(content);
    feed.appendChild(div);
  }
}

document.getElementById("startBtn").onclick = () => {
  if(polling) return;
  const ms = parseInt(document.getElementById("pollInterval").value) || 2000;
  polling = true;
  pollHandle = setInterval(fetchFeedOnce, ms);
  document.getElementById("status").textContent = "Running";
  fetchFeedOnce();
};

document.getElementById("stopBtn").onclick = () => {
  if(!polling) return;
  clearInterval(pollHandle);
  pollHandle = null;
  polling = false;
  document.getElementById("status").textContent = "Stopped";
};

// immediate-start: optional
// document.getElementById("startBtn").click();
</script>
</body>
</html>
"""

# -------------------------
# Scraper logic (async)
# -------------------------
CLIENT_TIMEOUT = 25.0

async def fetch_user_page(client: httpx.AsyncClient, username: str, base_host: str = "https://xcancel.com") -> Optional[str]:
    """
    Fetch the user's page HTML. Returns text or None.
    """
    url = f"{base_host.rstrip('/')}/{username}"
    try:
        r = await client.get(url, timeout=CLIENT_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        # quickly return None on errors
        return None

def parse_posts_from_html(html_text: str, username: str) -> List[Dict[str,Any]]:
    """
    Heuristics to extract posts from a front-end like xcancel/nitter.
    Returns list of dicts: {username, timestamp (ISO str), content, link, source}
    """
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")

    posts = []

    # Common frontends put tweets in <article> tags; try several selectors
    candidate_selectors = [
        "article",                  # generic
        "div.tweet",                # some clones
        "div.status",               # other clones
        "li.status",                # nitter-like
        "div.tweet-container",
        "div.tweet-item",
    ]

    elements = []
    for sel in candidate_selectors:
        found = soup.select(sel)
        if found:
            elements = found
            break

    # If nothing found, fallback to main timeline area
    if not elements:
        # Try finding elements that look like a timeline item by time tag
        elements = soup.select("time")
        # convert time tags to their parent nodes
        if elements:
            parents = []
            for t in elements:
                if t.parent:
                    parents.append(t.parent)
            elements = parents

    seen_texts = set()
    for el in elements:
        # timestamp: look for <time datetime="..."> or data-time attributes
        ts_el = el.find("time")
        timestamp = None
        if ts_el and ts_el.has_attr("datetime"):
            try:
                timestamp = dateparser.parse(ts_el["datetime"])
            except Exception:
                timestamp = None
        if not timestamp:
            # look for attributes like data-time, data-ms, or title with date
            for attr in ("data-time", "data-ms", "data-timestamp", "title"):
                if el.has_attr(attr):
                    try:
                        timestamp = dateparser.parse(el[attr])
                        break
                    except Exception:
                        continue

        # content: get text from element with class including 'content' 'tweet-text' etc.
        content = ""
        content_candidates = el.select(".tweet-text, .status__content, .content, .text, p, .tweet-body")
        if content_candidates:
            # join visible text nodes
            parts = []
            for c in content_candidates:
                txt = c.get_text(separator="\n").strip()
                if txt:
                    parts.append(txt)
            content = "\n\n".join(parts).strip()
        else:
            # fallback: element's textual content
            content = el.get_text(separator="\n").strip()

        # dedup empty
        if not content:
            continue

        # quick dedupe by content snippet
        snippet = content[:160]
        if snippet in seen_texts:
            continue
        seen_texts.add(snippet)

        # link: try find anchor to the post
        link = None
        # search for a direct permalink inside the element
        a = el.find("a", href=True)
        if a:
            link = a["href"]
            # normalize relative links
            if link.startswith("/"):
                link = "https://xcancel.com" + link

        # source: maybe the host or a meta label
        source = None
        # Try to find indicator like 'via' or site label
        src_candidate = el.select_one(".source, .meta .source, .via")
        if src_candidate:
            source = src_candidate.get_text(strip=True)

        # ensure timestamp exists; if not, fallback to now (UTC)
        if not timestamp:
            timestamp = datetime.now(timezone.utc)

        posts.append({
            "username": username,
            "timestamp": timestamp.isoformat(),
            "content": content,
            "link": link,
            "source": source
        })

    return posts

async def fetch_many_users(usernames: List[str], base_host: str = "https://xcancel.com", concurrency: int = 50) -> List[Dict[str,Any]]:
    """
    Concurrently fetch pages for many usernames and parse them into posts.
    concurrency: how many simultaneous requests (high -> faster)
    """
    timeout = httpx.Timeout(CLIENT_TIMEOUT)
    limits = httpx.Limits(max_keepalive_connections=concurrency, max_connections=concurrency)
    async with httpx.AsyncClient(timeout=timeout, limits=limits, headers={"User-Agent":"xcancel-feed/1.0"}) as client:
        sem = asyncio.Semaphore(concurrency)
        async def task(u):
            async with sem:
                html_text = await fetch_user_page(client, u, base_host=base_host)
                return parse_posts_from_html(html_text, u)
        tasks = [task(u) for u in usernames]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        posts = []
        for r in results:
            if isinstance(r, Exception):
                continue
            posts.extend(r or [])
        return posts

# -------------------------
# FastAPI endpoints
# -------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)

@app.get("/api/feed")
async def api_feed(users: Optional[str] = ""):
    """
    Query param 'users' expects comma-separated usernames (no @).
    Returns merged posts JSON sorted newest-first.
    """
    # parse users
    users_list = [u.strip() for u in users.split(",") if u.strip()]
    if not users_list:
        return JSONResponse({"posts": []})

    # fetch posts concurrently as fast as possible
    try:
        posts = await fetch_many_users(users_list, concurrency=200)
    except Exception as e:
        posts = []

    # parse timestamps to ensure sorting
    def parse_iso(ts_str):
        try:
            return dateparser.parse(ts_str)
        except Exception:
            return datetime.now(timezone.utc)

    for p in posts:
        try:
            p["_ts"] = parse_iso(p.get("timestamp"))
        except Exception:
            p["_ts"] = datetime.now(timezone.utc)

    posts_sorted = sorted(posts, key=lambda x: x["_ts"], reverse=True)

    # return only safe fields
    out = []
    for p in posts_sorted:
        out.append({
            "username": p.get("username"),
            "timestamp": p.get("timestamp"),
            "content": p.get("content"),
            "link": p.get("link"),
            "source": p.get("source")
        })

    return JSONResponse({"posts": out})

# -------------------------
# Run with: uvicorn xcancel_feed:app
# -------------------------
