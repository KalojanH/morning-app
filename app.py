"""
Morning News Radio — one-tap multilingual bulletin player.
Spotify-style morning routine: press play, hear the latest top-of-hour news
bulletin per language, auto-advance through your language order.

All feed fetching happens SERVER-SIDE (Python on Streamlit Cloud), so there
are no CORS problems. The browser only ever receives direct audio URLs.
"""

import html
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urljoin

import feedparser
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="Morning News Radio",
    page_icon="📻",
    layout="centered",
    initial_sidebar_state="collapsed",
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MorningNewsRadio/2.0",
    "Accept": "*/*",
}
TIMEOUT = 12
AUDIO_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.(?:mp3|m4a|aac|oga|ogg)(?:\?[^\s\"'<>\\]*)?", re.I)


# =====================================================================
# Generic helpers
# =====================================================================
def http_get(url: str, **kw) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    resp.raise_for_status()
    return resp


def parse_dt_any(value: Any) -> Optional[datetime]:
    """Parse RFC2822, ISO-8601 and a few European date formats into aware UTC."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d.%m.%Y %H:%M",
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.astimezone().strftime("%a %d %b, %H:%M")


def walk_json(obj: Any) -> Iterator[Dict[str, Any]]:
    """Yield every dict inside an arbitrarily nested JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v)


def find_audio_url(obj: Any, base: str = "") -> Optional[str]:
    """Find the first audio-looking URL anywhere inside a JSON fragment."""
    if isinstance(obj, str):
        m = AUDIO_RE.search(obj)
        if m:
            return m.group(0)
        if base and re.search(r"\.(mp3|m4a|aac)(\?|$)", obj, re.I):
            return urljoin(base, obj)
        return None
    if isinstance(obj, dict):
        # Prefer explicit audio keys first
        for key in ("url", "audio", "audio_url", "stream", "mediaUrl", "downloadable_url", "file"):
            if key in obj:
                found = find_audio_url(obj[key], base)
                if found:
                    return found
        for v in obj.values():
            found = find_audio_url(v, base)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = find_audio_url(v, base)
            if found:
                return found
    return None


# =====================================================================
# Fetchers — each returns {"title", "published" (datetime|None), "audio_url"}
# =====================================================================
def _entry_audio(entry: Any) -> Optional[str]:
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href")
        if href and ("audio" in enc.get("type", "") or AUDIO_RE.search(href)):
            return href
    for link in entry.get("links", []) or []:
        href = link.get("href")
        if href and ("audio" in link.get("type", "") or link.get("rel") == "enclosure" or AUDIO_RE.search(href)):
            return href
    for mc in entry.get("media_content", []) or []:
        u = mc.get("url")
        if u and ("audio" in mc.get("type", "") or AUDIO_RE.search(u)):
            return u
    return None


def fetch_rss_latest(feed_urls: List[str]) -> Optional[Dict[str, Any]]:
    """Parse one or many RSS feeds; return the single newest audio entry across all."""
    best: Optional[Dict[str, Any]] = None
    errors: List[str] = []
    for url in feed_urls:
        try:
            parsed = feedparser.parse(http_get(url).content)
            for entry in parsed.entries[:10]:
                audio = _entry_audio(entry)
                if not audio:
                    continue
                dt = parse_dt_any(entry.get("published") or entry.get("updated"))
                cand = {
                    "title": html.unescape(entry.get("title", "Bulletin")).strip(),
                    "published": dt,
                    "audio_url": audio,
                }
                if best is None:
                    best = cand
                elif dt and (best["published"] is None or dt > best["published"]):
                    best = cand
                break  # entries are newest-first; only the first audio entry per feed matters
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
    if best is None and errors:
        raise RuntimeError(" | ".join(errors[:2]))
    return best


def fetch_raiplaysound(program_json_url: str) -> Optional[Dict[str, Any]]:
    """RaiPlaySound program JSON (e.g. /programmi/gr1.json) → newest episode."""
    data = http_get(program_json_url).json()
    base = "https://www.raiplaysound.it/"
    candidates = []
    for d in walk_json(data):
        audio = d.get("audio")
        if not isinstance(audio, dict):
            continue
        url = find_audio_url(audio, base)
        if not url:
            continue
        title = d.get("episode_title") or d.get("toptitle") or d.get("title") or "GR1"
        dt = None
        for k, v in d.items():
            if "date" in k.lower() or "publication" in k.lower():
                dt = dt or parse_dt_any(v if isinstance(v, str) else None)
        ti = d.get("track_info")
        if isinstance(ti, dict):
            dt = dt or parse_dt_any(ti.get("date"))
        candidates.append({"title": str(title).strip(), "published": dt, "audio_url": url})
    if not candidates:
        return None
    dated = [c for c in candidates if c["published"]]
    if dated:
        return max(dated, key=lambda c: c["published"])
    return candidates[0]


