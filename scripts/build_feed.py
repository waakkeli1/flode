#!/usr/bin/env python3
"""Flöde – hakurobotti v2.

Rakentaa site/feed.json mekaanisesti, ilman tekoälyä. Lähteet:
  A) Kanavalista (channels.json): Shortsit omasta soittolistastaan (UUSH…) ja pitkät
     videot omastaan (UULF…), joten tyyppiä ei tarvitse arvata.
  B) Hakusanat: YouTuben haku alle 4 minuutin videoille. Otsikon pitää näyttää ruotsilta.
  C) Automaattinen kanavalöytö: kanava, joka osuu hakuihin toistuvasti ruotsinkielisillä
     videoilla, otetaan seurantaan.
Pitkistä videoista tehdään klippejä: tekstityksen tauoista (jos tekstitys saatiin) tai
YouTuben "eniten uudelleen katsottu" -kohdasta (paras hetki).
Kirjasto kertyy ajosta toiseen: edellinen feed.json luetaan pohjaksi.
"""
import json, os, re, time, random, http.client, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse, quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "channels.json")
OUT = os.path.join(ROOT, "site", "feed.json")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
FREQ_URL = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/sv/sv_50k.txt"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}

MAX_ITEMS = 5000          # kirjaston enimmäiskoko
KEEP_DAYS = 365           # vanhemmat pudotetaan
MAX_TRANSCRIPTS = 120     # tekstitysyrityksiä per ajo
MAX_WATCH_PAGES = 80      # "paras hetki" -hakuja per ajo
MAX_SHORT_CHECKS = 150    # varatapa: Shorts-tarkistuksia per ajo
RETRY_DAYS = 5
CLIP_LEN = 40
WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)


def log(*a):
    print(*a, flush=True)


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ verkko
def get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.5", "Cookie": "CONSENT=YES+cb; SOCS=CAI"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        log("  ! verkkovirhe:", url[:90], type(e).__name__)
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


# ------------------------------------------------------------------ välimuisti
def _read_json(text):
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_previous():
    """Yhdistää julkaistun feedin ja repoon ladatun site/feed.jsonin."""
    sources = []
    if SITE_URL:
        s, body = get(SITE_URL + "/feed.json")
        if s == 200:
            sources.append(_read_json(body))
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            sources.append(_read_json(f.read()))
    m = {"videos": {}, "channels": [], "items": {}, "discovered": {}}
    for src in sources:
        for vid, v in (src.get("videos") or {}).items():
            cur = m["videos"].get(vid)
            if cur is None or (v.get("cues") and not cur.get("cues")):
                m["videos"][vid] = v
        for it in src.get("items") or []:
            if isinstance(it, dict) and it.get("id"):
                m["items"].setdefault(it["id"], it)
        m["channels"] += src.get("channels") or []
        m["discovered"].update(src.get("discovered") or {})
    log(f"Välimuisti: {len(m['items'])} korttia, {len(m['videos'])} videota, {len(m['discovered'])} löydettyä kanavaa.")
    return m


# ------------------------------------------------------------------ kanavat ja RSS
def resolve_channel(entry, known):
    if entry.get("channel_id"):
        return entry["channel_id"]
    handle = (entry.get("handle") or "").strip()
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
        mm = re.search(pat, html)
        if mm:
            return mm.group(1)
    return None


