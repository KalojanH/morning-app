# 📻 Morning News Radio

One-tap multilingual morning news: press play, hear the latest news bulletin per language (🇩🇪 🇫🇷 🇬🇧 🇪🇸 🇮🇹 🇧🇬), auto-advancing through your chosen order. Everything plays inside the app — no external tabs.

## How it solves the "rewind to the full hour" problem

It doesn't seek live streams at all. Instead, the Python backend fetches each broadcaster's **recorded bulletin** (podcast RSS / JSON APIs / embedded page data) server-side and hands the browser a direct audio file that naturally starts at 0:00 of the bulletin. Live streams are only used as a fallback when no fresh bulletin exists.

| Language | Bulletin source | Method |
|---|---|---|
| 🇩🇪 | DLF Nachrichten | RSS (verified, same as your prototype) |
| 🇫🇷 | Radio France Journaux | 7 per-hour journal feeds (6h30, 7h30 w-e, 8h, 18h, 19h, 23h, France Culture) — newest wins |
| 🇬🇧 | BBC WS News Bulletin | Sounds `rms` API → `playlist.json` → mediaselector mp3; RSS + NPR News Now fallbacks |
| 🇪🇸 | RNE Boletines | RTVE open API — program id resolved at runtime |
| 🇮🇹 | Rai GR1 | rainews.it/notiziari/gr1 page (relinker media URL) + RaiPlaySound JSON fallback |
| 🇧🇬 | БНР Хоризонт | `__NEXT_DATA__` + Next.js `/_next/data/` route on binar.bg / bnrnews.bg |

Live fallbacks are resolved via radio-browser.info when no static URL is configured (avoids dead stream URLs and http/https mixed-content blocks).

## Deploy (same as your prototype)

1. Replace `app.py` and `requirements.txt` in your GitHub repo.
2. Streamlit Cloud redeploys automatically (or "Reboot app" in the dashboard).
3. **Open the 🩺 Diagnostics panel** at the bottom of the app first thing — it shows, per source: ✅ bulletin found / 📡 live fallback / ❌ error, the exact title + timestamp fetched, and a test player.

## First-run checklist (Diagnostics panel)

Some sources were built against documented APIs I could not fully verify from here. Each Diagnostics entry now shows a **Trace** line: which strategy was tried, which one answered, and why others failed — paste that trace back to me if a source still misbehaves.

- **DLF / 🇫🇷 Journaux** — should be ✅ (French now covers mornings and weekends).
- **BBC** — tries the Sounds API for the latest 5-min bulletin episode of `p002vsmz`; falls back to Global News Podcast RSS. NPR News Now stays available as switchable hourly alternative.
- **RAI** — scrapes the GR1 notiziari page for the newest edition's media URL. Note: RAI's relinker occasionally geo-restricts; if the bulletin won't *play* (but shows ✅), tell me — the trace will distinguish fetch vs playback problems.
- **БНР** — parses the embedded page data and the Next.js data route; trace shows how many audio candidates were found.

## Customizing

- **Order / on-off / source per language**: sidebar (↑ ↓ arrows, checkboxes, dropdowns).
- **Add a station**: add an entry to `SOURCES` (types: `rss`, `raiplaysound`, `rtve`, `bnr`, `live_only`) and reference it in `LANGUAGES[...]["sources"]`.
- **Freshness window**: `fresh_hours` per source decides when a bulletin is "too old" → live fallback.
- Bulletins are cached 4 minutes; "🔄 Refresh bulletins" forces a refetch.

## Phone tip

Open the Streamlit URL in your phone browser → "Add to Home Screen". The player supports lock-screen next/previous track controls (Media Session API).