def fetch_rtve(slug: str) -> Optional[Dict[str, Any]]:
    """RTVE open API. Resolve program id from slug, then read its audios RSS/JSON."""
    program_id = None
    # 1. slug endpoint
    try:
        data = http_get(f"https://www.rtve.es/api/programas/{slug}").json()
        for d in walk_json(data):
            if d.get("id") and (d.get("htmlUrl") or d.get("uri") or d.get("name")):
                program_id = d["id"]
                break
    except Exception:
        pass
    # 2. scrape the play page for a numeric program id
    if not program_id:
        try:
            page = http_get(f"https://www.rtve.es/play/audios/{slug}/").text
            m = re.search(r"programas?/(\d{3,7})", page) or re.search(r'"idProgram"\s*:\s*"?(\d{3,7})', page)
            if m:
                program_id = m.group(1)
        except Exception:
            pass
    attempts = []
    if program_id:
        attempts.append(f"https://www.rtve.es/api/programas/{program_id}/audios.rss")
    attempts.append(f"https://www.rtve.es/api/programas/{slug}/audios.rss")
    try:
        best = fetch_rss_latest(attempts)
        if best:
            return best
    except Exception:
        pass
    # 3. JSON variant, walk for any mp3
    for aurl in ([f"https://www.rtve.es/api/programas/{program_id}/audios.json"] if program_id else []):
        try:
            data = http_get(aurl).json()
            items = data.get("page", {}).get("items", []) or []
            for item in items:
                audio = find_audio_url(item)
                if audio:
                    return {
                        "title": item.get("longTitle") or item.get("title", "Boletín RNE"),
                        "published": parse_dt_any(item.get("publicationDate")),
                        "audio_url": audio,
                    }
        except Exception:
            continue
    return None


NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
BG_TITLE_RE = re.compile(r"[Ее]мисия|[Нн]овини")


def fetch_bnr(page_urls: List[str]) -> Optional[Dict[str, Any]]:
    """BNR (binar.bg / bnrnews.bg): parse the Next.js __NEXT_DATA__ payload
    server-side and dig out the newest news-bulletin audio."""
    for url in page_urls:
        try:
            page = http_get(url).text
        except Exception:
            continue
        m = NEXT_DATA_RE.search(page)
        blobs: List[Any] = []
        if m:
            try:
                blobs.append(json.loads(m.group(1)))
            except Exception:
                pass
        candidates = []
        for blob in blobs:
            for d in walk_json(blob):
                audio = None
                for v in d.values():
                    if isinstance(v, str):
                        am = AUDIO_RE.search(v)
                        if am:
                            audio = am.group(0)
                            break
                        if re.search(r"/api/media/[0-9a-f-]{20,}", v, re.I) and re.search(
                            r"audio|sound|mp3", json.dumps(d, ensure_ascii=False), re.I
                        ):
                            audio = urljoin(url, v)
                            break
                if not audio:
                    continue
                title = ""
                dt = None
                for k, v in d.items():
                    if isinstance(v, str):
                        if not title and BG_TITLE_RE.search(v) and len(v) < 150:
                            title = v
                        if "date" in k.lower() or "time" in k.lower() or "created" in k.lower():
                            dt = dt or parse_dt_any(v)
                candidates.append({
                    "title": title or "Емисия новини (БНР)",
                    "published": dt,
                    "audio_url": audio,
                    "_is_news": bool(title),
                })
        # fallback: raw regex over whole page for mp3 near "мисия"
        if not candidates:
            for am in AUDIO_RE.finditer(page):
                candidates.append({
                    "title": "Емисия новини (БНР)", "published": None,
                    "audio_url": am.group(0), "_is_news": False,
                })
        news = [c for c in candidates if c["_is_news"]] or candidates
        if news:
            dated = [c for c in news if c["published"]]
            best = max(dated, key=lambda c: c["published"]) if dated else news[0]
            best.pop("_is_news", None)
            return best
    return None


