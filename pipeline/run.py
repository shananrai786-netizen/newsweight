#!/usr/bin/env python3
"""Newsweight pipeline: fetch RSS -> group duplicates -> score new stories with Gemini -> write stories.json.

Free-tier friendly: scores only NEW stories, in small batches, and stops cleanly on rate limits.
Env: GEMINI_API_KEY (required unless MOCK=1), GEMINI_MODEL, MAX_NEW (default 12), OUT (default ../stories.json)
"""
import os, sys, re, json, hashlib, time, html, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("OUT", os.path.join(HERE, "..", "stories.json"))
FEEDS = os.environ.get("FEEDS", os.path.join(HERE, "feeds.json"))
MODEL = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash-lite"
MAX_NEW = int(os.environ.get("MAX_NEW", "12"))
BATCH = int(os.environ.get("BATCH", "6"))
KEEP_DAYS = 35
MAX_STORIES = 1500
FRESH_HOURS = 36
MATCH = 0.5  # headline similarity needed to call two items "the same story"

TOPICS = {"fin": "Finance & Markets", "bank": "Banking & Insurance", "eco": "Economy & Policy",
          "bus": "Business & Industry", "start": "Startups & Funding", "fmcg": "Retail & FMCG",
          "mkt": "Marketing & Media", "auto": "Auto & Mobility", "real": "Real Estate",
          "energy": "Energy & Climate", "agri": "Agriculture", "jobs": "Jobs & Workplace",
          "edu": "Education", "health": "Health", "pol": "Politics", "law": "Law & Courts",
          "world": "World Affairs", "tech": "Technology", "sci": "Science",
          "ent": "Film & Entertainment", "arts": "Arts & Culture", "lit": "Literature", "spo": "Sports"}
SCENES = ["arch", "ballot", "bank", "battery", "billboard", "book", "bulb", "candles", "cap", "car", "cart",
          "chip", "coins", "cricket", "dome", "factory", "film", "globe", "gold", "health", "house", "leaf",
          "oil", "palette", "people", "phone", "plane", "rocket", "scales", "shelf", "skyline", "truck", "wheat"]
LENSES = {"fin": "finance", "mkt": "marketing", "hr": "HR", "ops": "operations", "ana": "analytics and IT",
          "gm": "general management"}
STOP = set("the a an of to in on for and or at by with as is are was were be been from that this it its after over "
           "amid says say said new will may could would up down out into than more less has have had not no".split())


def log(*a):
    print(*a, file=sys.stderr)


