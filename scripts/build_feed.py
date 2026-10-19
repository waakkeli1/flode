#!/usr/bin/env python3
"""Flöde – hakurobotti.

Rakentaa site/feed.json mekaanisesti, ilman tekoälyä:
  1. Ratkaisee kanavien @-nimet kanava-ID:iksi.
  2. Lukee jokaisen kanavan YouTube-RSS-syötteen (15 uusinta videota).
  3. Tunnistaa Shortsit (youtube.com/shorts/ID vastaa 200, muut ohjautuvat).
  4. Yrittää hakea ruotsinkielisen tekstityksen (youtube-transcript-api).
     Jos YouTube estää pilvipalvelimet, yritykset lopetetaan siltä ajolta.
  5. Laskee vaikeustason 1–5 sanojen yleisyyden perusteella.
  6. Pilkkoo pitkät videot 20–45 s reeleiksi tekstityksen taukojen kohdalta.
Edellisen ajon tulokset luetaan julkaistusta feed.jsonista välimuistiksi.
"""
import json, os, re, time, random, http.client, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "channels.json")
OUT = os.path.join(ROOT, "site", "feed.json")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
FREQ_URL = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/sv/sv_50k.txt"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}

MAX_SHORT_CHECKS = 300      # uusia Shorts-tarkistuksia per ajo
MAX_TRANSCRIPTS = 120       # uusia tekstitysyrityksiä per ajo
RETRY_DAYS = 3              # epäonnistunut tekstitys yritetään uudelleen tämän jälkeen
KEEP_DAYS = 120             # vanhemmat videot pudotetaan feedistä
WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)


def log(*a):
    print(*a, flush=True)


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "sv-SE,sv;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        log("  ! verkkovirhe:", url, e)
        return 0, ""


def status_no_redirect(url, timeout=15):
    u = urlparse(url)
    try:
        c = http.client.HTTPSConnection(u.netloc, timeout=timeout)
        c.request("GET", u.path + ("?" + u.query if u.query else ""), headers={"User-Agent": UA})
        s = c.getresponse().status
        c.close()
        return s
    except Exception:
        return 0


def _read_json(text):
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_previous():
    """Yhdistää julkaistun feedin ja repoon ladatun site/feed.jsonin välimuistiksi.
    Repoon voi siis ladata kotikoneella ajetun feedin, jos YouTube estää GitHubin palvelimet."""
    sources = []
    if SITE_URL:
        s, body = get(SITE_URL + "/feed.json")
        if s == 200:
            sources.append(_read_json(body))
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            sources.append(_read_json(f.read()))
    merged = {"videos": {}, "channels": []}
    for src in sources:
        for vid, v in (src.get("videos") or {}).items():
            cur = merged["videos"].get(vid)
            if cur is None or (v.get("cues") and not cur.get("cues")):
                merged["videos"][vid] = v
        merged["channels"] += src.get("channels") or []
    log(f"Välimuisti: {len(merged['videos'])} videota, "
        f"{sum(1 for v in merged['videos'].values() if v.get('cues'))} tekstityksellä.")
    return merged