@st.cache_data(ttl=86400, show_spinner=False)
def resolve_live_stream(uuid: str = "", name: str = "") -> Optional[str]:
    """radio-browser.info lookup — returns a working (preferably https) stream URL."""
    hosts = ["de1.api.radio-browser.info", "de2.api.radio-browser.info", "fi1.api.radio-browser.info"]
    for host in hosts:
        try:
            if uuid:
                data = http_get(f"https://{host}/json/stations/byuuid/{uuid}").json()
            else:
                data = http_get(
                    f"https://{host}/json/stations/search",
                    params={"name": name, "limit": 10, "hidebroken": "true",
                            "order": "clickcount", "reverse": "true"},
                ).json()
            stations = data if isinstance(data, list) else []
            https = [s for s in stations if str(s.get("url_resolved", "")).startswith("https")]
            for s in https + stations:
                if s.get("url_resolved"):
                    return s["url_resolved"]
        except Exception:
            continue
    return None


# =====================================================================
# Station / source configuration
# =====================================================================
SOURCES: Dict[str, Dict[str, Any]] = {
    "dlf": {
        "station": "Deutschlandfunk Nachrichten",
        "type": "rss",
        "feeds": [
            "https://www.deutschlandfunk.de/nachrichten-108.xml",
            "https://www.deutschlandfunk.de/nachrichten-100.rss",
        ],
        "live": "https://st01.sslstream.dlf.de/dlf/01/128/mp3/stream.mp3",
        "fresh_hours": 3,
    },
    "dlf_kultur": {
        "station": "DLF Kultur — Kulturnachrichten",
        "type": "rss",
        "feeds": ["https://www.deutschlandfunkkultur.de/podcast-kulturnachrichten-100.xml"],
        "live": "https://st02.sslstream.dlf.de/dlf/02/128/mp3/stream.mp3",
        "fresh_hours": 26,
    },
    "franceinfo": {
        "station": "franceinfo — Journal",
        "type": "rss",
        # Per-hour journal feeds. 11736 (Journal de 19h) is verified live.
        # Add more IDs here as you confirm them in the Diagnostics panel.
        "feeds": [
            "https://radiofrance-podcast.net/podcast09/rss_11736.xml",
        ],
        "live": "https://icecast.radiofrance.fr/franceinfo-midfi.mp3",
        "fresh_hours": 26,
    },
    "bbc_bulletin": {
        "station": "BBC World Service — News",
        "type": "rss",
        "feeds": [
            "https://podcasts.files.bbci.co.uk/p002vsmz.rss",   # 5-min bulletin brand (may be empty)
            "https://podcasts.files.bbci.co.uk/p02nq0gn.rss",   # Global News Podcast (verified)
        ],
        "live_lookup": {"name": "BBC World Service"},
        "fresh_hours": 26,
    },
    "npr_now": {
        "station": "NPR News Now (hourly)",
        "type": "rss",
        "feeds": ["https://feeds.npr.org/500005/podcast.xml"],
        "live_lookup": {"name": "BBC World Service"},
        "fresh_hours": 3,
    },
    "rtve_boletines": {
        "station": "RNE — Boletines",
        "type": "rtve",
        "slug": "boletines-rne",
        "live_lookup": {"name": "RNE Radio Nacional"},
        "fresh_hours": 4,
    },
    "rai_gr1": {
        "station": "Rai Radio 1 — GR1",
        "type": "raiplaysound",
        "json_url": "https://www.raiplaysound.it/programmi/gr1.json",
        "live": "https://icestreaming.rai.it/1.mp3",
        "fresh_hours": 5,
    },
    "bnr_horizont": {
        "station": "БНР Хоризонт — Новини",
        "type": "bnr",
        "pages": [
            "https://binar.bg/news?p=horizont",
            "https://binar.bg/news",
            "https://bnrnews.bg/horizont",
        ],
        "live_lookup": {"uuid": "3a3b1465-6a6f-4289-901c-bd5890cb8370", "name": "BNR Horizont"},
        "fresh_hours": 4,
    },
    "nova_news": {
        "station": "Nova News (live only)",
        "type": "live_only",
        "live_lookup": {"name": "Nova News"},
        "fresh_hours": 0,
    },
}

