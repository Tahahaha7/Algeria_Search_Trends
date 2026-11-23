#!/usr/bin/env python3
"""Refresh the Google Trends data behind the Algeria Search Trends page.

    python3 update.py            refresh every section
    python3 update.py cars tv    refresh only the named sections

For each section this looks up a pool of candidates on Google Trends, ranks
them by search interest in Algeria over the past 12 months, and writes the
top five (with a five-year timeline and a breakdown by province), the names
rising fastest, and Google's own "rising searches" to data/trends.json.

The pool starts from the seed names below and grows by itself: when one of
Google's rising searches turns out to be a new name of the right kind, it is
added to data/pool.json and competes from the next run on. Edit the seeds
freely; a seed Google does not recognise is reported and skipped.

"kinds" is matched against the description Google gives each name (for
example "Automobile manufacturer"), so a seed resolves to the right thing;
"discover", where present, is a stricter pattern used for new names.

Google has no open API for this. The script uses the addresses the Trends
website itself calls, at a slow pace, and stops if Google says to slow down.
Run it again later and it carries on with the sections that are missing.
"""
import datetime
import http.cookiejar
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

GEO = "DZ"                 # Algeria
TIMELINE = "today 5-y"     # weekly points, five years
RECENT = "today 12-m"      # window for the province map and rising searches
TOP_N = 5
PAUSE = 1.5                # seconds between requests
MAX_NEW_LOOKUPS = 8        # rising searches checked for new names, per section per run

SECTIONS = {
    "cars": {
        "title": "Automobile Manufacturers",
        "property": "",            # "" is web search, "youtube" is YouTube search
        "category": 815,           # Google's "Vehicle Brands" category, for rising searches
        "kinds": r"\b(car|automobile|automotive|vehicle) (make|manufacturer|company|brand)\b|automaker|carmaker",
        "seeds": ["Renault", "Peugeot", "Fiat", "Citroën", "Dacia", "Volkswagen", "Toyota",
                  "Hyundai", "Kia", "Suzuki", "Nissan", "Chery", "Geely", "Mercedes-Benz",
                  "BMW", "Audi", "Opel", "SEAT", "Škoda", "Ford", "JAC Motors", "MG Motor",
                  "BYD", "DFSK", "Great Wall Motor", "Mitsubishi Motors"],
    },
    "tv": {
        "title": "Algerian TV Channels",
        "property": "youtube",
        "category": 36,            # "TV & Video"
        "kinds": r"\b(tv|television|news|sports)\b.*\b(channel|network|station)\b|\bbroadcaster\b",
        "seeds": ["Ennahar TV", "Echorouk TV", "Echorouk News", "El Bilad TV", "Samira TV",
                  "El Heddaf TV", "El Hayat TV", "El Djazairia One", "Beur TV", "Canal Algérie",
                  "AL24 News", "beIN Sports", "Al Jazeera", "MBC"],
    },
    "sweets": {
        "title": "Baklava vs. Zlabia",
        "property": "",
        "category": 122,           # "Cooking & Recipes"
        "kinds": r"dessert|pastr|sweet|confection|cookie|cake|biscuit|food|dish",
        "discover": r"dessert|pastr|sweet|confection|cookie|cake|biscuit",
        "plain_terms": True,       # sweets without a Google topic are searched as plain words
        "seeds": [["Baklava", "/m/0g07j"], ["Zlabia", "/m/04nyjg"], "Makroud", "Griwech",
                  "Kalb el louz", "Tcharek", "Dziriat", "Mchewek", "Ghribia", "Arayech",
                  "Bniouen", "Mkhabez", "Basbousa", "Kunafa", "Qatayef"],
    },
    "football": {
        "title": "Football Players",
        "property": "youtube",
        "category": 294,           # "Soccer"
        "kinds": r"footballer|\bplayer\b|striker|midfielder|defender|winger|goalkeeper",
        "seeds": ["Riyad Mahrez", "Lionel Messi", "Cristiano Ronaldo", "Kylian Mbappé",
                  "Erling Haaland", "Lamine Yamal", "Vinícius Júnior", "Mohamed Salah", "Neymar",
                  "Jude Bellingham", "Mohamed Amoura", "Ismaël Bennacer", "Rayan Aït-Nouri",
                  "Baghdad Bounedjah", "Amine Gouiri", "Farès Chaïbi", "Youcef Belaïli",
                  "Islam Slimani", "Aïssa Mandi", "Ramy Bensebaini", "Houssem Aouar",
                  "Saïd Benrahma", "Anis Hadj Moussa", "Ibrahim Maza"],
    },
    "bac": {
        "title": "Baccalauréat vs. Équipe nationale",
        "property": "",
        "fixed": True,             # always these two; no ranking, no rising list
        "seeds": [["Baccalauréat", "/m/01tc8y"], ["Équipe nationale", "/m/03_qj1"]],
    },
    "music": {
        "title": "Top Artists",
        "property": "youtube",
        "category": 35,            # "Music & Audio"
        "kinds": r"singer|rapper|musician|\bartist\b|\bband\b|songwriter|composer|\bdj\b|disc jockey|vocalist|musical (group|duo)",
        "seeds": ["Soolking", ["L'Algérino", "/m/04gvf3y"], "Cheb Khaled", "Cheb Mami", "Cheb Bilal",
                  "Cheb Hasni", "Kader Japonais", "Cheb Bello", "Bilal Sghir", "Faycel Sghir",
                  "Didine Canon 16", "Mouh Milano", "Djalil Palermo", "Trap King", "Flenn",
                  "Phobia Isaac", "TIF", "Raja Meziane", "DJ Snake", "Rim'K", "Lounès Matoub",
                  "Idir", "Warda Al-Jazairia", "Zaho"],
    },
}