def resolve_channel(entry, known):
    if entry.get("channel_id"):
        return entry["channel_id"]
    handle = entry.get("handle", "").strip()
    if not handle:
        return None
    if handle in known:
        return known[handle]
    s, html = get("https://www.youtube.com/" + (handle if handle.startswith("@") else "@" + handle))
    if s != 200:
        return None
    for pat in (r'"externalId":"(UC[\w-]{22})"',
                r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                r'"channelId":"(UC[\w-]{22})"'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def fetch_rss(channel_id):
    s, xml = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    if s != 200 or not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        group = e.find("media:group", NS)
        thumb, views = "", 0
        if group is not None:
            t = group.find("media:thumbnail", NS)
            thumb = t.get("url", "") if t is not None else ""
            st = group.find("media:community/media:statistics", NS)
            if st is not None:
                try:
                    views = int(st.get("views", "0"))
                except ValueError:
                    views = 0
        out.append({
            "vid": vid,
            "title": e.findtext("a:title", default="", namespaces=NS),
            "published": e.findtext("a:published", default="", namespaces=NS),
            "thumb": thumb, "views": views,
        })
    return out


# ------------------------------------------------------------------ tekstitykset
TRANSCRIPT_BLOCKED = False


def get_transcript(vid, lang):
    """Palauttaa listan [alku, loppu, teksti] tai None."""
    global TRANSCRIPT_BLOCKED
    if TRANSCRIPT_BLOCKED:
        return None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        TRANSCRIPT_BLOCKED = True
        log("  youtube-transcript-api puuttuu, tekstityksiä ei haeta.")
        return None
    try:
        api = YouTubeTranscriptApi()
        if hasattr(api, "fetch"):
            fetched = api.fetch(vid, languages=[lang])
            raw = [(sn.text, float(sn.start), float(sn.duration)) for sn in fetched]
        else:
            data = YouTubeTranscriptApi.get_transcript(vid, languages=[lang])
            raw = [(d["text"], float(d["start"]), float(d["duration"])) for d in data]
    except Exception as e:
        name = type(e).__name__
        if name in ("RequestBlocked", "IpBlocked", "TooManyRequests", "YouTubeRequestFailed", "PoTokenRequired"):
            TRANSCRIPT_BLOCKED = True
            log(f"  ! YouTube esti tekstitysten haun ({name}). Lopetetaan yritykset tältä ajolta.")
        return None
    return merge_cues(raw)


def clean_text(t):
    t = re.sub(r"\[[^\]]*\]|\([^)]*musik[^)]*\)", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t.replace("\n", " ")).strip()


def merge_cues(raw):
    """Yhdistää automaattitekstityksen sirpaleet luettaviksi repliikeiksi."""
    cues, cur = [], None
    for text, start, dur in raw:
        text = clean_text(text)
        if not text:
            continue
        end = start + max(dur, 0.3)
        if cur and (start - cur[1] > 1.0 or len(cur[2]) + len(text) > 84 or cur[1] - cur[0] > 6.5
                    or re.search(r"[.!?…]$", cur[2])):
            cues.append(cur)
            cur = None
        if cur:
            cur[1] = max(cur[1], end)
            cur[2] = cur[2] + " " + text
        else:
            cur = [start, end, text]
    if cur:
        cues.append(cur)
    for i in range(len(cues) - 1):
        cues[i][1] = min(cues[i][1], cues[i + 1][0])
    return [[round(a, 2), round(b, 2), t] for a, b, t in cues if b > a]


def make_clips(cues, min_len=20, max_len=45, max_clips=6):
    """Pilkkoo pitkän videon reeleiksi repliikkien rajoilta."""
    clips, start, last_end = [], None, None
    for i, (a, b, t) in enumerate(cues):
        if start is None:
            start = a
        last_end = b
        nxt = cues[i + 1][0] if i + 1 < len(cues) else None
        gap = (nxt - b) if nxt is not None else 99
        length = b - start
        if (length >= min_len and (gap >= 1.0 or re.search(r"[.!?…]$", t))) or length >= max_len:
            clips.append((round(max(0, start - 0.3), 2), round(b + 0.4, 2)))
            start = None
    if start is not None and last_end - start >= 12:
        clips.append((round(max(0, start - 0.3), 2), round(last_end + 0.4, 2)))
    if len(clips) > max_clips:  # valitaan tasaisesti eri kohdista
        step = len(clips) / max_clips
        clips = [clips[int(i * step)] for i in range(max_clips)]
    return clips


# ------------------------------------------------------------------ vaikeustaso
def load_freq():
    s, body = get(FREQ_URL, timeout=40)
    if s != 200:
        log("  ! sanalistaa ei saatu, vaikeustasoja ei lasketa.")
        return {}
    ranks = {}
    for i, line in enumerate(body.splitlines()):
        w = line.split(" ")[0].strip().lower()
        if w and w not in ranks:
            ranks[w] = i + 1
    log(f"Sanalista: {len(ranks)} sanaa.")
    return ranks


def level_of(cues, ranks):
    if not ranks or not cues:
        return None
    toks = [w.lower() for _, _, t in cues for w in WORD.findall(t)]
    if len(toks) < 8:
        return None
    share = sum(1 for w in toks if ranks.get(w, 10**9) <= 2000) / len(toks)
    for lvl, lim in ((1, 0.92), (2, 0.87), (3, 0.82), (4, 0.76)):
        if share >= lim:
            return lvl
    return 5


# ------------------------------------------------------------------ pääohjelma
def main():
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    lang = cfg.get("language", "sv")
    prev = load_previous()
    prev_videos = prev.get("videos", {})
    known_ids = {c["handle"]: c["id"] for c in prev.get("channels", []) if c.get("handle") and c.get("id")}
    ranks = load_freq()
    now = datetime.now(timezone.utc)

    channels, videos, items = [], {}, []
    short_checks = transcripts = 0

    for entry in cfg.get("channels", []):
        name, cat = entry.get("name") or entry.get("handle"), entry.get("category", "Övrigt")
        cid = resolve_channel(entry, known_ids)
        if not cid:
            log(f"✗ {name}: kanavaa ei löytynyt ({entry.get('handle')}). Tarkista @-nimi.")
            channels.append({"handle": entry.get("handle"), "name": name, "category": cat, "id": None, "ok": False})
            continue
        rss = fetch_rss(cid)
        log(f"✓ {name}: {len(rss)} videota")
        channels.append({"handle": entry.get("handle"), "name": name, "category": cat, "id": cid, "ok": bool(rss)})

        for v in rss:
            vid = v["vid"]
            try:
                age = (now - datetime.fromisoformat(v["published"].replace("Z", "+00:00"))).days
            except Exception:
                age = 0
            if age > KEEP_DAYS:
                continue
            pv = prev_videos.get(vid, {})
            rec = {"short": pv.get("short"), "cues": pv.get("cues"), "level": pv.get("level"), "tried": pv.get("tried", 0)}

            if rec["short"] is None and short_checks < MAX_SHORT_CHECKS:
                short_checks += 1
                st = status_no_redirect(f"https://www.youtube.com/shorts/{vid}")
                rec["short"] = True if st == 200 else (False if st in (301, 302, 303, 307, 308) else None)
                time.sleep(0.15)

            stale = time.time() - rec["tried"] > RETRY_DAYS * 86400
            if rec["cues"] is None and stale and transcripts < MAX_TRANSCRIPTS and not TRANSCRIPT_BLOCKED:
                transcripts += 1
                rec["tried"] = int(time.time())
                cues = get_transcript(vid, lang)
                if cues:
                    rec["cues"] = cues
                time.sleep(0.4 + random.random() * 0.4)
            if rec["cues"] and rec["level"] is None:
                rec["level"] = level_of(rec["cues"], ranks)
            videos[vid] = rec

            base = {"vid": vid, "title": v["title"], "channel": name, "cat": cat, "published": v["published"][:10],
                    "thumb": v["thumb"], "views": v["views"], "level": rec["level"], "subs": bool(rec["cues"])}
            if rec["short"] or (rec["short"] is None and not rec["cues"]):
                # Shortsit aina mukaan; tuntematon tyyppi ilman tekstitystä kohdellaan kuin Short
                items.append({**base, "id": vid, "kind": "short", "start": 0, "end": None})
            elif rec["cues"]:
                for a, b in make_clips(rec["cues"]):
                    items.append({**base, "id": f"{vid}@{int(a)}", "kind": "clip", "start": a, "end": b})
            # pitkät videot ilman tekstitystä jätetään pois: niitä ei voi pilkkoa järkevästi

    items.sort(key=lambda x: x["published"], reverse=True)
    feed = {
        "version": 1,
        "generated": now.isoformat(timespec="seconds"),
        "language": lang,
        "transcripts_blocked": TRANSCRIPT_BLOCKED,
        "channels": channels,
        "items": items,
        "videos": {k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in videos.items()},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, separators=(",", ":"))
    with_subs = sum(1 for i in items if i["subs"])
    log(f"\nValmis: {len(items)} korttia ({with_subs} tekstitettyä), {len(videos)} videota, "
        f"{sum(1 for c in channels if c['ok'])}/{len(channels)} kanavaa toimii.")
    if TRANSCRIPT_BLOCKED:
        log("Huom: YouTube esti tekstitysten haun GitHubin palvelimilta. Feed toimii silti, "
            "tekstitykset näytetään YouTuben omina.")


if __name__ == "__main__":
    main()