LANGUAGES: Dict[str, Dict[str, Any]] = {
    "de": {"flag": "🇩🇪", "label": "Deutsch", "sources": ["dlf", "dlf_kultur"]},
    "fr": {"flag": "🇫🇷", "label": "Français", "sources": ["franceinfo"]},
    "en": {"flag": "🇬🇧", "label": "English", "sources": ["bbc_bulletin", "npr_now"]},
    "es": {"flag": "🇪🇸", "label": "Español", "sources": ["rtve_boletines"]},
    "it": {"flag": "🇮🇹", "label": "Italiano", "sources": ["rai_gr1"]},
    "bg": {"flag": "🇧🇬", "label": "Български", "sources": ["bnr_horizont", "nova_news"]},
}
DEFAULT_ORDER = list(LANGUAGES.keys())


# =====================================================================
# Bulletin resolution (cached)
# =====================================================================
@st.cache_data(ttl=240, show_spinner=False)
def get_source_result(source_id: str) -> Dict[str, Any]:
    """Fetch the latest bulletin for a source; decide bulletin vs live fallback."""
    cfg = SOURCES[source_id]
    result: Dict[str, Any] = {
        "station": cfg["station"], "mode": None, "url": None,
        "title": "", "published": None, "error": "",
    }
    bulletin = None
    if cfg["type"] != "live_only":
        try:
            if cfg["type"] == "rss":
                bulletin = fetch_rss_latest(cfg["feeds"])
            elif cfg["type"] == "raiplaysound":
                bulletin = fetch_raiplaysound(cfg["json_url"])
            elif cfg["type"] == "rtve":
                bulletin = fetch_rtve(cfg["slug"])
            elif cfg["type"] == "bnr":
                bulletin = fetch_bnr(cfg["pages"])
        except Exception as exc:
            result["error"] = str(exc)[:300]

    fresh = False
    if bulletin:
        dt = bulletin.get("published")
        if dt is None:
            fresh = True  # no timestamp — trust the feed ordering
        else:
            fresh = (datetime.now(timezone.utc) - dt) <= timedelta(hours=cfg["fresh_hours"])

    if bulletin and fresh:
        result.update(
            mode="bulletin", url=bulletin["audio_url"],
            title=bulletin["title"], published=fmt_dt(bulletin.get("published")),
        )
        return result

    # Live fallback
    live = cfg.get("live")
    if not live and cfg.get("live_lookup"):
        live = resolve_live_stream(**cfg["live_lookup"])
    if live:
        note = "no recent bulletin — live radio" if cfg["type"] != "live_only" else "live radio"
        result.update(mode="live", url=live, title=note)
        if bulletin:  # stale bulletin still offered in diagnostics
            result["stale_bulletin"] = {
                "title": bulletin["title"], "url": bulletin["audio_url"],
                "published": fmt_dt(bulletin.get("published")),
            }
        return result

    result["error"] = result["error"] or "No bulletin and no live stream found"
    return result


# =====================================================================
# Sidebar — order, sources, refresh
# =====================================================================
if "lang_order" not in st.session_state:
    st.session_state.lang_order = DEFAULT_ORDER.copy()
if "enabled" not in st.session_state:
    st.session_state.enabled = {k: True for k in LANGUAGES}
if "chosen_source" not in st.session_state:
    st.session_state.chosen_source = {k: v["sources"][0] for k, v in LANGUAGES.items()}


def move_lang(code: str, delta: int) -> None:
    order = st.session_state.lang_order
    i = order.index(code)
    j = i + delta
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]


