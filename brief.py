#!/usr/bin/env python3
"""
AI Morning Brief — zero-cost edition.

Collects AI news from RSS/JSON sources, dedupes, ranks, summarises with a free
LLM tier (with fallbacks), renders an HTML email + static archive page, and
sends via Gmail SMTP.

Usage:
    python brief.py generate            # build today's edition
    python brief.py send                # email the built edition
    python brief.py send --wait-until 07:00   # sleep until 07:00 IST, then send
    python brief.py generate --demo     # no network: build from sample data
    python brief.py preview             # open the last built edition locally
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import yaml

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "brief.db"
DOCS = ROOT / "docs"
UA = "AIMorningBrief/1.0 (personal daily digest; +https://github.com/)"
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "40"))
LLM_BATCH = 8

SECTIONS = [
    ("breaking", "🔴 Breaking"),
    ("models", "🚀 New Models"),
    ("labs", "🏢 Lab Updates"),
    ("funding", "💰 Funding & M&A"),
    ("research", "📄 Research"),
    ("code", "🐙 Code & Open Source"),
    ("product", "🛠 Products & Tools"),
    ("policy", "⚖️ Policy & Regulation"),
    ("other", "📌 Also Worth Knowing"),
]
SECTION_KEYS = {k for k, _ in SECTIONS}


# ─────────────────────────────── storage ────────────────────────────────────
def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS seen (
        url_hash TEXT PRIMARY KEY,
        title    TEXT,
        tokens   TEXT,
        seen_on  TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS editions (
        edition_date TEXT PRIMARY KEY,
        payload      TEXT,
        status       TEXT,
        built_at     TEXT,
        sent_at      TEXT
    )""")
    conn.commit()
    return conn


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


# ─────────────────────────────── collection ─────────────────────────────────
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "gclid", "ref", "source", "mc_cid", "mc_eid"}


def canonical(url: str) -> str:
    try:
        p = urlparse(url.strip())
        q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING]
        netloc = p.netloc.lower().removeprefix("www.")
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme or "https", netloc, path, "", urlencode(q), ""))
    except Exception:
        return url


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical(url).encode()).hexdigest()[:32]


def tokens_of(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 3}


def fetch_source(src: dict, since: datetime) -> list[dict]:
    """Fetch one source. Never raises — a dead feed must not break the brief."""
    import feedparser

    out: list[dict] = []
    try:
        r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        if src.get("kind") == "hn_json":
            for hit in r.json().get("hits", []):
                url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
                ts = datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc)
                if ts < since:
                    continue
                out.append(_item(src, hit.get("title", ""), url, ts,
                                 social=int(hit.get("points") or 0)))
        else:
            feed = feedparser.parse(r.content)
            for e in feed.entries[:40]:
                url = e.get("link")
                title = (e.get("title") or "").strip()
                if not url or not title:
                    continue
                ts = _entry_time(e)
                if ts < since:
                    continue
                summary = re.sub(r"<[^>]+>", " ", e.get("summary", ""))[:1200]
                out.append(_item(src, title, url, ts, excerpt=" ".join(summary.split())))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {src['name']}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return out


