# 📻 Morning News Radio

One-tap multilingual morning news: press play, hear the latest news bulletin per language (🇩🇪 🇫🇷 🇬🇧 🇪🇸 🇮🇹 🇧🇬), auto-advancing through your chosen order. Everything plays inside the app — no external tabs.

## How it solves the "rewind to the full hour" problem

It doesn't seek live streams at all. Instead, the Python backend fetches each broadcaster's **recorded bulletin** (podcast RSS / JSON APIs / embedded page data) server-side and hands the browser a direct audio file that naturally starts at 0:00 of the bulletin. Live streams are only used as a fallback when no fresh bulletin exists.

| Language | Bulletin source | Method |
|---|---|---|
| 🇩🇪 | DLF Nachrichten | RSS (verified, same as your prototype) |
| 🇫🇷 | franceinfo Journal | per-hour journal RSS (`rss_11736` verified = Journal de 19h) |
| 🇬🇧 | BBC WS News / NPR News Now | BBC podcast CDN RSS + NPR hourly RSS (switchable) |
| 🇪🇸 | RNE Boletines | RTVE open API — program id resolved at runtime |
| 🇮🇹 | Rai GR1 | RaiPlaySound `programmi/gr1.json` API |
| 🇧🇬 | БНР Хоризонт | server-side parse of `__NEXT_DATA__` on binar.bg / bnrnews.bg |

Live fallbacks are resolved via radio-browser.info when no static URL is configured (avoids dead stream URLs and http/https mixed-content blocks).

## Deploy (same as your prototype)

1. Replace `app.py` and `requirements.txt` in your GitHub repo.
2. Streamlit Cloud redeploys automatically (or "Reboot app" in the dashboard).
3. **Open the 🩺 Diagnostics panel** at the bottom of the app first thing — it shows, per source: ✅ bulletin found / 📡 live fallback / ❌ error, the exact title + timestamp fetched, and a test player.

## First-run checklist (Diagnostics panel)

Some sources were built against documented APIs I could not fully verify from here. Expected states:

- **DLF** — should be ✅ immediately.
- **franceinfo** — ✅ only near/after 19h (only the Journal de 19h feed is confirmed). To get morning journals: find more `rss_XXXXX` IDs (Journal de 6h/7h/8h) — search "site:radiofrance-podcast.net journal" or use https://radio-france-rss.aerion.workers.dev — and add them to the `feeds` list of `franceinfo` in `SOURCES`. The app automatically plays the newest across all listed feeds.
- **BBC** — if `p002vsmz.rss` is empty it silently uses the Global News Podcast; switch the English source to **NPR News Now** in the sidebar for a true hourly 5-minute bulletin.
- **RTVE / RAI / БНР** — check Diagnostics; if ❌, the error message shows which URL failed. The fetchers are defensive (they scan the returned JSON for any audio), so partial API changes usually still work.

## Customizing

- **Order / on-off / source per language**: sidebar (↑ ↓ arrows, checkboxes, dropdowns).
- **Add a station**: add an entry to `SOURCES` (types: `rss`, `raiplaysound`, `rtve`, `bnr`, `live_only`) and reference it in `LANGUAGES[...]["sources"]`.
- **Freshness window**: `fresh_hours` per source decides when a bulletin is "too old" → live fallback.
- Bulletins are cached 4 minutes; "🔄 Refresh bulletins" forces a refetch.

## Phone tip

Open the Streamlit URL in your phone browser → "Add to Home Screen". The player supports lock-screen next/previous track controls (Media Session API).