BASE = "https://trends.google.com/trends"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
TRENDS_FILE = os.path.join(DATA_DIR, "trends.json")
POOL_FILE = os.path.join(DATA_DIR, "pool.json")


class RateLimited(Exception):
    pass


class Refused(Exception):
    pass


class Trends:
    """The handful of requests the Trends website makes, one at a time."""

    def __init__(self):
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.calls = 0
        self._get(BASE + "/?geo=" + GEO)   # a plain visit first, as a browser would make

    def _get(self, url):
        time.sleep(PAUSE)
        self.calls += 1
        try:
            with self.opener.open(url, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:   # no network, timeout
            return 0, repr(e)

    def _json(self, path, params):
        status, body = self._get(BASE + "/api/" + path + "?" + urllib.parse.urlencode(params))
        if status == 429:
            raise RateLimited(path)
        if status != 200 or "{" not in body:
            raise Refused("%s -> %s %s" % (path, status, " ".join(body[:140].split())))
        return json.loads(body[body.index("{"):])   # the body starts with a )]}' guard

    def explore(self, keywords, window, prop, category=0):
        items = [{"keyword": k, "geo": GEO, "time": window} for k in keywords]
        req = {"comparisonItem": items or [{"geo": GEO, "time": window}],
               "category": category, "property": prop}
        return self._json("explore", {"hl": "en-US", "tz": "-60", "req": json.dumps(req)})

    def _widget(self, explore, widget_id, path):
        for w in explore.get("widgets") or []:
            if w.get("id") == widget_id:
                return self._json("widgetdata/" + path, {
                    "hl": "en-US", "tz": "-60",
                    "req": json.dumps(w["request"]), "token": w["token"]})
        raise Refused("Google sent no %s for this request" % widget_id)

    def timeline(self, keywords, prop):
        """Weekly interest for up to five keywords, scaled together 0-100."""
        explore = self.explore(keywords, TIMELINE, prop)
        points = self._widget(explore, "TIMESERIES", "multiline")["default"]["timelineData"]
        now = time.time()
        points = [p for p in points
                  if not p.get("isPartial") and int(p["time"]) + 7 * 86400 <= now]
        times = [int(p["time"]) for p in points]
        series = [[p["value"][i] for p in points] for i in range(len(keywords))]
        return times, series, keyword_names(explore, len(keywords))

    def provinces(self, keywords, prop):
        explore = self.explore(keywords, RECENT, prop)
        rows = self._widget(explore, "GEO_MAP", "comparedgeo")["default"]["geoMapData"]
        return [{"code": r["geoCode"],
                 "name": re.sub(r"\s+Province$", "", r.get("geoName", "")),
                 "values": r["value"]} for r in rows]

    def category_searches(self, category, prop):
        """Google's top and rising searches inside one of its categories."""
        explore = self.explore([], RECENT, prop, category)
        lists = self._widget(explore, "RELATED_QUERIES", "relatedsearches")["default"]["rankedList"]
        lists = [[{"query": k.get("query", ""), "change": k.get("formattedValue", "")}
                  for k in l.get("rankedKeyword") or []] for l in lists]
        return (lists + [[], []])[:2]

    def suggest(self, text):
        """Google's topic suggestions for a name: [{mid, title, type}, ...]."""
        path = "autocomplete/" + urllib.parse.quote(text, safe="")
        return self._json(path, {"hl": "en-US", "tz": "-60"})["default"]["topics"]


def keyword_names(explore, n):
    """The display names Google attaches to the keywords of a request, where it sends them."""
    names = [None] * n
    for i, k in enumerate(explore.get("keywords") or []):
        if i < n and isinstance(k, dict):
            names[i] = k.get("name")
    for w in explore.get("widgets") or []:
        m = re.search(r"_(\d+)$", w.get("id", ""))
        i = int(m.group(1)) if m else 0
        if i < n and not names[i] and w.get("keywordName"):
            names[i] = w["keywordName"]
    return [x if x and not x.startswith(("/m/", "/g/")) else None for x in names]


def plain(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def mean(values):
    return sum(values) / len(values) if values else 0.0


def pick_topic(topics, kinds, wanted=None):
    """The suggestion of the right kind; an exact name match wins over the first one."""
    fits = [t for t in topics if re.search(kinds, t.get("type", ""), re.I)]
    if wanted:
        exact = [t for t in fits if plain(t.get("title", "")) == plain(wanted)]
        if exact:
            return exact[0]
    return fits[0] if fits else None


def build_pool(trends, cfg, saved, log):
    """Seeds resolved to Google topics, plus names discovered on earlier runs."""
    pool, known = [], {e["seed"]: e for e in saved if e.get("seed")}
    for seed in cfg["seeds"]:
        if isinstance(seed, (list, tuple)):
            pool.append({"seed": seed[0], "name": seed[0], "keyword": seed[1], "kind": "pinned"})
            continue
        old = known.get(seed)
        if old and (old["keyword"] or not older_than(old.get("checked"), 30)):
            pool.append(old)       # resolved before, or looked up recently without luck
            continue
        topics = trends.suggest(seed)
        topic = pick_topic(topics, cfg["kinds"], seed)
        if topic:
            entry = {"seed": seed, "name": topic["title"], "keyword": topic["mid"], "kind": topic["type"]}
        elif cfg.get("plain_terms"):
            entry = {"seed": seed, "name": seed, "keyword": seed, "kind": "search term"}
        else:
            entry = {"seed": seed, "name": seed, "keyword": None, "kind": "not found", "checked": today()}
        log("    %-20s -> %s [%s]" % (seed, entry["name"], entry["kind"]))
        if not topic:
            for t in topics[:3]:
                log("    %-20s    Google offered: %s [%s]" % ("", t.get("title"), t.get("type")))
        pool.append(entry)
    pool += [e for e in saved if e.get("discovered")]
    seen, unique = set(), []
    for e in pool:
        if e["keyword"] is None or e["keyword"] not in seen:
            seen.add(e["keyword"])
            unique.append(e)
    return unique


GENERIC = {"sports", "sport", "news", "motor", "motors", "algerie", "algeria", "junior", "canon"}


def mentions(query, pool):
    """Whether a search is about one of the pool's names (full name, seed, or surname)."""
    q = " %s " % plain(query)
    for e in pool:
        phrases = {plain(e["name"]), plain(e.get("seed") or e["name"])}
        last = plain(e["name"]).split()[-1:]
        if last and len(last[0]) >= 5 and last[0] not in GENERIC:
            phrases.add(last[0])
        if any(p and " %s " % p in q for p in phrases):
            return True
    return False


def keys(entries):
    return [e["keyword"] for e in entries]


def together(trends, entries, prop):
    """Each entry's 52-week mean when measured in the same request as the biggest of them."""
    champion, rest, seen = entries[0], entries[1:], {}
    while rest:                                   # heats of five, carrying the biggest so far
        heat, rest = [champion] + rest[:4], rest[4:]
        _, series, _ = trends.timeline(keys(heat), prop)
        seen = dict(zip(keys(heat), [mean(s[-52:]) for s in series]))
        champion = max(heat, key=lambda e: seen[e["keyword"]])
    if len(entries) <= 5:
        return seen                               # one heat held them all
    out, others = {}, [e for e in entries if e is not champion]
    for i in range(0, len(others), 4):            # everyone again, this time next to the biggest
        heat = [champion] + others[i:i + 4]
        _, series, _ = trends.timeline(keys(heat), prop)
        m = [mean(s[-52:]) for s in series]
        out.setdefault(champion["keyword"], m[0])
        for e, v in zip(heat[1:], m[1:]):
            out[e["keyword"]] = v * out[champion["keyword"]] / m[0] if m[0] else 0.0
    return out


def measure(trends, entries, prop):
    """One round: every entry's 52-week mean on a common scale. 0 = did not register.

    Google scales each request (at most five keywords) to its own maximum, in
    whole numbers from 0 to 100. So entries are measured in groups of five, and
    the group winners are then measured together to put the groups on one scale.
    """
    groups = [entries[i:i + 5] for i in range(0, len(entries), 5)]
    local = {}
    for group in groups:
        _, series, names = trends.timeline(keys(group), prop)
        for e, s, name in zip(group, series, names):
            if name and e["kind"] == "pinned":
                e["name"] = name
            last, before = mean(s[-13:]), mean(s[-26:-13])
            if before >= 2:                       # enough signal to call a trend
                e["growth"] = round((last - before) / before, 2)
            local[e["keyword"]] = mean(s[-52:])
    winners = [max(g, key=lambda e: local[e["keyword"]]) for g in groups]
    live = [w for w in winners if local[w["keyword"]] > 0]
    if not live:
        return {k: 0.0 for k in keys(entries)}
    common = together(trends, live, prop) if len(live) > 1 else {live[0]["keyword"]: local[live[0]["keyword"]]}
    values = {}
    for group, winner in zip(groups, winners):
        own = local[winner["keyword"]]
        factor = common.get(winner["keyword"], 0.0) / own if own else 0.0
        for e in group:
            values[e["keyword"]] = local[e["keyword"]] * factor
    return values


def rank(trends, pool, prop):
    """The pool ordered by search interest over the past 12 months.

    A name far smaller than the biggest rounds down to zero next to it. Names
    that do not register are measured again among themselves, until there are
    enough ranked names to fill the top list.
    """
    ranked, remaining = [], list(pool)
    while remaining and len(ranked) < TOP_N:
        values = measure(trends, remaining, prop)
        hits = sorted((e for e in remaining if values[e["keyword"]] > 0),
                      key=lambda e: values[e["keyword"]], reverse=True)
        if not hits:
            break
        ranked += hits
        remaining = [e for e in remaining if values[e["keyword"]] == 0]
    return ranked + remaining


def refresh(trends, cfg, saved, log):
    prop = cfg["property"]
    checked = dict(saved.get("checked") or {})
    pool = build_pool(trends, cfg, saved.get("entries") or [], log)
    unresolved = [e for e in pool if e["keyword"] is None]
    skipped = [e["seed"] for e in unresolved]
    pool = [e for e in pool if e["keyword"] is not None]
    if not pool:
        raise Refused("none of the seed names were recognised")

    ranked = pool if cfg.get("fixed") else rank(trends, pool, prop)
    top = ranked[:TOP_N]

    # One request with the top names together, so the chart is on a single scale.
    times, series, names = trends.timeline([e["keyword"] for e in top], prop)
    for e, name in zip(top, names):
        if name and e["kind"] == "pinned":
            e["name"] = name
    order = list(range(len(top)))
    if not cfg.get("fixed"):
        order.sort(key=lambda i: mean(series[i][-52:]), reverse=True)
    top, series = [top[i] for i in order], [series[i] for i in order]
    best = max([mean(s[-52:]) for s in series] + [1e-9])

    section = {
        "title": cfg["title"],
        "source": "YouTube search" if prop == "youtube" else "Web search",
        "top": [{"name": e["name"], "kind": e["kind"],
                 "share": round(100 * mean(s[-52:]) / best)} for e, s in zip(top, series)],
        "timeline": {"weeks": times, "series": series},
        "skipped": skipped,
    }
    if cfg.get("fixed"):
        return section, {"entries": pool + unresolved, "checked": checked}

    section["provinces"] = trends.provinces([e["keyword"] for e in top], prop)
    for row in section["provinces"]:
        row["values"] = [row["values"][i] for i in order]

    climbers = sorted((e for e in ranked if e.get("growth") and e["growth"] >= 0.25),
                      key=lambda e: e["growth"], reverse=True)
    section["rising"] = [{"name": e["name"], "growth": e["growth"]} for e in climbers[:TOP_N]]

    # Google's own rising searches in this category. Keep the ones about a name
    # in the pool; look a few short ones up, and if Google's first reading of a
    # search is a new name of the right kind, list it and add it to the pool.
    section["breakout"] = []
    try:
        _, rising = trends.category_searches(cfg["category"], prop)
    except Refused as e:
        log("    rising searches unavailable: %s" % e)
        rising = []
    discover = cfg.get("discover") or cfg["kinds"]
    lookups = 0
    for item in rising:
        query = item["query"]
        if mentions(query, pool):
            section["breakout"].append(item)
            continue
        if query not in checked:
            if len(query.split()) > 3 or lookups >= MAX_NEW_LOOKUPS:
                continue
            lookups += 1
            first = (trends.suggest(query) or [None])[0]
            fits = first and re.search(discover, first.get("type", ""), re.I)
            checked[query] = {"name": first["title"], "keyword": first["mid"], "kind": first["type"]} if fits else None
        found = checked[query]
        if found:
            section["breakout"].append(dict(item, name=found["name"]))
            if found["keyword"] not in {e["keyword"] for e in pool}:
                pool.append(dict(found, discovered=today()))
                log("    new name found: %s [%s]" % (found["name"], found["kind"]))
    section["breakout"] = section["breakout"][:TOP_N]
    return section, {"entries": pool + unresolved, "checked": checked}


def older_than(date, days):
    if not date:
        return True
    return (datetime.date.today() - datetime.date.fromisoformat(date)).days > days


def today():
    return datetime.date.today().isoformat()


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
        f.write("\n")


def report(section, log):
    log("  TOP %d, past 12 months (%s)" % (len(section["top"]), section["source"]))
    for i, t in enumerate(section["top"], 1):
        log("    %d. %-26s %3d   [%s]" % (i, t["name"][:26], t["share"], t["kind"]))
    if section.get("rising"):
        log("  RISING, last 3 months against the 3 before")
        for r in section["rising"]:
            log("    %-29s %+d%%" % (r["name"][:29], round(100 * r["growth"])))
    if section.get("breakout"):
        log("  GOOGLE'S RISING SEARCHES")
        for b in section["breakout"]:
            label = b["query"] + (" (%s)" % b["name"] if b.get("name") else "")
            log("    %-40s %s" % (label[:40], b["change"]))
    if section["skipped"]:
        log("  not recognised by Google, skipped: " + ", ".join(section["skipped"]))
    log("  chart: %d weeks; map: %d provinces" % (
        len(section["timeline"]["weeks"]), len(section.get("provinces") or [])))


def main(argv):
    wanted = argv or list(SECTIONS)
    unknown = [k for k in wanted if k not in SECTIONS]
    if unknown:
        sys.exit("unknown section: %s (choose from %s)" % (", ".join(unknown), ", ".join(SECTIONS)))

    def log(line):
        print(line, flush=True)

    data = load(TRENDS_FILE, {"sections": {}})
    pools = load(POOL_FILE, {})
    trends = Trends()
    done, failed = [], []
    try:
        for key in wanted:
            cfg = SECTIONS[key]
            log("\n== %s" % cfg["title"])
            try:
                section, pool = refresh(trends, cfg, pools.get(key) or {}, log)
            except Refused as e:
                log("  FAILED: %s" % e)
                failed.append(key)
                continue
            section["updated"] = today()
            data["sections"][key] = section
            pool["entries"] = [{k: v for k, v in e.items() if k != "growth"}
                               for e in pool["entries"]]
            pools[key] = pool
            data["updated"] = today()
            save(TRENDS_FILE, data)
            save(POOL_FILE, pools)
            report(section, log)
            done.append(key)
    except RateLimited:
        log("\nGoogle asked to slow down (429). Finished sections are saved; run again in an hour or so.")
    log("\n%d requests. Updated: %s. Failed: %s. Not reached: %s." % (
        trends.calls, ", ".join(done) or "none", ", ".join(failed) or "none",
        ", ".join(k for k in wanted if k not in done and k not in failed) or "none"))


if __name__ == "__main__":
    main(sys.argv[1:])