# ---------- fetch ----------
def clean(t, n=400):
    t = re.sub(r"<[^>]+>", " ", html.unescape(t or ""))
    return re.sub(r"\s+", " ", t).strip()[:n]


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def fetch_feed(f):
    req = urllib.request.Request(f["url"], headers={"User-Agent": "NewsweightBot/0.1 (headline reader)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def g(*names):
            for n in names:
                for c in it:
                    if c.tag.split("}")[-1] == n:
                        return c
            return None
        title = g("title")
        if title is None or not (title.text or "").strip():
            continue
        desc = g("description", "summary")
        date = g("pubDate", "published", "updated")
        d = parse_date(date.text if date is not None else None) or datetime.now(timezone.utc)
        items.append({"paper": f["paper"], "region": f.get("region", "IN"), "title": clean(title.text, 220),
                      "desc": clean(desc.text if desc is not None else "", 400), "pub": d})
    return items


# ---------- group duplicates ----------
def toks(t):
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOP and len(w) > 2}


def sim(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster(items, existing):
    """Attach items to existing stories or to new clusters. Returns (new_clusters, existing_updates)."""
    new, upd = [], {}
    ex = [(s, set(s.get("k", []))) for s in existing]
    for it in sorted(items, key=lambda x: x["pub"]):
        k = toks(it["title"])
        hit = max(ex, key=lambda e: sim(k, e[1]), default=None)
        if hit and sim(k, hit[1]) >= MATCH:
            upd.setdefault(hit[0]["id"], set()).add(it["paper"])
            continue
        c = max(new, key=lambda n: sim(k, n["k"]), default=None)
        if c and sim(k, c["k"]) >= MATCH:
            c["items"].append(it)
            c["papers"].add(it["paper"])
            c["k"] |= k
        else:
            new.append({"k": k, "items": [it], "papers": {it["paper"]}})
    return new, upd


# ---------- scoring ----------
RUBRIC = """You score news for Newsweight, an India-first site that rates every story 1-10 on five dimensions:
breadth (how many people/sectors it touches), magnitude (how large the change is), surprise (how far from expectations),
durability (how long the effect lasts), actionability (whether people must respond). Be calibrated: an unscheduled central-bank
rate move is ~9-10 on all; a routine company result is 3-5; most stories are 2-6. Use the full range and do not inflate.

For each numbered story return one JSON object:
 i: the story number
 topic: one id from %s
 region: "IN" if mainly about India or Indians, otherwise "GL"
 d: [breadth, magnitude, surprise, durability, actionability], integers 1-10
 h: a neutral headline in YOUR OWN words, at most 14 words (never copy the source headline)
 sum: 2-3 sentences in YOUR OWN words, using ONLY facts present in the text provided. Never invent numbers, names or causes.
 mean: one sentence on what it means for an ordinary reader
 why: one short sentence justifying the score
 lens: object with 1-3 keys from %s mapping to one sentence on what it means for that field; omit fields it does not touch
 tg: array of field ids from %s that it is relevant to
 img: the best-fitting id from %s
 thin: true if the text given was too short to support a reliable summary, else false
Return a JSON array only, no commentary."""


def prompt(batch):
    head = RUBRIC % (list(TOPICS), json.dumps(LENSES), list(LENSES), SCENES)
    body = "\n\n".join("Story %d (seen in %d papers: %s)\nSource headline: %s\nSource text: %s" % (
        i, len(c["papers"]), ", ".join(sorted(c["papers"])), c["items"][0]["title"],
        c["items"][0]["desc"] or "(no text provided)") for i, c in enumerate(batch))
    return head + "\n\n" + body


def gemini(text):
    key = os.environ["GEMINI_API_KEY"]
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % MODEL
    body = json.dumps({"contents": [{"parts": [{"text": text}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=90) as r:
        j = json.load(r)
    return json.loads(j["candidates"][0]["content"]["parts"][0]["text"])


def mock(batch):
    out = []
    for i, c in enumerate(batch):
        h = c["items"][0]["title"]
        out.append({"i": i, "topic": "bus", "region": c["items"][0]["region"], "d": [5, 4, 4, 3, 4],
                    "h": "Mock: " + " ".join(h.split()[:8]), "sum": "Mock summary for testing.", "mean": "Mock meaning.",
                    "why": "Mock score.", "lens": {"fin": "Mock finance note."}, "tg": ["fin"], "img": "skyline", "thin": False})
    return out


def valid(o):
    try:
        return (o["topic"] in TOPICS and o["region"] in ("IN", "GL") and len(o["d"]) == 5
                and all(isinstance(x, (int, float)) and 1 <= x <= 10 for x in o["d"])
                and 3 <= len(o["h"]) <= 140 and 20 <= len(o["sum"]) <= 900 and o["mean"] and o["why"])
    except Exception:
        return False


def clamp_scene(o):
    return o.get("img") if o.get("img") in SCENES else "skyline"


def score(clusters):
    done = []
    use_mock = os.environ.get("MOCK") == "1"
    for s in range(0, len(clusters), BATCH):
        batch = clusters[s:s + BATCH]
        try:
            res = mock(batch) if use_mock else gemini(prompt(batch))
        except urllib.error.HTTPError as e:
            log("Gemini HTTP", e.code, "- stopping; remaining stories wait for the next run")
            break
        except Exception as e:
            log("Gemini error:", repr(e), "- stopping")
            break
        for o in res if isinstance(res, list) else []:
            try:
                c = batch[int(o["i"])]
            except Exception:
                continue
            if not valid(o):
                log("rejected invalid output for:", c["items"][0]["title"][:60])
                continue
            if o.get("thin"):
                o["sum"] = o["sum"][:300]
            done.append((c, o))
        if not use_mock:
            time.sleep(5)  # stay under per-minute limits
    return done


def build(c, o):
    first = min(c["items"], key=lambda x: x["pub"])
    sid = hashlib.sha1(" ".join(sorted(c["k"])[:8]).encode()).hexdigest()[:10]
    lens = {k: v for k, v in (o.get("lens") or {}).items() if k in LENSES and isinstance(v, str)}
    tg = [t for t in (o.get("tg") or []) if t in LENSES] or list(lens)[:2]
    return {"id": sid, "r": o["region"], "t": o["topic"], "src": first["paper"], "p": len(c["papers"]),
            "d": [int(round(x)) for x in o["d"]], "h": clean(o["h"], 140), "sum": clean(o["sum"], 900),
            "mean": clean(o["mean"], 300), "why": clean(o["why"], 200), "lens": lens, "tg": tg,
            "img": clamp_scene(o), "pub": first["pub"].isoformat(), "k": sorted(c["k"]), "papers": sorted(c["papers"])}


# ---------- main ----------
def main():
    with open(FEEDS, encoding="utf8") as fh:
        feeds = json.load(fh)["feeds"]
    items, ok = [], 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)
    for f in feeds:
        try:
            got = [i for i in fetch_feed(f) if i["pub"] >= cutoff][:40]
            items += got
            ok += 1
        except Exception as e:
            log("feed failed:", f["paper"], repr(e)[:120])
    if not ok:
        log("no feed could be read; leaving stories.json unchanged")
        return 0

    try:
        with open(OUT, encoding="utf8") as fh:
            data = json.load(fh)
    except Exception:
        data = {"stories": []}
    stories = data.get("stories", [])
    new, upd = cluster(items, stories)

    for s in stories:  # more papers carrying an old story raises its paper count
        if s["id"] in upd:
            s["papers"] = sorted(set(s.get("papers", [])) | upd[s["id"]])
            s["p"] = max(s["p"], len(s["papers"]))

    new.sort(key=lambda c: (-len(c["papers"]), -max(i["pub"] for i in c["items"]).timestamp()))
    scored = score(new[:MAX_NEW])
    have = {s["id"] for s in stories}
    for c, o in scored:
        st = build(c, o)
        if st["id"] not in have:
            stories.append(st)
            have.add(st["id"])

    cut = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    stories = [s for s in stories if parse_date(s["pub"]) >= cut]
    stories.sort(key=lambda s: s["pub"], reverse=True)
    stories = stories[:MAX_STORIES]
    out = {"updated": datetime.now(timezone.utc).isoformat(), "stories": stories}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    log("feeds ok %d/%d, items %d, new clusters %d, scored %d, total %d" % (ok, len(feeds), len(items), len(new), len(scored), len(stories)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