def parse_rss(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        views = 0
        st = e.find("media:group/media:community/media:statistics", NS)
        if st is not None:
            try:
                views = int(st.get("views", "0"))
            except ValueError:
                pass
        author = e.findtext("a:author/a:name", default="", namespaces=NS)
        out.append({"vid": vid, "title": e.findtext("a:title", default="", namespaces=NS),
                    "published": (e.findtext("a:published", default="", namespaces=NS) or today())[:10],
                    "views": views, "author": author})
    return out


def fetch_feed(kind, ident):
    """kind: 'channel_id' tai 'playlist_id'. Palauttaa listan tai None (ei saatavilla)."""
    s, xml = get(f"https://www.youtube.com/feeds/videos.xml?{kind}={ident}")
    if s != 200 or not xml:
        return None
    return parse_rss(xml)


def channel_videos(cid):
    """Palauttaa (shortsit, pitkät). Käyttää kanavan erillisiä soittolistoja."""
    base = cid[2:]
    shorts = fetch_feed("playlist_id", "UUSH" + base)
    longs = fetch_feed("playlist_id", "UULF" + base)
    if shorts is not None or longs is not None:
        return shorts or [], longs or []
    # varatapa: kanavan yleissyöte + Shorts-tarkistus
    allv = fetch_feed("channel_id", cid) or []
    s_out, l_out = [], []
    for v in allv:
        st = status_no_redirect(f"https://www.youtube.com/shorts/{v['vid']}")
        (s_out if st == 200 else l_out).append(v)
        time.sleep(0.1)
    return s_out, l_out


# ------------------------------------------------------------------ haku
SV_WORDS = set("""och att det är jag inte ett på med för som av till om har du vi vad hur när den de man så kan ska min mitt din
ditt bästa sverige svensk svenska svenskt svenskar idag efter utan mer alla eller också nu här bara mycket aldrig alltid hej tack
igen får sin sina vår våra mot över under hela varför vem vilken dags första gången säger blir gör träning recept mat enkel snabb
rolig roligaste klipp avsnitt scen tips hemma världens""".split())
OTHER_NORDIC = set("og ikke jeg er hvordan hva hvad hvorfor kun også meget mye bare noen nogen".split())


def looks_swedish(title):
    t = title.lower()
    words = WORD.findall(t)
    sv = sum(1 for w in words if w in SV_WORDS)
    other = sum(1 for w in words if w in OTHER_NORDIC)
    return sv - other >= 1 or "sverige" in t or "svensk" in t


def _text(node):
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    if "runs" in node:
        return "".join(r.get("text", "") for r in node["runs"])
    if "content" in node:
        return node["content"]
    return ""


def _secs(txt):
    parts = [int(p) for p in re.findall(r"\d+", txt or "")]
    if not parts:
        return None
    s = 0
    for p in parts:
        s = s * 60 + p
    return s


def parse_search(html):
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    out, seen = [], set()

    def walk(n):
        if isinstance(n, dict):
            if "videoRenderer" in n:
                r = n["videoRenderer"]
                owner = (r.get("ownerText") or {}).get("runs") or [{}]
                out.append({"vid": r.get("videoId"), "title": _text(r.get("title")),
                            "channel": owner[0].get("text", ""),
                            "cid": (((owner[0].get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId")),
                            "length": _secs(_text(r.get("lengthText")))})
            elif "reelItemRenderer" in n:
                r = n["reelItemRenderer"]
                out.append({"vid": r.get("videoId"), "title": _text(r.get("headline")), "channel": "", "cid": None, "length": 59})
            elif "shortsLockupViewModel" in n:
                r = n["shortsLockupViewModel"]
                vid = ((((r.get("onTap") or {}).get("innertubeCommand") or {}).get("reelWatchEndpoint") or {}).get("videoId"))
                out.append({"vid": vid, "title": _text((r.get("overlayMetadata") or {}).get("primaryText")),
                            "channel": "", "cid": None, "length": 59})
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(data)
    res = []
    for r in out:
        if r["vid"] and r["vid"] not in seen:
            seen.add(r["vid"])
            res.append(r)
    return res


def search(q):
    # sp=EgIYAQ== : alle 4 minuutin videot
    s, html = get(f"https://www.youtube.com/results?search_query={quote(q)}&sp=EgIYAQ%3D%3D&hl=sv&gl=SE")
    return parse_search(html) if s == 200 else []


# ------------------------------------------------------------------ paras hetki
def best_moment(vid):
    """Palauttaa (alku, loppu) eniten uudelleen katsotusta kohdasta tai None."""
    s, html = get(f"https://www.youtube.com/watch?v={vid}&hl=sv")
    if s != 200 or not html:
        return None
    mm = re.search(r'"lengthSeconds":"(\d+)"', html)
    length = int(mm.group(1)) if mm else None
    markers = []
    for blob in re.findall(r'\{[^{}]*"intensityScoreNormalized"[^{}]*\}', html):
        try:
            d = json.loads(blob)
            markers.append((int(d["startMillis"]) / 1000, int(d["durationMillis"]) / 1000, float(d["intensityScoreNormalized"])))
        except Exception:
            continue
    if length and length < 50:
        return (0.0, float(length))
    if len(markers) >= 10:
        markers.sort()
        total = markers[-1][0] + markers[-1][1]
        best, best_score = None, -1
        for i, (st, du, _) in enumerate(markers):
            if st < total * 0.03:          # ohita alku (intro)
                continue
            score, span, j = 0.0, 0.0, i
            while j < len(markers) and span < CLIP_LEN:
                score += markers[j][2] * markers[j][1]
                span += markers[j][1]
                j += 1
            if span >= CLIP_LEN * 0.6 and score > best_score:
                best, best_score = st, score
        if best is not None:
            end = best + CLIP_LEN if not length else min(best + CLIP_LEN, length)
            return (round(best, 1), round(end, 1))
    if length and length > 90:
        a = length * 0.2
        return (round(a, 1), round(min(a + CLIP_LEN, length), 1))
    return None


# ------------------------------------------------------------------ tekstitykset
TRANSCRIPT_BLOCKED = False


def get_transcript(vid, lang):
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
            raw = [(sn.text, float(sn.start), float(sn.duration)) for sn in api.fetch(vid, languages=[lang])]
        else:
            raw = [(d["text"], float(d["start"]), float(d["duration"])) for d in YouTubeTranscriptApi.get_transcript(vid, languages=[lang])]
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
    if len(clips) > max_clips:
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
    disc_cfg = {"enabled": True, "min_hits": 2, "max_channels": 120, **(cfg.get("discovery") or {})}
    prev = load_previous()
    lib = dict(prev["items"])              # id -> kortti
    videos = dict(prev["videos"])          # vid -> välimuisti
    discovered = dict(prev["discovered"])  # cid -> {name, category, hits}
    known_ids = {c["handle"]: c["id"] for c in prev["channels"] if c.get("handle") and c.get("id")}
    ranks = load_freq()
    stats = {"new": 0, "search": 0, "disc_new": 0}
    long_queue = []                        # (vid, meta) pitkistä videoista

    def add(item):
        old = lib.get(item["id"])
        if old:
            old.update({k: v for k, v in item.items() if k in ("title", "views")})
        else:
            item.setdefault("found", today())
            lib[item["id"]] = item
            stats["new"] += 1

    def add_short(v, channel, cat, src):
        videos.setdefault(v["vid"], {})["short"] = True
        add({"id": v["vid"], "vid": v["vid"], "kind": "short", "start": 0, "end": None, "title": v["title"],
             "channel": channel, "cat": cat, "published": v.get("published") or today(), "views": v.get("views", 0), "src": src})

    # ---- A) kanavalista
    channels, curated_ids = [], set()
    for entry in cfg.get("channels", []):
        name, cat = entry.get("name") or entry.get("handle"), entry.get("category", "Övrigt")
        cid = resolve_channel(entry, known_ids)
        if not cid:
            log(f"✗ {name}: kanavaa ei löytynyt ({entry.get('handle')}).")
            channels.append({"handle": entry.get("handle"), "name": name, "category": cat, "id": None, "ok": False})
            continue
        curated_ids.add(cid)
        shorts, longs = channel_videos(cid)
        log(f"✓ {name}: {len(shorts)} shortsia, {len(longs)} pitkää")
        channels.append({"handle": entry.get("handle"), "name": name, "category": cat, "id": cid, "ok": bool(shorts or longs)})
        for v in shorts:
            add_short(v, name, cat, "ch")
        for v in longs:
            long_queue.append((v, name, cat, "ch"))

    # ---- B) hakusanat + C) kanavalöytö
    hits = {}
    for s_entry in cfg.get("searches", []):
        q, cat = s_entry.get("q"), s_entry.get("category", "Övrigt")
        if not q:
            continue
        results = search(q)
        ok = 0
        for r in results:
            if not looks_swedish(r["title"]):
                continue
            if r.get("length") and r["length"] > 240:
                continue
            ok += 1
            label = r["channel"] or "#" + q.split()[0]
            add_short({"vid": r["vid"], "title": r["title"], "published": today(), "views": 0}, label, cat, "search")
            stats["search"] += 1
            if r.get("cid") and r["cid"] not in curated_ids:
                h = hits.setdefault(r["cid"], {"name": r["channel"], "cats": {}, "n": 0})
                h["n"] += 1
                h["cats"][cat] = h["cats"].get(cat, 0) + 1
        log(f"🔎 \"{q}\": {len(results)} tulosta, {ok} ruotsinkielistä")
        time.sleep(0.8 + random.random() * 0.6)

    if disc_cfg["enabled"]:
        for cid, h in hits.items():
            if cid in discovered:
                discovered[cid]["hits"] = discovered[cid].get("hits", 0) + h["n"]
            elif h["n"] >= disc_cfg["min_hits"] and len(discovered) < disc_cfg["max_channels"]:
                discovered[cid] = {"name": h["name"], "category": max(h["cats"], key=h["cats"].get), "hits": h["n"]}
                stats["disc_new"] += 1
                log(f"✨ Uusi kanava löydetty: {h['name']} ({discovered[cid]['category']})")
        for cid, d in discovered.items():
            if cid in curated_ids:
                continue
            shorts = fetch_feed("playlist_id", "UUSH" + cid[2:]) or []
            for v in shorts:
                add_short(v, d["name"], d["category"], "disc")
            time.sleep(0.1)

    # ---- D) pitkät videot -> klipit
    watch_budget, tr_budget = MAX_WATCH_PAGES, MAX_TRANSCRIPTS
    for v, name, cat, src in long_queue:
        vid = v["vid"]
        rec = videos.setdefault(vid, {})
        rec["short"] = False
        stale = time.time() - rec.get("tried", 0) > RETRY_DAYS * 86400
        if not rec.get("cues") and stale and tr_budget > 0 and not TRANSCRIPT_BLOCKED:
            tr_budget -= 1
            rec["tried"] = int(time.time())
            cues = get_transcript(vid, lang)
            if cues:
                rec["cues"] = cues
            time.sleep(0.3)
        base = {"vid": vid, "kind": "clip", "title": v["title"], "channel": name, "cat": cat,
                "published": v.get("published") or today(), "views": v.get("views", 0), "src": src}
        if rec.get("cues"):
            for a, b in make_clips(rec["cues"]):
                add({**base, "id": f"{vid}@{int(a)}", "start": a, "end": b})
            continue
        if "best" not in rec and watch_budget > 0:
            watch_budget -= 1
            rec["best"] = best_moment(vid)
            time.sleep(0.4 + random.random() * 0.4)
        if rec.get("best"):
            a, b = rec["best"]
            add({**base, "id": f"{vid}@{int(a)}", "start": a, "end": b, "best": True})

    # ---- E) tekstitykset uusille shortseille
    for it in sorted(lib.values(), key=lambda x: x.get("found", ""), reverse=True):
        if tr_budget <= 0 or TRANSCRIPT_BLOCKED:
            break
        if it["kind"] != "short":
            continue
        rec = videos.setdefault(it["vid"], {})
        if rec.get("cues") or time.time() - rec.get("tried", 0) < RETRY_DAYS * 86400:
            continue
        tr_budget -= 1
        rec["tried"] = int(time.time())
        cues = get_transcript(it["vid"], lang)
        if cues:
            rec["cues"] = cues
        time.sleep(0.3)

    # ---- F) tasot, merkinnät ja karsinta
    cutoff = datetime.now(timezone.utc).timestamp() - KEEP_DAYS * 86400
    items = []
    for it in lib.values():
        try:
            ts = datetime.fromisoformat(it.get("published", today())).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            ts = time.time()
        if ts < cutoff:
            continue
        rec = videos.get(it["vid"], {})
        if rec.get("cues") and rec.get("level") is None:
            rec["level"] = level_of(rec["cues"], ranks)
        it["subs"] = bool(rec.get("cues"))
        it["level"] = rec.get("level")
        it.pop("thumb", None)
        it.pop("desc", None)
        items.append(it)
    items.sort(key=lambda x: (x.get("found", ""), x.get("published", "")), reverse=True)
    items = items[:MAX_ITEMS]
    used = {it["vid"] for it in items}
    videos = {k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in videos.items() if k in used}

    feed = {
        "version": 2,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "language": lang,
        "transcripts_blocked": TRANSCRIPT_BLOCKED,
        "channels": channels,
        "discovered": discovered,
        "items": items,
        "videos": videos,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, separators=(",", ":"))

    cats = {}
    for it in items:
        cats[it["cat"]] = cats.get(it["cat"], 0) + 1
    log(f"\nValmis: {len(items)} korttia kirjastossa ({stats['new']} uutta, {stats['search']} hausta), "
        f"{sum(1 for i in items if i['subs'])} napautettavalla tekstityksellä.")
    log(f"Kanavia: {sum(1 for c in channels if c['ok'])}/{len(channels)} listalta + {len(discovered)} löydettyä "
        f"({stats['disc_new']} uutta tällä ajolla).")
    log("Kategoriat: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])))
    if TRANSCRIPT_BLOCKED:
        log("Huom: YouTube esti tekstitysten haun GitHubin palvelimilta.")


if __name__ == "__main__":
    main()