def _entry_time(e) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        if e.get(key):
            return datetime(*e[key][:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _item(src, title, url, ts, excerpt="", social=0) -> dict:
    return {
        "title": title.strip(),
        "url": url,
        "source": src["name"],
        "authority": float(src.get("authority", 0.5)),
        "hint_section": src.get("section", "other"),
        "published_at": ts.astimezone(timezone.utc).isoformat(),
        "excerpt": excerpt,
        "social": social,
        "hash": url_hash(url),
    }


def collect(sources: list[dict], hours: int = 26) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    items, ok, total = [], 0, 0
    for src in sources:
        if not src.get("enabled", True):
            continue
        total += 1
        got = fetch_source(src, since)
        if got:
            ok += 1
        print(f"  · {src['name']:<28} {len(got):>3} items")
        items.extend(got)
    print(f"Collected {len(items)} items from {ok}/{total} live sources")
    if total and ok / total < 0.25:
        raise SystemExit("ABORT: fewer than half the sources responded")
    return items


# ──────────────────────────── dedupe + ranking ──────────────────────────────
def dedupe(items: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """URL dedupe against a 14-day memory, then near-duplicate title clustering."""
    cutoff = (datetime.now(IST) - timedelta(days=14)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM seen WHERE seen_on < ?", (cutoff,))
    known = {row[0] for row in conn.execute("SELECT url_hash FROM seen")}
    recent = [(r[0], set(json.loads(r[1] or "[]")))
              for r in conn.execute("SELECT title, tokens FROM seen WHERE seen_on >= ?",
                                    ((datetime.now(IST) - timedelta(days=3)).strftime("%Y-%m-%d"),))]

    kept: list[dict] = []
    for it in sorted(items, key=lambda x: (-x["authority"], x["published_at"])):
        if it["hash"] in known:
            continue
        toks = tokens_of(it["title"])
        if not toks:
            continue
        dup_of = None
        for other in kept:
            if jaccard(toks, other["_tokens"]) >= 0.55:
                dup_of = other
                break
        if dup_of is None and any(jaccard(toks, t) >= 0.6 for _, t in recent if t):
            continue  # story already covered in a previous edition
        if dup_of:
            dup_of.setdefault("also", []).append({"source": it["source"], "url": it["url"]})
            continue
        it["_tokens"] = toks
        kept.append(it)
        known.add(it["hash"])
    print(f"Deduped {len(items)} → {len(kept)}")
    return kept


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def rank(items: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    for it in items:
        age_h = max(0.0, (now - datetime.fromisoformat(it["published_at"])).total_seconds() / 3600)
        recency = 2.718 ** (-age_h / 14)
        social = min(1.0, (it.get("social", 0) / 300))
        corroboration = min(1.0, len(it.get("also", [])) / 3)
        it["score"] = round(0.45 * it["authority"] + 0.28 * recency
                            + 0.15 * corroboration + 0.12 * social, 4)
    items.sort(key=lambda x: -x["score"])

    # diversity cap: at most 4 items per source
    per_source: dict[str, int] = {}
    out = []
    for it in items:
        n = per_source.get(it["source"], 0)
        if n >= 4:
            continue
        per_source[it["source"]] = n + 1
        out.append(it)
        if len(out) >= MAX_ITEMS:
            break
    return out


# ──────────────────────────────── the LLM ───────────────────────────────────
class LLM:
    """Free-tier chain: Gemini → Groq → GitHub Models → extractive fallback."""

    def __init__(self) -> None:
        self.providers = []
        if os.getenv("GEMINI_API_KEY"):
            self.providers.append(self._gemini)
        if os.getenv("GROQ_API_KEY"):
            self.providers.append(self._groq)
        if os.getenv("GITHUB_TOKEN"):
            self.providers.append(self._github_models)
        self.used = "none"

    def complete(self, prompt: str, max_tokens: int = 2000) -> str | None:
        for fn in self.providers:
            for attempt in range(2):
                try:
                    text = fn(prompt, max_tokens)
                    if text:
                        self.used = fn.__name__.lstrip("_")
                        return text
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! LLM {fn.__name__} attempt {attempt+1}: {exc}", file=sys.stderr)
                    time.sleep(3 + attempt * 5)
        return None

    def _gemini(self, prompt: str, max_tokens: int) -> str:
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={os.environ['GEMINI_API_KEY']}")
        r = requests.post(url, timeout=90, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
        })
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _groq(self, prompt: str, max_tokens: int) -> str:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions", timeout=90,
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            json={"model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2, "max_tokens": max_tokens})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _github_models(self, prompt: str, max_tokens: int) -> str:
        r = requests.post(
            "https://models.github.ai/inference/chat/completions", timeout=90,
            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                     "Accept": "application/vnd.github+json"},
            json={"model": os.getenv("GH_MODEL", "openai/gpt-4.1-mini"),
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2, "max_tokens": min(max_tokens, 4000)})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def parse_json(text: str):
    """LLMs love markdown fences. Strip them, then find the outermost JSON."""
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"[\[{].*[\]}]", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None


ITEM_PROMPT = """You are an editor for a daily AI-industry brief read by a senior business executive.

For EACH numbered item below, using ONLY the title and excerpt provided, return an object:
  "id": the item number
  "summary": one or two plain sentences, max 40 words. No hype words
             (revolutionary, game-changing, unprecedented, groundbreaking).
  "why": max 15 words on why a business leader should care, or null if routine.
  "section": one of breaking, models, labs, funding, research, code, product, policy, other
  "significance": integer 1-5

Rules: invent nothing. If the excerpt is thin, summarise the headline only.
Never state a number that does not appear in the text.
Return ONLY a JSON array. No preamble, no markdown fences.

ITEMS:
{items}"""

EXEC_PROMPT = """You are writing the top of a daily AI brief for a senior executive at an
Indian industrial company (sugar, ethanol, agri-processing).

Here are today's ranked stories:
{digest}

Return ONLY this JSON object, no markdown:
{{
  "exec_summary": "120-150 words on the 3 things that actually matter today and why",
  "takeaways": ["5 bullets", "max 18 words each"],
  "business": "3 short sentences: the implication, the thing to watch, one concrete action.
               Be practical about where AI touches operations, supply chain and compliance —
               not generic SaaS commentary.",
  "subject_tail": "max 60 chars summarising today's top signals for the email subject line"
}}

Ground every claim in the stories above. Invent nothing."""


def summarise_items(items: list[dict], llm: LLM) -> None:
    for start in range(0, len(items), LLM_BATCH):
        batch = items[start:start + LLM_BATCH]
        listing = "\n\n".join(
            f"{i+1}. TITLE: {it['title']}\n   SOURCE: {it['source']}\n"
            f"   EXCERPT: {it['excerpt'][:600] or '(none)'}"
            for i, it in enumerate(batch))
        raw = llm.complete(ITEM_PROMPT.format(items=listing), max_tokens=1800)
        parsed = parse_json(raw) if raw else None
        by_id = {}
        if isinstance(parsed, list):
            for obj in parsed:
                try:
                    by_id[int(obj.get("id"))] = obj
                except (TypeError, ValueError):
                    continue
        for i, it in enumerate(batch):
            obj = by_id.get(i + 1, {})
            it["summary"] = clean(obj.get("summary")) or extractive(it)
            it["why"] = clean(obj.get("why"))
            sec = (obj.get("section") or "").strip().lower()
            it["section"] = sec if sec in SECTION_KEYS else it["hint_section"]
            try:
                it["significance"] = max(1, min(5, int(obj.get("significance", 3))))
            except (TypeError, ValueError):
                it["significance"] = 3
            it["method"] = "llm" if obj else "extractive"
        print(f"  · summarised {start+len(batch)}/{len(items)} (via {llm.used})")


HYPE = re.compile(r"\b(revolutionary|game[- ]chang\w+|unprecedented|groundbreaking|"
                  r"mind[- ]blowing|insane|jaw[- ]dropping)\b", re.I)


def clean(s) -> str | None:
    if not isinstance(s, str):
        return None
    s = " ".join(s.split()).strip()
    return HYPE.sub("notable", s) if s else None


def extractive(it: dict) -> str:
    """No-LLM fallback: first two sentences of the excerpt, else the title."""
    text = it.get("excerpt") or ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(sentences[:2]).strip()
    return out[:300] if len(out) > 40 else it["title"]


def build_executive(items: list[dict], llm: LLM) -> dict:
    digest = "\n".join(
        f"- [{it['section']}] {it['title']} ({it['source']}) — {it.get('summary','')}"
        for it in items[:25])
    raw = llm.complete(EXEC_PROMPT.format(digest=digest), max_tokens=1200)
    obj = parse_json(raw) if raw else None
    if not isinstance(obj, dict):
        top = items[:5]
        return {
            "exec_summary": "Automated summary unavailable today — the headlines below are "
                            "ranked by source authority and recency.",
            "takeaways": [it["title"][:110] for it in top],
            "business": "",
            "subject_tail": ", ".join(it["title"][:28] for it in items[:2]),
        }
    obj["takeaways"] = [clean(t) for t in obj.get("takeaways", []) if clean(t)][:5]
    obj["exec_summary"] = clean(obj.get("exec_summary")) or ""
    obj["business"] = clean(obj.get("business")) or ""
    return obj


# ─────────────────────────────── rendering ──────────────────────────────────
CSS = {
    "bg": "#f4f5f7", "card": "#ffffff", "ink": "#14161a", "mute": "#5b6270",
    "line": "#e3e6eb", "accent": "#b4531f", "dark": "#16181d",
}


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_html(ed: dict) -> str:
    c = CSS
    rows: list[str] = []
    for key, label in SECTIONS:
        group = [i for i in ed["items"] if i["section"] == key]
        if not group:
            continue  # empty sections simply disappear
        rows.append(f"""
    <tr><td style="padding:26px 24px 8px 24px;">
      <div style="font:600 12px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
                  letter-spacing:.12em;text-transform:uppercase;color:{c['accent']};">{label}</div>
    </td></tr>""")
        for it in group:
            also = ""
            if it.get("also"):
                names = ", ".join(sorted({a["source"] for a in it["also"]})[:3])
                also = f" · also: {esc(names)}"
            why = ""
            if it.get("why"):
                why = (f'<div style="margin-top:6px;font:400 13px/1.5 -apple-system,Segoe UI,'
                       f'Roboto,sans-serif;color:{c["accent"]};">→ {esc(it["why"])}</div>')
            rows.append(f"""
    <tr><td style="padding:6px 24px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:{c['card']};border:1px solid {c['line']};border-radius:10px;">
        <tr><td style="padding:14px 16px;">
          <a href="{esc(it['url'])}" style="font:600 16px/1.35 Georgia,serif;
             color:{c['ink']};text-decoration:none;">{esc(it['title'])}</a>
          <div style="margin-top:7px;font:400 14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;
                      color:{c['ink']};">{esc(it.get('summary',''))}</div>
          {why}
          <div style="margin-top:8px;font:400 12px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
                      color:{c['mute']};">{esc(it['source'])}{also}</div>
        </td></tr>
      </table>
    </td></tr>""")

    takeaways = "".join(
        f'<li style="margin-bottom:6px;">{esc(t)}</li>' for t in ed.get("takeaways", []))
    business = ""
    if ed.get("business"):
        business = f"""
    <tr><td style="padding:20px 24px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:#fdf6f0;border:1px solid #f0dfd0;border-radius:10px;">
        <tr><td style="padding:16px 18px;">
          <div style="font:600 12px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
                      letter-spacing:.12em;text-transform:uppercase;color:{c['accent']};">
            💼 What this means for business</div>
          <div style="margin-top:8px;font:400 14px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;
                      color:{c['ink']};">{esc(ed['business'])}</div>
        </td></tr>
      </table>
    </td></tr>"""

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>AI Morning Brief — {ed['date']}</title></head>
<body style="margin:0;padding:0;background:{c['bg']};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{esc(ed['preheader'])}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{c['bg']};">
<tr><td align="center" style="padding:20px 10px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
       style="width:600px;max-width:100%;background:{c['bg']};">

  <tr><td style="background:{c['dark']};padding:22px 24px;border-radius:12px 12px 0 0;">
    <div style="font:700 20px/1.2 Georgia,serif;color:#fff;">AI Morning Brief</div>
    <div style="margin-top:5px;font:400 13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
                color:#9aa2b1;">{esc(ed['date_long'])} · {ed['read_time']} min read ·
                {ed['item_count']} stories from {ed['source_count']} sources</div>
  </td></tr>

  <tr><td style="background:{c['card']};padding:20px 24px;border-left:1px solid {c['line']};
                 border-right:1px solid {c['line']};">
    <div style="font:600 12px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
                letter-spacing:.12em;text-transform:uppercase;color:{c['accent']};">
      Executive summary</div>
    <div style="margin-top:8px;font:400 15px/1.65 Georgia,serif;color:{c['ink']};">
      {esc(ed['exec_summary'])}</div>
    {'<div style="margin-top:14px;font:600 12px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:' + c['accent'] + ';">Key takeaways</div><ul style="margin:8px 0 0 0;padding-left:20px;font:400 14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;color:' + c['ink'] + ';">' + takeaways + '</ul>' if takeaways else ''}
  </td></tr>

  {''.join(rows)}
  {business}

  <tr><td style="padding:22px 24px 30px 24px;text-align:center;
                 font:400 12px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;color:{c['mute']};">
    Generated {esc(ed['generated_at'])} IST · summariser: {esc(ed['llm'])}<br>
    <a href="{esc(ed['archive_url'])}" style="color:{c['accent']};">Read the archive →</a>
  </td></tr>

</table></td></tr></table></body></html>"""


def render_text(ed: dict) -> str:
    lines = [f"AI MORNING BRIEF — {ed['date_long']}",
             f"{ed['item_count']} stories · {ed['read_time']} min read", "",
             "EXECUTIVE SUMMARY", ed["exec_summary"], ""]
    if ed.get("takeaways"):
        lines += ["KEY TAKEAWAYS"] + [f"  - {t}" for t in ed["takeaways"]] + [""]
    for key, label in SECTIONS:
        group = [i for i in ed["items"] if i["section"] == key]
        if not group:
            continue
        lines.append(re.sub(r"[^\w &]", "", label).strip().upper())
        for it in group:
            lines += [f"  {it['title']}", f"    {it.get('summary','')}",
                      f"    {it['source']} — {it['url']}", ""]
    if ed.get("business"):
        lines += ["WHAT THIS MEANS FOR BUSINESS", ed["business"], ""]
    lines.append(ed["archive_url"])
    return "\n".join(lines)


# ────────────────────────────── edition build ───────────────────────────────
def build(demo: bool = False) -> dict:
    conn = db()
    if demo:
        items = demo_items()
        sources_n = 4
    else:
        cfg = yaml.safe_load((ROOT / "sources.yaml").read_text())
        sources = cfg["sources"]
        sources_n = len(sources)
        items = collect(sources)

    items = rank(dedupe(items, conn))
    llm = LLM()
    if items:
        summarise_items(items, llm)
    ed_meta = build_executive(items, llm)

    for it in items:
        it.pop("_tokens", None)

    now = datetime.now(IST)
    words = sum(len((i.get("summary") or "").split()) for i in items) + 200
    date = today_ist()
    edition = {
        "date": date,
        "date_long": now.strftime("%A, %d %B %Y"),
        "generated_at": now.strftime("%H:%M"),
        "items": items,
        "item_count": len(items),
        "source_count": sources_n,
        "read_time": max(2, round(words / 220)),
        "llm": llm.used,
        "exec_summary": ed_meta["exec_summary"],
        "takeaways": ed_meta["takeaways"],
        "business": ed_meta["business"],
        "archive_url": os.getenv("ARCHIVE_URL", "https://example.github.io/ai-brief/"),
    }
    tail = ed_meta.get("subject_tail") or f"{len(items)} stories"
    edition["subject"] = f"AI Brief · {now.strftime('%d %b')} · {tail}"[:78]
    edition["preheader"] = (f"{len(items)} stories · {edition['read_time']} min read · "
                            f"{tail}")[:140]

    # remember what we've seen so tomorrow doesn't repeat it
    for it in items:
        conn.execute(
            "INSERT OR REPLACE INTO seen (url_hash,title,tokens,seen_on) VALUES (?,?,?,?)",
            (it["hash"], it["title"], json.dumps(sorted(tokens_of(it["title"]))), date))
    conn.execute("INSERT OR REPLACE INTO editions (edition_date,payload,status,built_at) "
                 "VALUES (?,?,?,?)",
                 (date, json.dumps(edition), "ready", now.isoformat()))
    conn.commit()

    write_archive(edition)
    print(f"Edition {date}: {len(items)} items, {edition['read_time']} min read, "
          f"summariser={llm.used}")
    return edition


def write_archive(ed: dict) -> None:
    (DOCS / "briefs").mkdir(parents=True, exist_ok=True)
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    (DOCS / "briefs" / f"{ed['date']}.html").write_text(render_html(ed), encoding="utf-8")
    (DOCS / "data" / f"{ed['date']}.json").write_text(json.dumps(ed, indent=1), encoding="utf-8")
    (DOCS / "data" / "latest.json").write_text(json.dumps(ed, indent=1), encoding="utf-8")
    index_path = DOCS / "data" / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else []
    index = [e for e in index if e["date"] != ed["date"]]
    index.insert(0, {"date": ed["date"], "count": ed["item_count"],
                     "read_time": ed["read_time"]})
    index_path.write_text(json.dumps(index[:400], indent=1), encoding="utf-8")


# ──────────────────────────────── delivery ──────────────────────────────────
def send(date: str | None = None, dry_run: bool = False, to: list[str] | None = None) -> None:
    conn = db()
    date = date or today_ist()
    row = conn.execute("SELECT payload,status FROM editions WHERE edition_date=?",
                       (date,)).fetchone()
    if not row:
        raise SystemExit(f"No edition built for {date}. Run: python brief.py generate")
    ed = json.loads(row[0])

    recipients = to or [a.strip() for a in os.environ["RECIPIENTS"].split(",") if a.strip()]
    user, password = os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"]

    html_body, text_body = render_html(ed), render_text(ed)
    size_kb = len(html_body.encode()) / 1024
    if size_kb > 100:
        print(f"! HTML is {size_kb:.0f} KB — Gmail clips above ~102 KB", file=sys.stderr)

    if dry_run:
        print(f"DRY RUN → would send '{ed['subject']}' ({size_kb:.0f} KB) to {recipients}")
        return

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=45) as smtp:
        smtp.login(user, password)
        for addr in recipients:
            msg = EmailMessage()
            msg["Subject"] = ed["subject"]
            msg["From"] = f"AI Morning Brief <{user}>"
            msg["To"] = addr
            msg["List-Unsubscribe"] = f"<mailto:{user}?subject=unsubscribe>"
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
            smtp.send_message(msg)
            print(f"  ✓ sent to {addr}")
            time.sleep(2)

    conn.execute("UPDATE editions SET status='sent', sent_at=? WHERE edition_date=?",
                 (datetime.now(IST).isoformat(), date))
    conn.commit()


def wait_until(hhmm: str) -> None:
    """Absorb GitHub Actions cron drift: sleep until the exact IST minute."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    target = datetime.now(IST).replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (target - datetime.now(IST)).total_seconds()
    if 0 < delta <= 3600:
        print(f"Waiting {delta/60:.1f} min until {hhmm} IST…")
        time.sleep(delta)
    elif delta < 0:
        print(f"Already past {hhmm} IST ({-delta/60:.1f} min late) — sending now.")


# ──────────────────────────────── demo data ─────────────────────────────────
def demo_items() -> list[dict]:
    now = datetime.now(timezone.utc)
    raw = [
        ("Lab releases a smaller model matching last year's flagship on reasoning",
         "Example Lab", 0.95, "models",
         "The model runs on a single GPU and matches the previous flagship on maths and "
         "coding benchmarks while costing roughly a fifth as much to serve."),
        ("Chipmaker reports record data-centre revenue on AI demand", "Example Wire", 0.9,
         "labs", "Quarterly data-centre revenue rose sharply, with the company citing "
                 "sustained orders from cloud providers building inference capacity."),
        ("Agri-tech startup raises $40M Series B for crop-yield forecasting",
         "Example Ventures", 0.75, "funding",
         "The round was led by an infrastructure fund. The company sells yield prediction "
         "to sugar and grain processors across three countries."),
        ("New paper shows retrieval beats fine-tuning for factual accuracy", "arXiv", 0.85,
         "research", "Across six benchmarks the retrieval pipeline reduced factual errors "
                     "compared with a fine-tuned baseline of the same size."),
        ("Regulator publishes draft compliance rules for AI in manufacturing",
         "Example Register", 0.9, "policy",
         "The draft sets documentation requirements for automated decision systems used in "
         "industrial process control, with a consultation window of ninety days."),
    ]
    return [_item({"name": s, "authority": a, "section": sec},
                  t, f"https://example.com/{i}", now - timedelta(hours=i * 3), excerpt=x)
            for i, (t, s, a, sec, x) in enumerate(raw)]


# ─────────────────────────────────── cli ────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="AI Morning Brief — zero-cost edition")
    ap.add_argument("command", choices=["generate", "send", "preview"])
    ap.add_argument("--demo", action="store_true", help="build from sample data, no network")
    ap.add_argument("--date")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--to", help="comma-separated override recipients")
    ap.add_argument("--wait-until", help="e.g. 07:00 — sleep until this IST time, then send")
    args = ap.parse_args()

    if args.command == "generate":
        build(demo=args.demo)
    elif args.command == "send":
        if args.wait_until:
            wait_until(args.wait_until)
        send(args.date, args.dry_run,
             [a.strip() for a in args.to.split(",")] if args.to else None)
    else:
        date = args.date or today_ist()
        path = DOCS / "briefs" / f"{date}.html"
        print(path if path.exists() else f"No brief at {path}")


if __name__ == "__main__":
    main()
