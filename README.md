# 📻 Morning News Radio

One-tap multilingual morning news: press play, hear the latest news bulletin per language (🇩🇪 🇫🇷 🇬🇧 🇪🇸 🇮🇹 🇧🇬), auto-advancing through your chosen order. Everything plays inside the app — no external tabs.

## How it solves the "rewind to the full hour" problem

It doesn't seek live streams at all. Instead, the Python backend fetches each broadcaster's **recorded bulletin** (podcast RSS / JSON APIs / embedded page data) server-side and hands the browser a direct audio file that naturally starts at 0:00 of the bulletin. Live streams are only used as a fallback when no fresh bulletin exists.

All four problem sources were fixed by inspecting the real players in Chrome (network + embedded data):

| Language | Bulletin source | Method (verified in browser) |
|---|---|---|
| 🇩🇪 | DLF Nachrichten | RSS (verified, same as your prototype) |
| 🇫🇷 | franceinfo "Le journal de …" | franceinfo's 16 hourly journal podcast pages have **no RSS**; fetcher walks back from the current Paris hour, episode pages embed the mp3 |
| 🇬🇧 | BBC WS News Bulletin (hourly) | Sounds `rms` API episode ids → mediaselector **v3** (`cvid/urn:bbc:pips:pid:…`) → HLS audio master, played via **hls.js** |
| 🇪🇸 | RNE Boletines | RTVE open API — program id resolved at runtime |
| 🇮🇹 | Rai GR1 | Edition list (title + ISO date + relinker URL) parsed from the page's `data` attributes; relinker `cont=` tokens are non-numeric; plays directly in `<audio>` (tested: 304s edition) |
| 🇧🇬 | БНР Хоризонт | Hidden JSON API `binar.bg/api/programs/news/horizont` → `NewsAudio` uuid → `binar.bg/api/media/{uuid}` (verified server-side) |
| 🇷🇺 | Euronews «Новости дня» | Programme page lists episodes (3 editions/day); episode pages embed a direct **mp4** — the audio player plays its audio track |
| 🇧🇪 | VRT Radio 1 nieuws | Public GraphQL (`page → player → modes → streamId`) → anonymous player token → media aggregator → HLS (hls.js) |

Every source returns the **latest two** bulletins: the player shows a "one bulletin earlier" button, and Diagnostics lists both with test players (BBC's HLS URLs can't preview in the Diagnostics widget — main player only).

Live fallbacks are resolved via radio-browser.info when no static URL is configured (avoids dead stream URLs and http/https mixed-content blocks).

## ⏪ Live-rewind mode (parallel track)

A toggle above the player switches between **Hourly bulletins** and **Live, rewound to :00**. In rewind mode, at e.g. 07:11 you hear the live stream from 07:00 (11 minutes behind live), with full scrubbing via ±15s and next/prev as usual.

Physics check (browser-verified, July 2026): rewinding requires the broadcaster to keep a DVR buffer.

| Station | Live buffer | Rewind mode behaviour |
|---|---|---|
| 🇬🇧 BBC WS | ~6 h HLS DVR (public) | ✅ true rewind — URL auto-resolved via mediaselector (`.norewind` stripped) |
| 🇪🇸 RTVE 24h | ~111 min HLS DVR (public `_dvr_` variant) | ✅ true rewind (video stream; audio plays via hidden sink) |
| 🇩🇪 DLF | Icecast only | :00 Nachrichten bulletin |
| 🇫🇷 franceinfo | ~30 s window | :00 journal bulletin |
| 🇮🇹 RaiNews24 | ~1 min, tokenized | :00 GR1 edition |
| 🇧🇬 BNR | ~30 s window | :00 емисия |

Seek math: `position = liveEdge − (minutes past the hour)` — no timestamps needed, works on any DVR stream. Diagnostics shows each DVR stream's measured window.

## Deploy (same as your prototype)

1. Replace `app.py` and `requirements.txt` in your GitHub repo.
2. Streamlit Cloud redeploys automatically (or "Reboot app" in the dashboard).
3. **Open the 🩺 Diagnostics panel** at the bottom of the app first thing — it shows, per source: ✅ bulletin found / 📡 live fallback / ❌ error, the exact title + timestamp fetched, and a test player.

## First-run checklist (Diagnostics panel)

Each Diagnostics entry shows a **Trace** line: which strategy was tried, which one answered, and why others failed — paste that trace back if a source misbehaves.

Notes:

- **🇫🇷** now delivers the true franceinfo hourly journal (5h–23h slots incl. 18h30; no 14h/15h/20h/21h editions exist as podcasts).
- **🇬🇧** bulletins are HLS — they play in the main player (hls.js) but not in the small Diagnostics preview widget.
- **🇮🇹** relinker URLs may be IP-checked at resolution time; the app hands your browser the original relinker (exactly what rainews.it itself does), so playback happens with your IP.

## Customizing

- **Order**: drag the language cards in the sidebar into any order (requires the `streamlit-sortables` package from requirements.txt; falls back to ↑/↓ arrows if missing). On-off checkboxes and per-language source dropdowns sit below.
- **Add a station**: add an entry to `SOURCES` (types: `rss`, `raiplaysound`, `rtve`, `bnr`, `live_only`) and reference it in `LANGUAGES[...]["sources"]`.
- **Freshness window**: `fresh_hours` per source decides when a bulletin is "too old" → live fallback.
- Bulletins are cached 4 minutes; "🔄 Refresh bulletins" forces a refetch.

## Phone tip

Open the Streamlit URL in your phone browser → "Add to Home Screen". The player supports lock-screen next/previous track controls (Media Session API).