with st.sidebar:
    st.header("⚙️ Languages")
    st.caption("Order = play order. Toggle off what you skip today.")
    for code in st.session_state.lang_order:
        lang = LANGUAGES[code]
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
        with c1:
            st.session_state.enabled[code] = st.checkbox(
                f"{lang['flag']} {lang['label']}",
                value=st.session_state.enabled[code],
                key=f"en_{code}",
            )
        with c2:
            st.button("↑", key=f"up_{code}", on_click=move_lang, args=(code, -1))
        with c3:
            st.button("↓", key=f"dn_{code}", on_click=move_lang, args=(code, 1))
        with c4:
            st.write("")
        if len(lang["sources"]) > 1:
            st.session_state.chosen_source[code] = st.selectbox(
                "Source",
                lang["sources"],
                index=lang["sources"].index(st.session_state.chosen_source[code]),
                format_func=lambda s: SOURCES[s]["station"],
                key=f"src_{code}",
                label_visibility="collapsed",
            )
    st.divider()
    if st.button("🔄 Refresh bulletins"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Bulletins are re-fetched every 4 minutes automatically.")


# =====================================================================
# Build playlist
# =====================================================================
st.markdown(
    "<h1 style='letter-spacing:-0.04em;margin-bottom:0'>📻 Morning News Radio</h1>"
    "<p style='color:#667085;margin-top:2px'>One tap. Latest bulletin, every language, in your order.</p>",
    unsafe_allow_html=True,
)

playlist: List[Dict[str, Any]] = []
problems: List[str] = []
active_codes = [c for c in st.session_state.lang_order if st.session_state.enabled[c]]

with st.spinner("Fetching the latest bulletins…"):
    for code in active_codes:
        source_id = st.session_state.chosen_source[code]
        res = get_source_result(source_id)
        if res["url"]:
            playlist.append({
                "lang": code,
                "flag": LANGUAGES[code]["flag"],
                "label": LANGUAGES[code]["label"],
                "station": res["station"],
                "mode": res["mode"],
                "url": res["url"],
                "title": res["title"],
                "published": res["published"] or "",
            })
        else:
            problems.append(f"{LANGUAGES[code]['flag']} {res['station']}: {res['error']}")

if problems:
    st.warning("Skipped: " + " · ".join(problems))

# =====================================================================
# Embedded player
# =====================================================================
if not playlist:
    st.error("Nothing playable right now. Open Diagnostics below to see what failed.")
else:
    playlist_json = json.dumps(playlist, ensure_ascii=False).replace("</", "<\\/")
    player_html = """
<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif; }
  body { background: transparent; }
  .card { background: linear-gradient(150deg,#101828,#1d2939 60%,#243b53); border-radius: 22px;
          padding: 22px 22px 18px; color: #fff; box-shadow: 0 14px 40px rgba(16,24,40,.35); }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
  .chip { border: 1px solid rgba(255,255,255,.18); background: rgba(255,255,255,.06); color: #e4e7ec;
          border-radius: 999px; padding: 7px 13px; font-size: 14px; cursor: pointer; display: flex;
          align-items: center; gap: 6px; transition: all .15s; }
  .chip:hover { background: rgba(255,255,255,.14); }
  .chip.active { background: #f97316; border-color: #f97316; color: #fff; font-weight: 700; }
  .chip .lv { font-size: 9px; font-weight: 800; background: #ef4444; border-radius: 4px; padding: 1px 4px; }
  .now { min-height: 66px; margin-bottom: 6px; }
  .station { font-size: 13px; letter-spacing: .06em; text-transform: uppercase; color: #98a2b3; }
  .title { font-size: 17px; font-weight: 700; margin-top: 3px; line-height: 1.3;
           display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .when { font-size: 13px; color: #98a2b3; margin-top: 3px; }
  .bar { height: 5px; border-radius: 3px; background: rgba(255,255,255,.14); margin: 12px 0 16px; cursor: pointer; }
  .bar > div { height: 100%; width: 0%; border-radius: 3px; background: #f97316; }
  .controls { display: flex; align-items: center; justify-content: center; gap: 18px; }
  button.ctl { background: rgba(255,255,255,.08); border: none; color: #fff; border-radius: 50%;
               width: 46px; height: 46px; font-size: 17px; cursor: pointer; transition: background .15s; }
  button.ctl:hover { background: rgba(255,255,255,.18); }
  button.play { width: 74px; height: 74px; font-size: 30px; background: #f97316; box-shadow: 0 8px 24px rgba(249,115,22,.45); }
  button.play:hover { background: #fb8a3c; }
  .foot { text-align: center; color: #667085; font-size: 12px; margin-top: 12px; }
</style></head><body>
<div class="card">
  <div class="chips" id="chips"></div>
  <div class="now">
    <div class="station" id="station"></div>
    <div class="title" id="title">Press play to start your briefing</div>
    <div class="when" id="when"></div>
  </div>
  <div class="bar" id="bar"><div id="fill"></div></div>
  <div class="controls">
    <button class="ctl" id="prev" title="Previous language">⏮</button>
    <button class="ctl" id="back" title="Back 15s">↺15</button>
    <button class="ctl play" id="play">▶</button>
    <button class="ctl" id="fwd" title="Forward 15s">↻15</button>
    <button class="ctl" id="next" title="Next language">⏭</button>
  </div>
  <div class="foot" id="foot"></div>
</div>
<script>
const PLAYLIST = __PLAYLIST__;
let idx = 0, started = false;
const audio = new Audio();
audio.preload = "none";
const $ = id => document.getElementById(id);

function renderChips() {
  $("chips").innerHTML = "";
  PLAYLIST.forEach((item, i) => {
    const c = document.createElement("div");
    c.className = "chip" + (i === idx ? " active" : "");
    c.innerHTML = item.flag + " " + item.label + (item.mode === "live" ? ' <span class="lv">LIVE</span>' : "");
    c.onclick = () => playIndex(i);
    $("chips").appendChild(c);
  });
}
function renderNow() {
  const item = PLAYLIST[idx];
  $("station").textContent = item.station;
  $("title").textContent = started ? item.title : "Press play to start your briefing";
  $("when").textContent = item.mode === "live" ? "● live stream" : (item.published || "latest bulletin");
  $("foot").textContent = (idx + 1) + " / " + PLAYLIST.length + " — auto-advances when a bulletin ends";
  renderChips();
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: item.title || item.station, artist: item.station, album: "Morning News Radio" });
  }
}
function playIndex(i) {
  idx = (i + PLAYLIST.length) % PLAYLIST.length;
  started = true;
  const item = PLAYLIST[idx];
  audio.src = item.url;
  audio.play().catch(() => {});
  renderNow();
  $("play").textContent = "⏸";
}
$("play").onclick = () => {
  if (!started) { playIndex(0); return; }
  if (audio.paused) { audio.play().catch(()=>{}); $("play").textContent = "⏸"; }
  else { audio.pause(); $("play").textContent = "▶"; }
};
$("next").onclick = () => playIndex(idx + 1);
$("prev").onclick = () => playIndex(idx - 1);
$("back").onclick = () => { if (isFinite(audio.duration)) audio.currentTime = Math.max(0, audio.currentTime - 15); };
$("fwd").onclick  = () => { if (isFinite(audio.duration)) audio.currentTime = Math.min(audio.duration, audio.currentTime + 15); };
audio.onended = () => {
  if (idx + 1 < PLAYLIST.length) playIndex(idx + 1);
  else { $("play").textContent = "▶"; $("title").textContent = "Briefing finished ✓"; }
};
audio.ontimeupdate = () => {
  if (isFinite(audio.duration) && audio.duration > 0)
    $("fill").style.width = (100 * audio.currentTime / audio.duration) + "%";
  else $("fill").style.width = "100%";
};
$("bar").onclick = (e) => {
  if (!isFinite(audio.duration)) return;
  const r = $("bar").getBoundingClientRect();
  audio.currentTime = audio.duration * (e.clientX - r.left) / r.width;
};
audio.onerror = () => {
  $("title").textContent = "⚠ Could not play — skipping in 2s";
  if (started) setTimeout(() => { if (idx + 1 < PLAYLIST.length) playIndex(idx + 1); }, 2000);
};
if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("nexttrack", () => playIndex(idx + 1));
  navigator.mediaSession.setActionHandler("previoustrack", () => playIndex(idx - 1));
}
renderNow();
</script></body></html>
""".replace("__PLAYLIST__", playlist_json)
    components.html(player_html, height=430)

# =====================================================================
# Diagnostics
# =====================================================================
with st.expander("🩺 Diagnostics — what each source returned"):
    st.caption(
        "Use this after every deploy: it shows exactly which feed answered, what it "
        "returned, and lets you test-play each audio URL. If a source shows an error, "
        "the fix is usually adding/correcting a feed URL in SOURCES at the top of app.py."
    )
    for code in st.session_state.lang_order:
        lang = LANGUAGES[code]
        for source_id in lang["sources"]:
            res = get_source_result(source_id)
            icon = "✅" if res["mode"] == "bulletin" else ("📡" if res["mode"] == "live" else "❌")
            st.markdown(f"**{icon} {lang['flag']} {res['station']}** — mode: `{res['mode']}`")
            if res["title"]:
                st.write(f"“{res['title']}” {('— ' + res['published']) if res['published'] else ''}")
            if res.get("stale_bulletin"):
                sb = res["stale_bulletin"]
                st.write(f"Stale bulletin available: “{sb['title']}” — {sb['published']}")
            if res["error"]:
                st.code(res["error"], language=None)
            if res["url"]:
                st.audio(res["url"])
            st.divider()
