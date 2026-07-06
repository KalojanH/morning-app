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
from zoneinfo import ZoneInfo

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


def http_post(url: str, json_body: Any = None, extra_headers: Optional[Dict[str, str]] = None) -> requests.Response:
    h = dict(HEADERS)
    h.update(extra_headers or {})
    resp = requests.post(url, json=json_body, headers=h, timeout=TIMEOUT)
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


FR_MONTHS = {"janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5,
             "juin": 6, "juillet": 7, "aout": 8, "août": 8, "septembre": 9,
             "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12}
BG_MONTHS = {"януари": 1, "февруари": 2, "март": 3, "април": 4, "май": 5, "юни": 6,
             "юли": 7, "август": 8, "септември": 9, "октомври": 10,
             "ноември": 11, "декември": 12}


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


def verify_audio(url: str) -> Optional[str]:
    """Follow redirects server-side and confirm the URL really serves audio.
    Returns the final resolved URL if it is audio, else None."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True, allow_redirects=True)
        ct = (r.headers.get("Content-Type") or "").lower()
        final = r.url
        r.close()
        if "audio" in ct or "mpeg" in ct or (
            "octet-stream" in ct and AUDIO_RE.search(final)
        ) or AUDIO_RE.search(final):
            return final
    except Exception:
        pass
    return None


# =====================================================================
# Fetchers — each returns a list (newest first, up to 2) of
# {"title", "published" (datetime|None), "audio_url"}
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


def _sort_newest(cands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Newest first; undated entries keep their original (feed) order at the end."""
    dated = [c for c in cands if c["published"]]
    undated = [c for c in cands if not c["published"]]
    dated.sort(key=lambda c: c["published"], reverse=True)
    return dated + undated


def fetch_rss_latest(feed_urls: List[str], title_filter: str = "",
                     per_feed: int = 2) -> List[Dict[str, Any]]:
    """Parse one or many RSS feeds; return the newest audio entries across all."""
    cands: List[Dict[str, Any]] = []
    errors: List[str] = []
    tf = re.compile(title_filter, re.I) if title_filter else None
    for url in feed_urls:
        try:
            parsed = feedparser.parse(http_get(url).content)
            taken = 0
            for entry in parsed.entries[:12]:
                audio = _entry_audio(entry)
                if not audio:
                    continue
                title = html.unescape(entry.get("title", "Bulletin")).strip()
                if tf and not tf.search(title):
                    continue
                cands.append({
                    "title": title,
                    "published": parse_dt_any(entry.get("published") or entry.get("updated")),
                    "audio_url": audio,
                })
                taken += 1
                if taken >= per_feed:
                    break
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
    if not cands and errors:
        raise RuntimeError(" | ".join(errors[:2]))
    return _sort_newest(cands)[:2]


def fetch_raiplaysound(program_json_url: str) -> List[Dict[str, Any]]:
    """RaiPlaySound program JSON (e.g. /programmi/gr1.json) → newest episodes."""
    data = http_get(program_json_url).json()
    base = "https://www.raiplaysound.it/"
    candidates = []
    seen = set()
    for d in walk_json(data):
        audio = d.get("audio")
        if not isinstance(audio, dict):
            continue
        url = find_audio_url(audio, base)
        if not url or url in seen:
            continue
        seen.add(url)
        title = d.get("episode_title") or d.get("toptitle") or d.get("title") or "GR1"
        dt = None
        for k, v in d.items():
            if "date" in k.lower() or "publication" in k.lower():
                dt = dt or parse_dt_any(v if isinstance(v, str) else None)
        ti = d.get("track_info")
        if isinstance(ti, dict):
            dt = dt or parse_dt_any(ti.get("date"))
        candidates.append({"title": str(title).strip(), "published": dt, "audio_url": url})
    return _sort_newest(candidates)[:2]


def fetch_rtve(slug: str) -> List[Dict[str, Any]]:
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
    results: List[Dict[str, Any]] = []
    for aurl in ([f"https://www.rtve.es/api/programas/{program_id}/audios.json"] if program_id else []):
        try:
            data = http_get(aurl).json()
            items = data.get("page", {}).get("items", []) or []
            for item in items:
                audio = find_audio_url(item)
                if audio:
                    results.append({
                        "title": item.get("longTitle") or item.get("title", "Boletín RNE"),
                        "published": parse_dt_any(item.get("publicationDate")),
                        "audio_url": audio,
                    })
                if len(results) >= 2:
                    break
        except Exception:
            continue
    return _sort_newest(results)[:2]


def _parse_bg_title_dt(title: str) -> Optional[datetime]:
    """'Емисия новини от 20:00 часа на 5 юли 2026 г.' → aware datetime (Sofia)."""
    tm = re.search(r"(\d{1,2})[:.](\d{2})\s*час", title)
    dm = re.search(r"на\s+(\d{1,2})\s+([а-я]+)\s+(\d{4})", title, re.I)
    if not dm:
        return None
    month = BG_MONTHS.get(dm.group(2).lower())
    if not month:
        return None
    try:
        return datetime(
            int(dm.group(3)), month, int(dm.group(1)),
            int(tm.group(1)) if tm else 0, int(tm.group(2)) if tm else 0,
            tzinfo=ZoneInfo("Europe/Sofia"),
        ).astimezone(timezone.utc)
    except Exception:
        return None


def fetch_bnr(api_url: str, media_base: str,
              notes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """BNR bulletins via binar.bg's JSON API (discovered by network inspection):
    GET /api/programs/news/horizont → [{NewsTitle, NewsAudio (uuid), ...}, ...]
    newest first; audio file = /api/media/{uuid}."""
    notes = notes if notes is not None else []
    data = http_get(api_url).json()
    if not isinstance(data, list):
        notes.append("unexpected API shape")
        return []
    notes.append(f"news API ok ({len(data)} items)")
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        uuid = item.get("NewsAudio")
        title = item.get("NewsTitle") or "Емисия новини"
        if not uuid:
            continue
        out.append({
            "title": title,
            "published": _parse_bg_title_dt(title),
            "audio_url": f"{media_base}/{uuid}",
        })
        if len(out) >= 2:
            break
    return out


# ---------- BBC (Sounds rms API → mediaselector v3 → HLS) ----------
def _bbc_hls_for_pid(pid: str, notes: List[str]) -> Optional[str]:
    """The rms episode id IS the playable version id: feed it to mediaselector v3
    as cvid/urn:bbc:pips:pid:{id}. WS bulletins are HLS/DASH only (no mp3) —
    return the https HLS master (played via hls.js in the embedded player)."""
    ms = http_get(
        "https://open.live.bbc.co.uk/mediaselector/6/select/version/3.0/"
        f"mediaset/pc/cvid/urn:bbc:pips:pid:{pid}/format/json/cors/1"
    ).json()
    fallback = None
    for media in ms.get("media", []) or []:
        for conn in media.get("connection", []) or []:
            href, tf = conn.get("href", ""), conn.get("transferFormat")
            if not href.startswith("https"):
                continue
            if tf == "hls":
                return href
            if ".mp3" in href or tf == "plain":
                fallback = fallback or href
    if fallback:
        notes.append("no hls, using plain")
    return fallback


def fetch_bbc_brand(brand_pid: str, fallback_feeds: List[str],
                    notes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Latest short news bulletins of BBC brand p002vsmz (hourly, 5 minutes).
    Verified flow (Chrome network inspection): rms API lists episodes newest
    first; mediaselector v3 with the episode id returns the HLS audio master."""
    notes = notes if notes is not None else []
    episodes: List[Dict[str, Any]] = []
    try:
        data = http_get(
            f"https://rms.api.bbc.co.uk/v2/programmes/playable"
            f"?container={brand_pid}&sort=-release_date&type=episode"
        ).json()
        for ep in (data.get("data") or [])[:3]:
            t = ep.get("titles") or {}
            episodes.append({
                "pid": ep.get("id"),
                "title": " — ".join(x for x in (t.get("primary"), t.get("secondary")) if x)
                         or "BBC News Bulletin",
                "published": parse_dt_any((ep.get("release") or {}).get("date")),
            })
        if episodes:
            notes.append(f"rms ok ({len(episodes)} episode(s))")
    except Exception as exc:
        notes.append(f"rms fail: {str(exc)[:80]}")
    results: List[Dict[str, Any]] = []
    for ep in episodes:
        if not ep["pid"] or len(results) >= 2:
            continue
        try:
            hls = _bbc_hls_for_pid(ep["pid"], notes)
            if hls:
                results.append({"title": ep["title"], "published": ep["published"],
                                "audio_url": hls, "format": "hls"})
            else:
                notes.append(f"no playable media for {ep['pid']}")
        except Exception as exc:
            notes.append(f"mediaselector fail {ep['pid']}: {str(exc)[:60]}")
    if results:
        notes.append(f"{len(results)} bulletin(s) via mediaselector v3")
        return results
    # Fallback: this brand's own podcast RSS only
    try:
        best = fetch_rss_latest(fallback_feeds)
        if best:
            notes.append("brand rss fallback ok")
            return best
    except Exception as exc:
        notes.append(f"brand rss fallback fail: {str(exc)[:80]}")
    return []


# ---------- RAI (rainews.it notiziari page → relinker) ----------
# cont= values are opaque tokens (NOT numeric ids) — verified in Chrome.
RELINKER_RE = re.compile(
    r"https?://mediapolis[a-z0-9.]*\.rai\.it/relinker/relinkerServlet\.htm\?cont=[A-Za-z0-9]+", re.I)
DATA_ATTR_RE = re.compile(r"""\bdata=(?:"([^"]+)"|'([^']+)')""")


def fetch_rainews(page_url: str, raiplaysound_json: str,
                  notes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Latest GR1 editions. The rainews.it page embeds per-edition JSON in
    element data attributes: the 'Ultime edizioni' aggregator has cards with
    title, broadcast.edition.dateIso and a relinker content_url. The relinker
    plays directly in an <audio> element (verified in Chrome)."""
    notes = notes if notes is not None else []
    try:
        page = http_get(page_url).text
        editions: List[Dict[str, Any]] = []
        by_url: Dict[str, Dict[str, Any]] = {}

        def add(title, date_iso, url):
            if not url:
                return
            dt = parse_dt_any(date_iso)
            if url in by_url:
                # merge: a dated/card entry enriches an earlier undated player entry
                e = by_url[url]
                e["published"] = e["published"] or dt
                if title and (not e["title"] or e["title"] == "GR1"):
                    e["title"] = title
                return
            entry = {"title": title or "GR1", "published": dt, "audio_url": url}
            by_url[url] = entry
            editions.append(entry)

        for m in DATA_ATTR_RE.finditer(page):
            raw = html.unescape(m.group(1) or m.group(2) or "")
            if "content_url" not in raw and "cards" not in raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            for card in obj.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                url = card.get("content_url") or (card.get("media") or {}).get("mediapolis") \
                    if isinstance(card.get("media"), dict) else card.get("content_url")
                edition = (card.get("broadcast") or {}).get("edition") or {}
                add(card.get("title"), edition.get("dateIso") or card.get("edizioneIso"), url)
            if obj.get("content_url") and obj.get("audio"):
                ti = obj.get("track_info") or {}
                add(obj.get("title"), ti.get("dateIso") or ti.get("date"), obj["content_url"])
        # raw fallback: any relinker URLs in the page
        if not editions:
            for link in dict.fromkeys(RELINKER_RE.findall(html.unescape(page))):
                add("GR1", None, link)
        if editions:
            editions = _sort_newest(editions)[:2]
            notes.append(f"rainews ok ({len(editions)} edition(s))")
            return editions
        notes.append("rainews page ok but no editions found")
    except Exception as exc:
        notes.append(f"rainews fail: {str(exc)[:80]}")
    try:
        res = fetch_raiplaysound(raiplaysound_json)
        if res:
            notes.append("raiplaysound fallback ok")
            return res
    except Exception as exc:
        notes.append(f"raiplaysound fail: {str(exc)[:80]}")
    return []


# ---------- Euronews Russian (Top News Stories Today — video mp4, audio track) ----------
EURONEWS_EP_RE = re.compile(r"/video/(\d{4})/(\d{2})/(\d{2})/[a-z0-9-]+")
EURONEWS_MP4_RE = re.compile(r'"contentUrl"\s*:\s*"(https:[^"]+\.mp4[^"]*)"')
EURONEWS_EDITIONS = {  # slug hint → (label, approx CET hour)
    "utrennij": ("утренний выпуск", 8),
    "dnevnoj": ("дневной выпуск", 13),
    "vechernij": ("вечерний выпуск", 20),
}


def fetch_euronews(program_url: str, notes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """ru.euronews.com programme page lists episodes newest-first; each episode
    page embeds a direct mp4 (JSON-LD contentUrl). <audio> plays the mp4's
    audio track, so no video embedding is needed."""
    notes = notes if notes is not None else []
    base = "https://" + program_url.split("//", 1)[-1].split("/", 1)[0]
    page = http_get(program_url).text
    eps = list(dict.fromkeys(m.group(0) for m in EURONEWS_EP_RE.finditer(page)))
    if not eps:
        notes.append("no episode links found")
        return []
    notes.append(f"programme page ok ({len(eps)} episode(s))")
    results: List[Dict[str, Any]] = []
    for path in eps[:3]:
        if len(results) >= 2:
            break
        try:
            ep_page = http_get(base + path).text
            m = EURONEWS_MP4_RE.search(ep_page)
            if not m:
                notes.append(f"{path.rsplit('/',1)[-1][:30]}: no contentUrl")
                continue
            dm = EURONEWS_EP_RE.search(path)
            label, hour = "выпуск новостей", 12
            for hint, (lab, h) in EURONEWS_EDITIONS.items():
                if hint in path:
                    label, hour = lab, h
                    break
            published = None
            if dm:
                published = datetime(
                    int(dm.group(1)), int(dm.group(2)), int(dm.group(3)), hour, 0,
                    tzinfo=ZoneInfo("Europe/Paris"),
                ).astimezone(timezone.utc)
                if datetime.now(timezone.utc) - published > timedelta(hours=30):
                    notes.append("stale episode, stopping")
                    break
            results.append({
                "title": f"Новости дня — {label}",
                "published": published,
                "audio_url": m.group(1),
            })
        except Exception as exc:
            notes.append(f"episode fail: {str(exc)[:60]}")
    return results


# ---------- VRT Radio 1 (Flemish) — GraphQL → token → HLS ----------
VRT_GQL = "https://www.vrt.be/vrtnu-api/graphql/public/v1"
VRT_GQL_HEADERS = {
    "content-type": "application/json",
    "x-vrt-client-name": "WEB",
    "x-vrt-client-version": "1.5.17",
    "x-vrt-zone": "default",
    "accept": "application/graphql-response+json, application/json",
}
VRT_TOKENS = "https://media-services-public.vrt.be/vualto-video-aggregator-web/rest/external/v2/tokens"
VRT_VIDEOS = "https://media-services-public.vrt.be/vualto-video-aggregator-web/rest/external/v2/videos/"


def fetch_vrt(page_id: str, notes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """VRT MAX 'recentste nieuws' (verified flow): public GraphQL page query →
    modes[0].streamId → anonymous player token → media aggregator → HLS."""
    notes = notes if notes is not None else []
    q = ("query($id: ID!){ page(id:$id){ ... on PlaybackPage { title "
         "player { title modes { label streamId durationInSeconds } } } } }")
    data = http_post(VRT_GQL, {"query": q, "variables": {"id": page_id}},
                     VRT_GQL_HEADERS).json()
    player = ((data.get("data") or {}).get("page") or {}).get("player") or {}
    modes = player.get("modes") or []
    stream_id = modes[0].get("streamId") if modes else None
    title = player.get("title") or "Nieuws (VRT Radio 1)"
    if not stream_id:
        notes.append("graphql ok but no streamId")
        return []
    notes.append("graphql ok")
    token = http_post(VRT_TOKENS, {}, {"content-type": "application/json"}).json().get("vrtPlayerToken", "")
    if not token:
        notes.append("no player token")
        return []
    from urllib.parse import quote
    agg = http_get(f"{VRT_VIDEOS}{quote(stream_id, safe='')}"
                   f"?vrtPlayerToken={quote(token, safe='')}&client=vrtvideo@PROD").json()
    hls = next((t.get("url") for t in agg.get("targetUrls", []) if t.get("type") == "hls"), None)
    if not hls:
        notes.append(f"aggregator ok but no hls ({[t.get('type') for t in agg.get('targetUrls', [])]})")
        return []
    notes.append("aggregator ok (hls)")
    # 'nieuws van 20u00 op Radio 1' → today (or yesterday if in the future) at 20:00 Brussels
    published = None
    tm = re.search(r"(\d{1,2})u(\d{2})", title)
    if tm:
        brussels = ZoneInfo("Europe/Brussels")
        now_b = datetime.now(brussels)
        cand = now_b.replace(hour=int(tm.group(1)), minute=int(tm.group(2)),
                             second=0, microsecond=0)
        if cand > now_b + timedelta(minutes=5):
            cand -= timedelta(days=1)
        published = cand.astimezone(timezone.utc)
    return [{"title": title, "published": published, "audio_url": hls, "format": "hls"}]


# ---------- franceinfo (per-hour "Le journal de …" pages, no RSS exists) ----------
# Slots verified to exist on radiofrance.fr/franceinfo/podcasts (Chrome, July 2026)
FRANCEINFO_SLOTS = ["5h00", "6h00", "7h00", "8h00", "9h00", "10h00", "11h00", "12h00",
                    "13h00", "16h00", "17h00", "18h00", "18h30", "19h00", "22h00", "23h00"]
FR_EP_MP3_RE = re.compile(r"https:[^\"'\\<> ]+?\.mp3(?:\?[^\"'\\<> ]*)?")


def _slot_minutes(slot: str) -> int:
    h, _, m = slot.partition("h")
    return int(h) * 60 + int(m or 0)


def fetch_franceinfo(base: str, notes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """franceinfo publishes each hourly 'Le journal de XXh' as a podcast page but
    with NO RSS feed. Walk backwards from the current Paris time; each show page
    links its episodes newest-first, and the episode page embeds the mp3."""
    notes = notes if notes is not None else []
    now_paris = datetime.now(ZoneInfo("Europe/Paris"))
    cur = now_paris.hour * 60 + now_paris.minute
    by_recent = sorted(FRANCEINFO_SLOTS, key=_slot_minutes, reverse=True)
    ordered = [s for s in by_recent if _slot_minutes(s) <= cur] + \
              [s for s in by_recent if _slot_minutes(s) > cur]  # wrap to yesterday evening
    results: List[Dict[str, Any]] = []
    fetched = 0
    for slot in ordered:
        if len(results) >= 2 or fetched >= 5:
            break
        try:
            fetched += 1
            show = http_get(f"{base}/franceinfo/podcasts/le-journal-de-{slot}").text
            eps = re.findall(rf"/franceinfo/podcasts/le-journal-de-{slot}/[a-z0-9-]{{10,}}", show)
            if not eps:
                notes.append(f"{slot}: no episodes")
                continue
            ep_page = http_get(base + eps[0]).text
            m = FR_EP_MP3_RE.search(ep_page)
            if not m:
                notes.append(f"{slot}: no mp3 on episode page")
                continue
            # date from the slug: ...-du-dimanche-05-juillet-2026-...
            dm = re.search(r"-(\d{2})-([a-zéû]+)-(\d{4})", eps[0])
            published = None
            if dm and FR_MONTHS.get(dm.group(2)):
                published = datetime(
                    int(dm.group(3)), FR_MONTHS[dm.group(2)], int(dm.group(1)),
                    _slot_minutes(slot) // 60, _slot_minutes(slot) % 60,
                    tzinfo=ZoneInfo("Europe/Paris"),
                ).astimezone(timezone.utc)
                if datetime.now(timezone.utc) - published > timedelta(hours=26):
                    notes.append(f"{slot}: stale ({published.date()})")
                    continue
            results.append({
                "title": f"Le journal de {slot}",
                "published": published,
                "audio_url": m.group(0),
            })
            notes.append(f"{slot}: ok")
        except Exception as exc:
            notes.append(f"{slot}: {str(exc)[:60]}")
    return results


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
        "station": "franceinfo — Le journal",
        "type": "franceinfo",
        # franceinfo's hourly "Le journal de XXh" podcast pages (no RSS exists);
        # the fetcher walks backwards from the current Paris hour.
        "base": "https://www.radiofrance.fr",
        "live": "https://icecast.radiofrance.fr/franceinfo-midfi.mp3",
        "fresh_hours": 26,
    },
    "bbc_bulletin": {
        "station": "BBC World Service — News Bulletin",
        "type": "bbc",
        "brand": "p002vsmz",  # hourly 5-minute bulletins (bbc.co.uk/programmes/p002vsmz/episodes/player)
        "fallback_feeds": [
            "https://podcasts.files.bbci.co.uk/p002vsmz.rss",
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
        "type": "rainews",
        "page": "https://www.rainews.it/notiziari/gr1",
        "json_url": "https://www.raiplaysound.it/programmi/gr1.json",
        "live": "https://icestreaming.rai.it/1.mp3",
        "fresh_hours": 8,
    },
    "bnr_horizont": {
        "station": "БНР Хоризонт — Новини",
        "type": "bnr",
        # JSON API discovered via network inspection on binar.bg/news
        "api": "https://binar.bg/api/programs/news/horizont",
        "media_base": "https://binar.bg/api/media",
        "live_lookup": {"uuid": "3a3b1465-6a6f-4289-901c-bd5890cb8370", "name": "BNR Horizont"},
        "fresh_hours": 4,
    },
    "nova_news": {
        "station": "Nova News (live only)",
        "type": "live_only",
        "live_lookup": {"name": "Nova News"},
        "fresh_hours": 0,
    },
    "euronews_ru": {
        "station": "Euronews — Новости дня",
        "type": "euronews",
        "program": "https://ru.euronews.com/programs/top-news-stories-today",
        "fresh_hours": 30,  # three editions per day
    },
    "vrt_radio1": {
        "station": "VRT Radio 1 — Nieuws",
        "type": "vrt",
        "page_id": "/vrtmax/kanalen/radio-1/recentste-nieuws/",
        "live_lookup": {"name": "VRT Radio 1"},
        "fresh_hours": 26,
    },
}

# =====================================================================
# Live-rewind track (parallel to bulletins).
# Browser inspection (July 2026): only BBC WS and RTVE 24h expose live
# streams with a DVR window big enough to rewind to the last full hour.
# All other stations get their :00 recorded bulletin in rewind mode.
# =====================================================================
LIVE_REWIND: Dict[str, Dict[str, Any]] = {
    "en": {
        "type": "dvr",
        "station": "BBC World Service — live",
        "resolver": "bbc_ws",   # mediaselector lookup first (pool number can change)
        "urls": [
            "https://as-hls-ww-live.akamaized.net/pool_07364996/live/ww/"
            "bbc_world_service_news_internet/bbc_world_service_news_internet.isml/"
            "bbc_world_service_news_internet-audio%3d96000.m3u8",
        ],
    },
    "es": {
        "type": "dvr",
        "station": "RTVE Canal 24 horas — en directo",
        "urls": [
            "https://rtvelivestream.rtve.es/rtvesec/24h/24h_main_dvr_576.m3u8",
            "https://rtvelivestream.rtve.es/rtvesec/24h/24h_main_dvr_720.m3u8",
        ],
    },
    # de / fr / it / bg / ru / nl → type "bulletin" (their live streams keep
    # no DVR buffer — verified: DLF icecast, franceinfo 30s, RaiNews24 1min
    # tokenized, BNR 30s). The :00 bulletin IS what aired at the full hour.
}


def _hls_window_minutes(manifest: str) -> int:
    durs = re.findall(r"#EXTINF:([\d.]+)", manifest)
    return int(sum(float(d) for d in durs) / 60)


def _resolve_bbc_ws_live() -> List[str]:
    """mediaselector v2 for the live WS stream; strip .norewind to get the
    DVR-windowed HLS variant."""
    out: List[str] = []
    try:
        ms = http_get(
            "https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/"
            "mediaset/pc/vpid/bbc_world_service/format/json"
        ).json()
        for media in ms.get("media", []) or []:
            for conn in media.get("connection", []) or []:
                href = conn.get("href", "")
                if href.startswith("https") and ".m3u8" in href:
                    out.append(href.replace(".norewind", ""))
    except Exception:
        pass
    return out


@st.cache_data(ttl=600, show_spinner=False)
def get_rewind_stream(lang: str) -> Optional[Dict[str, Any]]:
    """Return a verified DVR live stream for a language, or None."""
    cfg = LIVE_REWIND.get(lang)
    if not cfg or cfg.get("type") != "dvr":
        return None
    candidates: List[str] = []
    if cfg.get("resolver") == "bbc_ws":
        candidates.extend(_resolve_bbc_ws_live())
    candidates.extend(cfg["urls"])
    unverified: Optional[str] = None
    for url in candidates:
        try:
            manifest = http_get(url).text
            if "#EXTM3U" not in manifest:
                continue
            window = _hls_window_minutes(manifest)
            if window >= 65:
                return {"url": url, "window_min": window, "station": cfg["station"],
                        "verified": True}
            if window == 0 and "#EXT-X-STREAM-INF" in manifest:
                unverified = unverified or url  # master playlist — window unknown
        except Exception:
            continue
    if unverified:
        return {"url": unverified, "window_min": 0, "station": cfg["station"],
                "verified": False}
    return None


LANGUAGES: Dict[str, Dict[str, Any]] = {
    "de": {"flag": "🇩🇪", "label": "Deutsch", "sources": ["dlf", "dlf_kultur"]},
    "fr": {"flag": "🇫🇷", "label": "Français", "sources": ["franceinfo"]},
    "en": {"flag": "🇬🇧", "label": "English", "sources": ["bbc_bulletin", "npr_now"]},
    "es": {"flag": "🇪🇸", "label": "Español", "sources": ["rtve_boletines"]},
    "it": {"flag": "🇮🇹", "label": "Italiano", "sources": ["rai_gr1"]},
    "bg": {"flag": "🇧🇬", "label": "Български", "sources": ["bnr_horizont", "nova_news"]},
    "ru": {"flag": "🇷🇺", "label": "Русский", "sources": ["euronews_ru"]},
    "nl": {"flag": "🇧🇪", "label": "Vlaams", "sources": ["vrt_radio1"]},
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
        "title": "", "published": None, "error": "", "debug": "",
        "bulletins": [],
    }
    bulletins: List[Dict[str, Any]] = []
    notes: List[str] = []
    if cfg["type"] != "live_only":
        try:
            if cfg["type"] == "rss":
                bulletins = fetch_rss_latest(cfg["feeds"], cfg.get("title_filter", ""))
            elif cfg["type"] == "raiplaysound":
                bulletins = fetch_raiplaysound(cfg["json_url"])
            elif cfg["type"] == "rainews":
                bulletins = fetch_rainews(cfg["page"], cfg["json_url"], notes)
            elif cfg["type"] == "bbc":
                bulletins = fetch_bbc_brand(cfg["brand"], cfg["fallback_feeds"], notes)
            elif cfg["type"] == "rtve":
                bulletins = fetch_rtve(cfg["slug"])
            elif cfg["type"] == "bnr":
                bulletins = fetch_bnr(cfg["api"], cfg["media_base"], notes)
            elif cfg["type"] == "franceinfo":
                bulletins = fetch_franceinfo(cfg["base"], notes)
            elif cfg["type"] == "euronews":
                bulletins = fetch_euronews(cfg["program"], notes)
            elif cfg["type"] == "vrt":
                bulletins = fetch_vrt(cfg["page_id"], notes)
        except Exception as exc:
            result["error"] = str(exc)[:300]
    result["debug"] = " → ".join(notes)
    result["bulletins"] = [
        {"title": b["title"], "url": b["audio_url"], "published": fmt_dt(b.get("published")),
         "format": b.get("format", "file")}
        for b in (bulletins or [])
    ]

    latest = bulletins[0] if bulletins else None
    fresh = False
    if latest:
        dt = latest.get("published")
        if dt is None:
            fresh = True  # no timestamp — trust the feed ordering
        else:
            window = cfg["fresh_hours"]
            if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                # date-only precision (parsed as midnight) — judge by day, not hour
                window = max(window, 26)
            fresh = (datetime.now(timezone.utc) - dt) <= timedelta(hours=window)

    if latest and fresh:
        result.update(
            mode="bulletin", url=latest["audio_url"],
            title=latest["title"], published=fmt_dt(latest.get("published")),
            format=latest.get("format", "file"),
        )
        return result

    # Live fallback
    live = cfg.get("live")
    if not live and cfg.get("live_lookup"):
        live = resolve_live_stream(**cfg["live_lookup"])
    if live:
        note = "no recent bulletin — live radio" if cfg["type"] != "live_only" else "live radio"
        result.update(mode="live", url=live, title=note)
        return result

    result["error"] = result["error"] or "No bulletin and no live stream found"
    return result


# =====================================================================
# Sidebar — order, sources, refresh
# =====================================================================
try:
    from streamlit_sortables import sort_items
    HAS_SORTABLES = True
except ImportError:
    HAS_SORTABLES = False

if "lang_order" not in st.session_state:
    st.session_state.lang_order = DEFAULT_ORDER.copy()
for _code in LANGUAGES:  # pick up newly added languages
    if _code not in st.session_state.lang_order:
        st.session_state.lang_order.append(_code)
if "enabled" not in st.session_state:
    st.session_state.enabled = {}
for _code in LANGUAGES:
    st.session_state.enabled.setdefault(_code, True)
if "chosen_source" not in st.session_state:
    st.session_state.chosen_source = {}
for _code, _v in LANGUAGES.items():
    st.session_state.chosen_source.setdefault(_code, _v["sources"][0])


def move_lang(code: str, delta: int) -> None:
    order = st.session_state.lang_order
    i = order.index(code)
    j = i + delta
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]


LABEL_TO_CODE = {f"{v['flag']} {v['label']}": k for k, v in LANGUAGES.items()}

with st.sidebar:
    st.header("⚙️ Languages")
    if HAS_SORTABLES:
        st.caption("Drag to set the play order. Toggle off what you skip today.")
        labels = [f"{LANGUAGES[c]['flag']} {LANGUAGES[c]['label']}"
                  for c in st.session_state.lang_order]
        sorted_labels = sort_items(labels, direction="vertical", key="lang_sortable")
        new_order = [LABEL_TO_CODE[lb] for lb in sorted_labels if lb in LABEL_TO_CODE]
        if new_order and new_order != st.session_state.lang_order:
            st.session_state.lang_order = new_order
    else:
        st.caption("Order = play order (↑/↓). Toggle off what you skip today.")
    for code in st.session_state.lang_order:
        lang = LANGUAGES[code]
        if HAS_SORTABLES:
            st.session_state.enabled[code] = st.checkbox(
                f"{lang['flag']} {lang['label']}",
                value=st.session_state.enabled[code],
                key=f"en_{code}",
            )
        else:
            c1, c2, c3 = st.columns([3, 1, 1])
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

play_mode = st.radio(
    "Mode",
    ["📰 Hourly bulletins", "⏪ Live, rewound to :00"],
    horizontal=True,
    label_visibility="collapsed",
    key="play_mode",
)
IS_REWIND = play_mode.startswith("⏪")
if IS_REWIND:
    st.caption(
        "True live rewind where the broadcaster keeps a buffer (BBC, RTVE); "
        "everywhere else you hear that station's recorded :00 bulletin — "
        "the same thing its live stream aired at the full hour."
    )

playlist: List[Dict[str, Any]] = []
problems: List[str] = []
active_codes = [c for c in st.session_state.lang_order if st.session_state.enabled[c]]

with st.spinner("Fetching the latest bulletins…"):
    for code in active_codes:
        base_item = {
            "lang": code,
            "flag": LANGUAGES[code]["flag"],
            "label": LANGUAGES[code]["label"],
        }
        if IS_REWIND:
            rw = get_rewind_stream(code)
            if rw:
                playlist.append({
                    **base_item,
                    "station": rw["station"],
                    "mode": "rewind",
                    "url": rw["url"],
                    "title": "Live stream, rewound to the last full hour",
                    "published": (f"DVR window ≈ {rw['window_min']} min"
                                  if rw.get("verified") else "DVR window unverified"),
                    "format": "hls",
                    "alt": None,
                })
                continue
            # no DVR stream for this language → recorded :00 bulletin below
        source_id = st.session_state.chosen_source[code]
        res = get_source_result(source_id)
        if res["url"]:
            alt = None
            if res["mode"] == "bulletin" and len(res.get("bulletins", [])) > 1:
                alt = res["bulletins"][1]
            playlist.append({
                **base_item,
                "station": res["station"] + (" · :00 bulletin" if IS_REWIND and res["mode"] == "bulletin" else ""),
                "mode": res["mode"],
                "url": res["url"],
                "title": res["title"],
                "published": res["published"] or "",
                "format": res.get("format", "file"),
                "alt": alt,
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
<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js"></script>
<style>
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
  .altrow { text-align: center; margin-top: 12px; min-height: 22px; }
  .altbtn { background: none; border: 1px solid rgba(255,255,255,.22); color: #cbd5e1;
            border-radius: 999px; padding: 4px 12px; font-size: 12px; cursor: pointer; }
  .altbtn:hover { background: rgba(255,255,255,.1); }
  .altbtn.on { border-color: #f97316; color: #f97316; }
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
  <div class="altrow" id="altrow"></div>
  <div class="foot" id="foot"></div>
</div>
<script>
const PLAYLIST = __PLAYLIST__;
let idx = 0, started = false, onAlt = false, hls = null;
const audio = new Audio();
audio.preload = "none";
// Hidden sink for live-rewind HLS (RTVE's stream carries video, a bare Audio
// element can't take it). Bulletins keep using the proven `audio` element.
const liveEl = document.createElement("video");
liveEl.style.display = "none";
liveEl.playsInline = true;
liveEl.preload = "none";
document.body.appendChild(liveEl);
let media = audio;
const $ = id => document.getElementById(id);

function switchMedia(el) {
  if (media !== el) { try { media.pause(); } catch(e){} media = el; }
}

function seekToLastFullHour() {
  const now = new Date();
  const past = now.getMinutes() * 60 + now.getSeconds();  // seconds since :00
  let tries = 0;
  const attempt = () => {
    tries++;
    try {
      const sk = media.seekable;
      if (sk.length && sk.end(sk.length - 1) > 0) {
        const start = sk.start(0), end = sk.end(sk.length - 1);
        media.currentTime = Math.max(start + 4, end - past);
        $("when").textContent = "⏪ rewound to " +
          String(now.getHours()).padStart(2, "0") + ":00 — " +
          Math.round(past / 60) + " min behind live";
        return;
      }
    } catch(e) {}
    if (tries < 24) setTimeout(attempt, 500);
  };
  attempt();
}

function setSource(item) {
  if (hls) { hls.destroy(); hls = null; }
  const isRewind = item.mode === "rewind";
  switchMedia(isRewind ? liveEl : audio);
  const isHls = item.format === "hls" || /\.m3u8/.test(item.url);
  if (isHls && window.Hls && Hls.isSupported()) {
    hls = new Hls(isRewind ? {liveDurationInfinity: true} : {});
    hls.loadSource(item.url);
    hls.attachMedia(media);
    if (isRewind) hls.once(Hls.Events.LEVEL_LOADED, () => seekToLastFullHour());
  } else {
    media.src = item.url;  // Safari plays HLS natively; files play everywhere
    if (isRewind) media.addEventListener("loadedmetadata", () => seekToLastFullHour(), {once: true});
  }
}

function renderChips() {
  $("chips").innerHTML = "";
  PLAYLIST.forEach((item, i) => {
    const c = document.createElement("div");
    c.className = "chip" + (i === idx ? " active" : "");
    c.innerHTML = item.flag + " " + item.label
      + (item.mode === "live" ? ' <span class="lv">LIVE</span>' : "")
      + (item.mode === "rewind" ? ' <span class="lv" style="background:#0ea5e9">⏪</span>' : "");
    c.onclick = () => playIndex(i);
    $("chips").appendChild(c);
  });
}
function current() {
  const item = PLAYLIST[idx];
  return (onAlt && item.alt) ? {...item, title: item.alt.title, url: item.alt.url,
                                published: item.alt.published, mode: "bulletin",
                                format: item.alt.format || item.format} : item;
}
function renderNow() {
  const item = current();
  $("station").textContent = item.station;
  $("title").textContent = started ? item.title : "Press play to start your briefing";
  $("when").textContent = item.mode === "live" ? "● live stream"
      : item.mode === "rewind" ? "⏪ live rewind — " + (item.published || "")
      : ((item.published || "latest bulletin") + (onAlt ? "  · older bulletin" : ""));
  $("foot").textContent = (idx + 1) + " / " + PLAYLIST.length + " — auto-advances when a bulletin ends";
  const base = PLAYLIST[idx];
  if (base.alt) {
    $("altrow").innerHTML = '<button class="altbtn' + (onAlt ? ' on' : '') + '" id="altbtn">' +
      (onAlt ? "▶ back to latest" : "▶ one bulletin earlier" +
        (base.alt.published ? " (" + base.alt.published + ")" : "")) + '</button>';
    document.getElementById("altbtn").onclick = () => { onAlt = !onAlt; playIndex(idx, true); };
  } else {
    $("altrow").innerHTML = "";
  }
  renderChips();
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: item.title || item.station, artist: item.station, album: "Morning News Radio" });
  }
}
function playIndex(i, keepAlt) {
  const newIdx = (i + PLAYLIST.length) % PLAYLIST.length;
  if (!keepAlt) onAlt = false;
  idx = newIdx;
  started = true;
  const item = current();
  setSource(item);
  media.play().catch(() => {});
  renderNow();
  $("play").textContent = "⏸";
}
$("play").onclick = () => {
  if (!started) { playIndex(0); return; }
  if (media.paused) { media.play().catch(()=>{}); $("play").textContent = "⏸"; }
  else { media.pause(); $("play").textContent = "▶"; }
};
$("next").onclick = () => playIndex(idx + 1);
$("prev").onclick = () => playIndex(idx - 1);
function nudge(delta) {
  try {
    const sk = media.seekable;
    if (!sk.length) return;
    media.currentTime = Math.max(sk.start(0),
      Math.min(sk.end(sk.length - 1) - 1, media.currentTime + delta));
  } catch(e) {}
}
$("back").onclick = () => nudge(-15);
$("fwd").onclick  = () => nudge(15);
function onEnded() {
  if (media !== audio) return;  // live streams don't end
  if (idx + 1 < PLAYLIST.length) playIndex(idx + 1);
  else { $("play").textContent = "▶"; $("title").textContent = "Briefing finished ✓"; }
}
function onTime() {
  if (isFinite(media.duration) && media.duration > 0)
    $("fill").style.width = (100 * media.currentTime / media.duration) + "%";
  else $("fill").style.width = "100%";
}
function onErr() {
  $("title").textContent = "⚠ Could not play — skipping in 2s";
  if (started) setTimeout(() => { if (idx + 1 < PLAYLIST.length) playIndex(idx + 1); }, 2000);
}
for (const el of [audio, liveEl]) {
  el.onended = onEnded; el.ontimeupdate = onTime; el.onerror = onErr;
}
$("bar").onclick = (e) => {
  if (!isFinite(media.duration)) return;
  const r = $("bar").getBoundingClientRect();
  media.currentTime = media.duration * (e.clientX - r.left) / r.width;
};
if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("nexttrack", () => playIndex(idx + 1));
  navigator.mediaSession.setActionHandler("previoustrack", () => playIndex(idx - 1));
}
renderNow();
</script></body></html>
""".replace("__PLAYLIST__", playlist_json)
    components.html(player_html, height=480)

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
            for i, b in enumerate(res.get("bulletins", [])[:2]):
                label = "Latest" if i == 0 else "Previous"
                st.write(f"{label}: “{b['title']}” {('— ' + b['published']) if b['published'] else ''}")
                if b.get("format") == "hls":
                    st.caption("HLS stream — plays in the main player above (this test widget can't preview HLS).")
                    st.code(b["url"], language=None)
                else:
                    st.audio(b["url"])
            if res["mode"] == "live" and res["url"]:
                st.write("Live stream in use:")
                st.audio(res["url"])
            if res["error"]:
                st.code(res["error"], language=None)
            if res.get("debug"):
                st.caption(f"Trace: {res['debug']}")
            st.divider()
    st.markdown("**⏪ Live-rewind streams (DVR)**")
    for code, cfg in LIVE_REWIND.items():
        rw = get_rewind_stream(code)
        if rw:
            icon = "✅" if rw.get("verified") else "⚠️"
            st.write(f"{icon} {LANGUAGES[code]['flag']} {rw['station']} — "
                     f"DVR window ≈ {rw['window_min']} min")
            st.code(rw["url"], language=None)
        else:
            st.write(f"❌ {LANGUAGES[code]['flag']} {cfg['station']} — no DVR stream reachable")
    st.caption("All other stations keep no live buffer (verified July 2026) — "
               "rewind mode plays their recorded :00 bulletin instead.")
