#!/usr/bin/env python3
"""Daily Brief: fetch the news, pick the stories, write a script, voice it, publish the feed.

Runs every morning on GitHub Actions (.github/workflows/daily-episode.yml).
Everything you'd normally want to change is in config.toml, not here.

Modes:
  run    make today's episode (skips if it already exists)
  force  remake today's episode
  probe  check every feed and article extractor, publish nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape, quoteattr

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
UTC = dt.timezone.utc
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept-Language": "en-AU,en;q=0.9",
}
GROQ_URL = os.environ.get("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
ELEVEN_BASE = os.environ.get("ELEVEN_BASE", "https://api.elevenlabs.io")
STORY_BREAK = '<break time="1.4s" />'
BEAT_BREAK = '<break time="0.6s" />'
ELEVEN_MAX_CHARS = 4800

SUMMARY_LINES: list[str] = []


# ---------------------------------------------------------------- utilities

REDACT: list[re.Pattern] = []  # private watchlist names, hidden from the public run logs


def redact(text: str) -> str:
    for pat in REDACT:
        text = pat.sub("[private]", text)
    return text


def log(*args) -> None:
    print(redact(" ".join(str(a) for a in args)), flush=True)


def summary(line: str = "") -> None:
    """Collect lines for the GitHub Actions run summary page."""
    SUMMARY_LINES.append(redact(line))


WARNINGS: list[str] = []


def warn(message: str) -> None:
    """Something went wrong but the episode can still go out."""
    WARNINGS.append(message)
    log(f"  ⚠ {message}")
    summary(f"⚠️ {message}\n")


def write_summary() -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path and SUMMARY_LINES:
        with open(path, "a", encoding="utf-8") as f:
            f.write(redact("\n".join(SUMMARY_LINES)) + "\n")


# ------------------------------------------------------------ private sections

def is_private(cfg: dict, section_id: str) -> bool:
    return any(s["id"] == section_id and s.get("private") for s in cfg["sections"])


def private_hash(cfg: dict, word: str) -> str:
    """Hide a headline word in the public state file, keyed by the private watchlist itself."""
    salt = cfg.get("_private_salt", "")
    return hashlib.sha256(f"{salt}:{word}".encode()).hexdigest()[:12]


def prepare_private_sections(cfg: dict) -> None:
    """Build feeds for sections whose watchlist lives in a GitHub secret.

    The secret has one company per line. Optionally add search terms after an equals sign:
        Woolworths
        Coles = "Coles Group" OR "Coles supermarkets"
    Names never appear in the config, published files or run logs. A section whose secret is
    missing is switched off.
    """
    keep = []
    for section in cfg["sections"]:
        var = section.get("watchlist_secret")
        if not var:
            keep.append(section)
            continue
        raw = os.environ.get(var, "")
        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, terms = line.partition("=")
            terms, _, note = terms.partition("|")  # optional note for the editor after a "|"
            name, terms, note = name.strip(), terms.strip(), note.strip()
            if name:
                entries.append((name, terms or f'"{name}"', note))
        if not entries:
            print(f"  {section['name']}: no {var} secret set, so this section is off", flush=True)
            continue
        section["private"] = True
        cfg["_private_salt"] = cfg.get("_private_salt", "") + raw
        lang = section.get("watchlist_region", "hl=en-AU&gl=AU&ceid=AU:en")
        feeds = []
        notes = [f"{name}: {note}" for name, _, note in entries if note]
        if notes:  # only ever sent to the editor model, never logged or published
            section["brief"] = section["brief"] + " Notes on specific companies (the source shows which " \
                "company's search found a story): " + "; ".join(notes)
        for name, terms, _ in entries:
            q = requests.utils.quote(f"({terms}) when:2d")
            feeds.append({"name": f"Watchlist: {name}", "url": f"https://news.google.com/rss/search?q={q}&{lang}"})
            REDACT.append(re.compile(re.escape(q), re.I))  # the whole encoded search, e.g. in an error message
            for phrase in {name, *re.findall(r'"([^"]{3,})"', terms)}:
                for form in {phrase, requests.utils.quote(phrase), phrase.replace(" ", "+")}:
                    REDACT.append(re.compile(rf"(?<!\w){re.escape(form)}(?!\w)", re.I))
        section["feeds"] = feeds
        print(f"  {section['name']}: watching {len(feeds)} companies from the {var} secret", flush=True)
        keep.append(section)
    cfg["sections"] = keep


def set_output(**values) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in values.items():
        log(f"output {k}={v}")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{k}={v}\n")


def strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(text, "html.parser").get_text(" ", strip=True)).strip()


def spoken_len(script: str) -> int:
    return len(re.sub(r"<break[^>]*/>", " ", script).strip())


ORDINALS = [
    "", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
    "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth",
    "seventeenth", "eighteenth", "nineteenth", "twentieth", "twenty-first", "twenty-second",
    "twenty-third", "twenty-fourth", "twenty-fifth", "twenty-sixth", "twenty-seventh",
    "twenty-eighth", "twenty-ninth", "thirtieth", "thirty-first",
]


def nice_date(d: dt.date) -> str:
    return f"{d.strftime('%A')}, {d.day} {d.strftime('%B')}"


def spoken_date(d: dt.date) -> str:
    return f"{d.strftime('%A')}, the {ORDINALS[d.day]} of {d.strftime('%B')}"


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ------------------------------------------------------------------ fetching

def parse_time(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                return dt.datetime(*t[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                pass
    return None


def fetch_feed(url: str):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if not parsed.entries:
        problem = getattr(parsed, "bozo_exception", None) or "no items"
        raise ValueError(f"not a usable feed ({problem})")
    return parsed


def collect_items(cfg: dict, now: dt.datetime, since: dt.datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Return (items, feed_report). Items published before `since` are skipped."""
    fcfg = cfg["fetch"]
    since = since or now - dt.timedelta(hours=fcfg.get("lookback_hours", 30))
    skip_all = [re.compile(p, re.I) for p in fcfg.get("skip_title_patterns", [])]
    items, report = [], []
    for section in cfg["sections"]:
        # A section can add its own skip patterns on top of the global ones.
        skip = skip_all + [re.compile(p, re.I) for p in section.get("skip_title_patterns", [])]
        for n, feed in enumerate(section["feeds"], 1):
            row = {"section": section["id"], "feed": feed["name"], "url": feed["url"]}
            if section.get("private"):
                row.update(feed=f"watchlist item {n}", url="(private)")
            try:
                parsed = fetch_feed(feed["url"])
            except Exception as e:  # noqa: BLE001 - any failure just skips the feed
                row.update(ok=False, error=str(e)[:120])
                report.append(row)
                log(f"  ✗ {feed['name']}: {row['error']}")
                continue
            kept = skipped = old = 0
            newest = None
            for rank, entry in enumerate(parsed.entries[: fcfg.get("max_items_per_feed", 25)]):
                title = strip_html(entry.get("title", ""))
                # "Live:" on a breaking story is a format, not a sign the story is minor. Strip it
                # so the editor judges the story itself rather than dropping it as a live blog.
                title = re.sub(r"^\s*(live|live updates|breaking)\s*[:|–-]\s*", "", title, flags=re.I)
                link = entry.get("link", "")
                if not title or not link:
                    continue
                published = parse_time(entry)
                if published and (newest is None or published > newest):
                    newest = published
                if published and published < since:
                    old += 1
                    continue
                if any(p.search(title) for p in skip):
                    skipped += 1
                    continue
                body = ""
                if entry.get("content"):
                    body = strip_html(entry["content"][0].get("value", ""))
                items.append({
                    "section": section["id"],
                    "source": feed["name"],
                    "weight": float(feed.get("weight", 1.0)),
                    "ranked": bool(feed.get("ranked", False)),
                    "rank": rank,
                    "title": title,
                    "url": link,
                    "summary": strip_html(entry.get("summary", ""))[:400],
                    "feed_body": body,
                    "published": published or now,
                })
                kept += 1
            row.update(ok=True, kept=kept, skipped=skipped, old=old,
                       newest_hours=round((now - newest).total_seconds() / 3600, 1) if newest else None)
            report.append(row)
            log(f"  ✓ {feed['name']}: {kept} fresh, {old} older, {skipped} filtered")
    return items, report


# ------------------------------------------------------------------- ranking

STOP = set("""a an the and or of to in on for at by with from as is are was were be been being it its
this that these those after over into about up out new says say said will would could can has have had
his her their they he she we you your how why what who when where which while than then not no but
just more most also amid after before first last one two three year years day days week weeks
vs v live update updates latest news report reports""".split())


def tokens(title: str) -> set[str]:
    t = re.sub(r"['’]s\b", "", title.lower())
    return {w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2 and w not in STOP}


def same_story(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    small = min(len(a), len(b))
    if small <= 2:
        return inter == small and inter >= 1 and len(a | b) <= 4
    return inter >= 2 and inter / small >= 0.5


def cluster_and_score(items: list[dict], cfg: dict, now: dt.datetime, covered: list[dict]) -> dict[str, list[dict]]:
    lookback = cfg["fetch"].get("lookback_hours", 30)
    covered_tokens = [(tokens(c["title"]), c) for c in covered if not c.get("private")]
    private_tokens = [(set(c.get("tokens_h", [])), c) for c in covered if c.get("private")]
    by_section: dict[str, list[dict]] = {}
    for section in cfg["sections"]:
        sid = section["id"]
        clusters: list[dict] = []
        for it in [i for i in items if i["section"] == sid]:
            tk = tokens(it["title"])
            for c in clusters:
                if same_story(tk, c["tokens"]):
                    c["items"].append(it)
                    c["tokens"] |= tk
                    break
            else:
                clusters.append({"items": [it], "tokens": set(tk)})
        for c in clusters:
            its = c["items"]
            sources = sorted({i["source"] for i in its})
            lead = max(its, key=lambda i: (i["weight"], -i["rank"]))
            rank_score = max([max(0.0, 1 - i["rank"] / 15) for i in its if i["ranked"]] or [0.0])
            newest = max(i["published"] for i in its)
            recency = max(0.0, 1 - (now - newest).total_seconds() / 3600 / lookback)
            weight = max(i["weight"] for i in its)
            score = weight * (1 + 0.8 * (len(sources) - 1) + 0.6 * rank_score + 0.5 * recency)
            if section.get("private"):
                hashed = {private_hash(cfg, t) for t in tokens(lead["title"])}
                prior = next((cv for tk, cv in private_tokens if same_story(hashed, tk)), None)
            else:
                prior = next((cv for tk, cv in covered_tokens if same_story(tokens(lead["title"]), tk)), None)
            if prior:
                score *= 0.6
            c.update(section=sid, lead=lead, sources=sources, score=round(score, 3), covered=prior)
        clusters.sort(key=lambda c: c["score"], reverse=True)
        by_section[sid] = clusters[: int(section.get("candidates", cfg["fetch"].get("candidates_per_section", 12)))]
        log(f"  {sid}: {len(clusters)} stories from {sum(len(c['items']) for c in clusters)} items")
    return by_section


# ----------------------------------------------------------------------- LLM

class LLMError(RuntimeError):
    pass


def groq_chat(messages: list[dict], cfg: dict, max_tokens: int, json_mode: bool = False,
              temperature: float = 0.7) -> str:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMError("GROQ_API_KEY is not set (add it under Settings → Secrets and variables → Actions)")
    lcfg = cfg["llm"]
    models = [lcfg["model"], *lcfg.get("fallback_models", [])]
    est_prompt = sum(len(m["content"]) for m in messages) // 3 + 100
    budget = int(lcfg.get("tokens_per_minute", 8000))
    want = max(1000, min(max_tokens, budget - est_prompt - 150))
    last_error = "no models tried"
    for model in models:
        body = {"model": model, "messages": messages, "max_completion_tokens": want,
                "temperature": temperature}
        if model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = lcfg.get("reasoning_effort", "low")
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(8):
            try:
                r = requests.post(GROQ_URL, json=body, timeout=180,
                                  headers={"Authorization": f"Bearer {key}"})
            except requests.RequestException as e:
                last_error = f"network: {e}"
                time.sleep(10)
                continue
            if r.status_code == 429:
                wait = r.headers.get("retry-after")
                m = re.search(r"try again in ([\d.]+)(ms|s)", r.text)
                secs = float(wait) if wait and wait.replace(".", "").isdigit() else (
                    float(m.group(1)) / (1000 if m.group(2) == "ms" else 1) if m else 20)
                secs = min(max(secs, 2) + 1, 90)
                log(f"    rate limited by Groq, waiting {secs:.0f}s")
                time.sleep(secs)
                continue
            if r.status_code == 413:
                body["max_completion_tokens"] = int(body["max_completion_tokens"] * 0.7)
                last_error = f"413 request too large: {r.text[:200]}"
                log(f"    {last_error} — retrying smaller")
                if body["max_completion_tokens"] < 800:
                    break
                continue
            if r.status_code in (400, 404) and re.search(r"model|decommission", r.text, re.I) \
                    and "json" not in r.text.lower():
                last_error = f"{model} unavailable: {r.text[:200]}"
                log(f"    {last_error}")
                break
            if r.status_code == 400 and json_mode and "json" in r.text.lower():
                log("    model's JSON didn't validate — retrying without JSON mode")
                body.pop("response_format", None)
                continue
            if r.status_code >= 500:
                last_error = f"{r.status_code} from Groq"
                time.sleep(10)
                continue
            if r.status_code != 200:
                raise LLMError(f"Groq {r.status_code}: {r.text[:300]}")
            data = r.json()
            choice = data["choices"][0]
            usage = data.get("usage", {})
            log(f"    {model}: {usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out, "
                f"finish={choice.get('finish_reason')}")
            if choice.get("finish_reason") == "length":
                # Reasoning tokens count against the limit: never accept a truncated answer.
                if body["max_completion_tokens"] < budget - est_prompt - 150:
                    body["max_completion_tokens"] = budget - est_prompt - 150
                    continue
                last_error = f"{model} ran out of tokens (lower source_chars_per_story if this repeats)"
                log(f"    {last_error}")
                break
            content = (choice["message"].get("content") or "").strip()
            if not content:
                last_error = "empty response"
                continue
            return content
    raise LLMError(f"all models failed: {last_error}")


def parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ----------------------------------------------------------------- selection

SELECT_SYSTEM = "You are the editor of a short daily audio news brief made for one listener in Sydney."

DROP_RULES = """- DROP every time: opinion and analysis columns, explainers with no new event, quizzes, live
  blogs that only recap a finished event (a live page on a breaking story is fine and may well be
  the biggest story of the day), "how to watch" guides, betting and odds, celebrity gossip, sponsored
  content, podcast or video-only items, photo galleries, listicles, and anything where the
  headline is the only fact.
- A story already covered in a recent episode only comes back if there's a clear new
  development. If you pick one, say what's new in "why".
- Never pick two items that are the same story. Two items about the same match, event or
  announcement count as the same story, even if they take different angles.
- More outlets covering a story usually means it's bigger."""


def covered_block(covered: list[dict] | None) -> str:
    if not covered:
        return ""
    lines = "\n".join(f"  - {c['date']}: {c['label']}" for c in covered[-40:] if not c.get("private"))
    return ("\n- ALREADY COVERED in recent episodes. Don't pick these again, or another write-up of the "
            "same match, event or announcement, unless there's a genuinely new development:\n" + lines)


def ask_editor(flat: list[dict], indices: list[int], wanted: str, today: dt.date, cfg: dict,
               lead_rule: bool, covered: list[dict] | None = None) -> list[dict]:
    lines = []
    for idx in indices:
        c = flat[idx]
        lead = c["lead"]
        blurb = (lead["summary"] or "")[: int(cfg["fetch"].get("editor_blurb_chars", 160))]
        note = f"\n     (already covered on {c['covered']['date']})" if c["covered"] else ""
        lines.append(f"[{idx}] {c['section']} | {', '.join(c['sources'])} ({len(c['sources'])}) | "
                     f"{lead['title']}\n     {blurb}{note}")
    lead_line = ("- Within each section, most important first. The first news pick leads the whole "
                 "episode: the story the listener would be most annoyed to have missed.\n" if lead_rule
                 else "- Most important first.\n")
    prompt = f"""Today is {nice_date(today)}. Choose the stories for today's episode.

WHAT TO PICK
{wanted}

CANDIDATES (index | section | outlets covering it (count) | headline, then blurb)
{chr(10).join(lines)}

RULES
{lead_line}{DROP_RULES}{covered_block(covered)}
- Pick exactly the number asked for each section, only from that section's own candidates.
  Pick fewer only if every one of that section's candidates breaks the DROP rules.
- Sections marked OPTIONAL are different: picking zero is normal. Only pick a story there if it
  clearly clears that section's bar.

For each pick, also write "label": a plain headline of at most ten words for the episode notes.
Real names only: no outlet names, URLs, hashtags or clickbait.

Return ONLY JSON: {{"picks": [{{"index": 0, "why": "<at most 8 words>", "label": "<headline>"}}]}}"""
    raw = groq_chat([{"role": "system", "content": SELECT_SYSTEM}, {"role": "user", "content": prompt}],
                    cfg, max_tokens=4000, json_mode=True, temperature=0.3)
    picks = parse_json(raw).get("picks", [])
    return picks if isinstance(picks, list) else []


def select_stories(by_section: dict, cfg: dict, today: dt.date, covered: list[dict] | None = None) -> list[dict]:
    flat: list[dict] = []
    sec_idx: dict[str, list[int]] = {}
    for section in cfg["sections"]:
        for c in by_section.get(section["id"], []):
            sec_idx.setdefault(section["id"], []).append(len(flat))
            flat.append(c)
    if not flat:
        return []
    cap = {s["id"]: s["picks"] for s in cfg["sections"]}
    counts: dict[str, int] = {}
    chosen, seen = [], set()

    def accept(picks: list, allowed: set[int], how: str) -> None:
        for p in picks:
            try:
                idx = int(p["index"])
            except (KeyError, TypeError, ValueError):
                continue
            if idx in seen or idx not in allowed:
                continue
            c = flat[idx]
            sid = c["section"]
            if counts.get(sid, 0) >= cap[sid]:
                continue
            seen.add(idx)
            counts[sid] = counts.get(sid, 0) + 1
            label = re.sub(r"\s+", " ", str(p.get("label") or c["lead"]["title"])).strip()
            chosen.append({**c, "why": str(p.get("why", "")).strip(), "label": label, "picked_by": how})

    def want_line(sec: dict) -> str:
        if sec.get("optional"):
            return f"- {sec['id']}: OPTIONAL, pick 0 to {sec['picks']}. Only stories that clear this bar: {sec['brief']}"
        return f"- {sec['id']}: pick exactly {sec['picks']}. {sec['brief']}"

    wanted = "\n".join(want_line(s) for s in cfg["sections"] if sec_idx.get(s["id"]))
    accept(ask_editor(flat, list(range(len(flat))), wanted, today, cfg, lead_rule=True, covered=covered),
           set(range(len(flat))), "editor")

    # The editor sometimes skips a whole section. Ask again for just that section, and if it
    # still comes back short, fall back to the highest-ranked stories.
    for section in cfg["sections"]:
        sid = section["id"]
        pool = [i for i in sec_idx.get(sid, []) if i not in seen]
        need = cap[sid] - counts.get(sid, 0)
        if need <= 0 or not pool or section.get("optional"):
            continue
        log(f"  {sid}: editor picked {counts.get(sid, 0)} of {cap[sid]}, asking again for this section")
        try:
            accept(ask_editor(flat, pool, f"- {sid}: pick exactly {need}. {section['brief']}", today, cfg,
                              lead_rule=False, covered=covered), set(pool), "editor (second ask)")
        except LLMError as e:
            log(f"    second ask failed: {e}")
        need = cap[sid] - counts.get(sid, 0)
        if need > 0:
            backups = [i for i in pool if i not in seen and not flat[i]["covered"]][:need]
            if backups:
                warn(f"{section['name']}: editor picked too few, so {len(backups)} top-ranked "
                     f"stor{'y' if len(backups) == 1 else 'ies'} filled in")
            accept([{"index": i, "why": "top-ranked story", "label": flat[i]["lead"]["title"]} for i in backups],
                   set(backups), "ranking")
    order = {s["id"]: n for n, s in enumerate(cfg["sections"])}
    chosen.sort(key=lambda c: order[c["section"]])  # stable: keeps the editor's order inside a section
    return chosen


# ------------------------------------------------------------------- reading

BOILERPLATE = re.compile(
    r"sign up|newsletter|subscribe|cookie|advertis|all rights reserved|follow us|"
    r"read more:|related:|download the|get our|you can unsubscribe|this article was|"
    r"contact the|privacy policy|terms of use|share this", re.I)


def extract_article(url: str, limit: int = 4000) -> str:
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "figure",
                     "noscript", "button", "svg"]):
        tag.decompose()
    node = soup.find("article") or soup.find("main") or soup.body
    if node is None:
        return ""
    paras = [p.get_text(" ", strip=True) for p in node.find_all("p")]
    paras = [p for p in paras if len(p) > 60 and not BOILERPLATE.search(p)]
    return re.sub(r"\s+", " ", " ".join(paras))[:limit]


def gather_source_text(story: dict, cfg: dict, limit: int | None = None) -> str:
    limit = int(limit or cfg["fetch"].get("source_chars_per_story", 2400))
    ordered = sorted(story["items"], key=lambda i: (-i["weight"], i["rank"]))
    parts, used = [], 0
    for it in ordered[:3]:
        if used >= limit:
            break
        text = ""
        try:
            text = extract_article(it["url"], limit=limit)
        except Exception as e:  # noqa: BLE001
            log(f"    couldn't read {it['source']}: {str(e)[:80]}")
        if len(text) < 300 and len(it.get("feed_body", "")) > len(text):
            text = it["feed_body"][:limit]
        if len(text) < 200:
            continue
        room = limit - used if not parts else min(limit - used, limit // 2)
        chunk = text[:room]
        parts.append(f"[{it['source']}] {chunk}")
        used += len(chunk)
    if not parts:
        blurbs = " ".join(i["summary"] for i in ordered if i["summary"])[:600]
        if blurbs:
            return (f"(only a short summary was available — use only these facts, "
                    f"keep this story brief, do not invent any detail)\n{blurbs}")
        return "(none available — do not invent any detail; keep this story to two sentences)"
    return "\n\n".join(parts)


# ------------------------------------------------------------------- writing

WRITER_SYSTEM = """You write the spoken script for a personal daily news podcast. A text-to-speech voice \
reads it to one listener, so write for the ear, not the eye.

Voice: a sharp, friendly Australian newsreader talking to one person over breakfast. Warm, direct, \
a little dry. Never hype.

WRITING RULES
- Australian English. Contractions. Short sentences. One idea per breath.
- Every fact comes from the SOURCE TEXT for that story. Never state a number, score, date, name, \
quote or price that isn't in it. If the source is thin, say less rather than filling gaps.
- Your own background knowledge is out of date: competition formats, who manages or plays for \
whom, league positions and form have all changed. Never add those from memory. Use only the source \
text, the data you're given and the CURRENT FACTS below. In particular, never say where a team sits \
in a table (top, bottom, leaders, relegation zone, unbeaten) unless the source or data says so.
- Read each source sentence carefully before reusing it: keep each statistic attached to the \
person or team it's about, and don't merge two facts into one.
- Never assert anything about the listener's own life, plans or opinions.
- Never link a person to a team, club, company or role unless the source text does. If you're \
not sure who someone plays for or manages, leave it out.
- Never narrate structure: no "our top story", "next up", "in other news", "moving on", "turning to", \
"that's all for". The listener can hear the order.
- Write numbers and dates the way a newsreader says them, in words: "two-nil", "about three hundred \
people", "the fourteenth of November", "twenty twenty-six". No digits, symbols or abbreviations a \
voice could misread. Initialisms people say as letters (AFL, NRL, WWE, AEW) are fine.
- Attribute claims that are disputed or come from one side ("police say", "the club says").
- Only spoken words and the pause tags below. No headings, labels, lists, markdown, emojis or \
stage directions.

PAUSE TAGS (use these exact strings)
- Between stories: <break time="1.4s" />
- Between beats inside a story: <break time="0.6s" />"""


def current_facts(cfg: dict) -> str:
    """The [context] facts from config.toml: things the model would otherwise get wrong from memory."""
    facts = cfg.get("context", {}).get("facts", [])
    if not facts:
        return ""
    return "CURRENT FACTS (true today, even if your memory says otherwise):\n" + "\n".join(f"- {f}" for f in facts)


FACT_FIXES: list[dict] = []  # what the checker corrected, published in last_run.json

CHECK_SYSTEM = "You are a meticulous fact-checker for a spoken news script. You compare claims to sources."


def tidy_breaks(text: str) -> str:
    """Clean up pause tags left doubled or dangling after a sentence was removed."""
    beat, story = re.escape(BEAT_BREAK), re.escape(STORY_BREAK)
    text = re.sub(rf"(?:{beat}\s*)+{story}", STORY_BREAK, text)
    text = re.sub(rf"{story}\s*(?:{beat}\s*)+", f"{STORY_BREAK} ", text)
    text = re.sub(rf"(?:{beat}\s*){{2,}}", f"{BEAT_BREAK} ", text)
    text = re.sub(rf"(?:{story}\s*){{2,}}", f"{STORY_BREAK}\n", text)
    text = re.sub(rf"^\s*(?:{beat}|{story})\s*", "", text)
    text = re.sub(rf"\s*(?:{beat}|{story})\s*$", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def split_units(script: str) -> list[list]:
    """The script as [is_pause_tag, text] units: pause tags on their own, text split into sentences."""
    units: list[list] = []
    for p in re.split(r"(<break[^>]*/>)", script):
        if p.startswith("<break"):
            units.append([True, p])
        else:
            units += [[False, s] for s in re.split(r"(?<=[.!?])\s+", p) if s.strip()]
    return units


def fact_check(script: str, source: str, cfg: dict, what: str, private: bool = False) -> str:
    """Second pass: find claims the source doesn't support and fix or drop just those sentences.

    The checker returns exact find/replace pairs rather than a rewritten script, so nothing else in
    the script can drift. Fixes that don't match the script word for word are ignored.
    """
    if not cfg["llm"].get("fact_check", True) or not script.strip():
        return script
    limit = int(cfg["llm"].get("fact_check_source_chars", 9000))
    prompt = f"""Check this SCRIPT for {what} against its SOURCE.

Find every sentence in the SCRIPT that states something the SOURCE doesn't support or that \
contradicts it: wrong or invented numbers, scores, dates, names, table or league positions, \
competition stages or formats, records, who did what, a statistic attached to the wrong person or \
team, or anything added from outside knowledge. Anything that contradicts CURRENT FACTS is wrong too. \
Fair paraphrase, tone and obvious general knowledge (that a match was at home, what a club is) are fine.

{current_facts(cfg)}

SOURCE
{source[:limit]}

SCRIPT
{script}

For each problem sentence, give "find": the sentence copied EXACTLY from the script (without any \
pause tags), "replace": the corrected sentence using only the source (keep numbers in words, same \
voice), or "" to delete it if the source can't support it, and "why": at most twelve words.
If nothing is wrong, return an empty list.
Return ONLY JSON: {{"fixes": [{{"find": "...", "replace": "...", "why": "..."}}]}}"""
    try:
        raw = groq_chat([{"role": "system", "content": CHECK_SYSTEM}, {"role": "user", "content": prompt}],
                        cfg, max_tokens=4000, json_mode=True, temperature=0.1)
        fixes = parse_json(raw).get("fixes", [])
    except Exception as e:  # noqa: BLE001 - a failed check never stops the episode
        log(f"    fact check skipped for {what}: {str(e)[:100]}")
        return script
    def norm(s: str) -> str:  # ignore hyphen, quote and spacing differences when matching
        s = re.sub(r"[‐-―−]", "-", s)
        s = s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
        return re.sub(r"\s+", " ", s).strip().lower().rstrip(".!?")

    units = split_units(script)
    for fx in fixes if isinstance(fixes, list) else []:
        find = norm(re.sub(r"<[^>]*>", "", str(fx.get("find", ""))))
        replace = re.sub(r"<[^>]*>", "", str(fx.get("replace", ""))).strip()
        if len(find) < 12:
            continue
        for u in units:
            sent = norm(u[1])
            if u[0] or not sent or not (find == sent or (find in sent and len(find) >= 0.6 * len(sent))):
                continue
            if norm(replace) == sent:
                break
            if private:
                FACT_FIXES.append({"part": what, "was": "(private)", "now": "(private)", "why": "(private)"})
                log(f"    fact check ({what}): {'fixed' if replace else 'removed'} a sentence")
            else:
                FACT_FIXES.append({"part": what, "was": u[1].strip(), "now": replace or "(removed)",
                                   "why": str(fx.get("why", ""))[:120]})
                log(f"    fact check ({what}): {'fixed' if replace else 'removed'}: {u[1].strip()[:90]}")
            u[1] = replace
            break
    out = tidy_breaks(" ".join(u[1] for u in units if u[1]))
    if spoken_len(out) < 0.45 * spoken_len(script):  # the checker gutted it: trust the original
        log(f"    fact check ({what}) removed too much, keeping the original")
        return script
    return out


def story_weight(stories: list[dict]) -> float:
    """Stories with only a headline or summary to go on count for less, so the writer isn't
    pushed to pad them out with invented detail."""
    return sum(0.4 if s.get("source_text", "").startswith("(") else 1.0 for s in stories)


def section_lengths(section: dict, n: float, cfg: dict) -> tuple[int, int, int]:
    """(target, floor, ceiling) in spoken characters for a section with n stories."""
    L = cfg["length"]
    if section.get("style") == "roundup":
        target = int(section.get("roundup_chars", 1600))
        return target, int(target * 0.7), int(target * 1.5)
    def get(key: str) -> int:
        return int(section.get(key, L[key]))
    return (int(get("target_chars_per_story") * n), int(get("min_chars_per_story") * n),
            int(get("max_chars_per_story") * max(n, 1)))


ROUNDUP_SHAPE = """Write this part as ONE flowing roundup, not a separate segment per item. Shape, with the \
beat pause tag between the three parts and no story pause tags:
1. The headline: the biggest result or news, with enough detail to be useful (the winner, how, \
what it means).
2. Around the scene: the other notable results and news, a sentence or two each, linked naturally.
3. Where it's heading: the storylines and feuds to watch and the next big event or show, \
only as far as the sources say. Never invent a date or a match."""

STORY_SHAPE = """Each story gets three beats, in order, separated by the beat pause tag:
1. What happened: the news itself, plain and specific.
2. The context: what led here, why it matters, who it affects.
3. What's next: the next date, decision, match or consequence to watch. If the source doesn't say, \
name the open question instead. Never invent a date.

Each beat is two to four sentences. Put the story pause tag between stories."""


BRIEF_SHAPE = """Each story is short: two to four sentences in total, covering what happened and why it \
matters to a fan. Mention what's next only if the source says. Put the story pause tag between stories."""


def section_prompt(section: dict, stories: list[dict], cfg: dict, today: dt.date, first: bool) -> str:
    target, floor, _ = section_lengths(section, story_weight(stories), cfg)
    shape = {"roundup": ROUNDUP_SHAPE, "brief": BRIEF_SHAPE}.get(section.get("style", ""), STORY_SHAPE)
    blocks = []
    for n, s in enumerate(stories, 1):
        cov = ""
        if s.get("covered"):
            what = "" if s["covered"].get("private") else f" (\"{s['covered']['label']}\")"
            cov = (f"\nThe listener heard about this on {s['covered']['date']}{what}. "
                   f"Lead with what's new; one sentence of recap at most.")
        blocks.append(f"--- STORY {n}: {s['label']}\nWhy it was picked: {s['why']}{cov}\n"
                      f"SOURCE TEXT:\n{s['source_text']}")
    opening = ("This part opens the episode, straight after a short greeting. Start with the lead "
               "story itself; don't greet the listener." if first else
               f"This part follows other stories. Its first sentence should make the shift to "
               f"{section['name'].lower()} obvious naturally (name the competition, place or person), "
               f"without announcing a segment.")
    return f"""Write the {section['name']} part of today's episode ({nice_date(today)}).

About the listener: {section.get('listener_note', '')}

{current_facts(cfg)}

{shape}

Aim for about {target:,} characters of spoken text for this part, and no fewer than {floor:,}.

{opening}

{chr(10).join(blocks)}
---
Return only the script for this part."""


def clean_script(text: str) -> str:
    def norm_break(m: re.Match) -> str:
        return STORY_BREAK if float(m.group(1)) >= 1 else BEAT_BREAK
    text = re.sub(r"<\s*break\s+time\s*=\s*[\"']?\s*([0-9]*\.?[0-9]+)\s*s?\s*[\"']?\s*/?\s*>", norm_break, text, flags=re.I)
    text = re.sub(r"<(?!break time=\"[01]\.[0-9]s\" />)[^>]*>", "", text)  # any other tag
    text = re.sub(r"^\s*(#+|[-*•]|\d+\.)\s+", "", text, flags=re.M)        # markdown leftovers
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^\s*(story \d+|beat \d+)[:.]?\s*", "", text, flags=re.I | re.M)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    text = re.sub(rf"({re.escape(BEAT_BREAK)}\s*)+$|({re.escape(STORY_BREAK)}\s*)+$", "", text).strip()
    return text


def write_section(section: dict, stories: list[dict], cfg: dict, today: dt.date, first: bool) -> str:
    _, floor, ceiling = section_lengths(section, story_weight(stories), cfg)
    messages = [{"role": "system", "content": WRITER_SYSTEM},
                {"role": "user", "content": section_prompt(section, stories, cfg, today, first)}]
    script = clean_script(groq_chat(messages, cfg, max_tokens=6000))
    chars = spoken_len(script)
    log(f"  {section['id']}: {chars:,} chars (floor {floor:,}, ceiling {ceiling:,})")
    split = section.get("style") != "roundup"

    def right_shape(text: str) -> bool:  # one segment per story, nothing tacked on after them
        return not split or len([p for p in text.split(STORY_BREAK) if p.strip()]) == len(stories)

    fix = None
    if chars < floor:
        fix = (f"That script is {chars:,} characters. It must be at least {floor:,}. You wrote roughly "
               f"one sentence per beat; the brief calls for two to four. Rewrite it at full depth: same "
               f"stories, same order, same voice, genuinely more substance per beat from the source text. "
               f"Don't add stories and don't pad with filler.")
    elif chars > ceiling:
        fix = (f"That script is {chars:,} characters. It must be under {ceiling:,}. Tighten it: same "
               f"stories, same order, same voice, cut repetition and minor detail first.")
    elif not right_shape(script):
        fix = (f"That script has the wrong shape. Write exactly {len(stories)} stor"
               f"{'y' if len(stories) == 1 else 'ies'}, with the story pause tag only between them, and "
               f"nothing after the last one: no summary, no extra commentary paragraphs.")
    if fix:
        if split:
            fix += (f" Keep exactly {len(stories)} stories separated by the story pause tag. Add depth "
                    f"inside each story; never add extra paragraphs or a wrap-up after them.")
        messages += [{"role": "assistant", "content": script}, {"role": "user", "content": fix}]
        try:
            retry = clean_script(groq_chat(messages, cfg, max_tokens=6000))
            new_chars = spoken_len(retry)
            log(f"  {section['id']} retry: {new_chars:,} chars")
            mid = (floor + ceiling) / 2
            better_shape = right_shape(retry) and not right_shape(script)
            worse_shape = right_shape(script) and not right_shape(retry)
            if better_shape or (not worse_shape and abs(new_chars - mid) < abs(chars - mid)):
                script = retry
        except LLMError as e:
            log(f"  {section['id']} retry failed, keeping first draft: {e}")
    source = "\n\n".join(f"--- STORY {n}: {s['label']}\n{s.get('source_text', '')}"
                         for n, s in enumerate(stories, 1))
    return fact_check(script, source, cfg, section["name"], private=bool(section.get("private")))


def assemble(intro: str, sections: list[str], outro: str) -> str:
    body = f" {STORY_BREAK}\n".join(s for s in sections if s)
    return f"{intro} {BEAT_BREAK}\n{body} {STORY_BREAK}\n{outro}".strip()


# ------------------------------------------------------------------ results

ESPN_BASE = os.environ.get("ESPN_BASE", "https://site.api.espn.com/apis/site/v2/sports")


def espn_events(path: str, start: dt.date, end: dt.date) -> list[dict]:
    """Every game from start to end (UTC dates), one request per day."""
    events: dict[str, dict] = {}
    day = start
    while day <= end:
        r = requests.get(f"{ESPN_BASE}/{path}/scoreboard", params={"dates": f"{day:%Y%m%d}"},
                         headers=UA, timeout=20)
        r.raise_for_status()
        for ev in r.json().get("events", []):
            events[str(ev.get("id"))] = ev
        day += dt.timedelta(days=1)
    return list(events.values())


def describe_event(ev: dict, league: str, tz: ZoneInfo) -> dict | None:
    """A finished game as plain facts, or None if it isn't finished."""
    comp = (ev.get("competitions") or [{}])[0]
    status = (comp.get("status") or ev.get("status") or {}).get("type", {})
    if not status.get("completed"):
        return None
    teams = comp.get("competitors", [])
    home = next((c for c in teams if c.get("homeAway") == "home"), None)
    away = next((c for c in teams if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    names = {c.get("team", {}).get("id"): c.get("team", {}).get("displayName", "?") for c in teams}
    scorers: dict[str, list[str]] = {}
    for d in comp.get("details", []) or []:
        who = (d.get("athletesInvolved") or [{}])[0].get("displayName", "")
        if not who:
            continue
        if d.get("scoringPlay"):
            mins = d.get("clock", {}).get("displayValue", "")
            extra = " (pen)" if d.get("penaltyKick") else " (own goal)" if d.get("ownGoal") else ""
            team = names.get(d.get("team", {}).get("id"), "?")
            scorers.setdefault(f"{team} goals", []).append(f"{who} {mins}{extra}".strip())
        elif d.get("redCard"):
            scorers.setdefault("red cards", []).append(who)
    if not scorers:  # basketball and similar: each team's top scorer, from ESPN's game leaders
        for c in teams:
            for cat in c.get("leaders", []) or []:
                if cat.get("name") == "points" and cat.get("leaders"):
                    top = cat["leaders"][0]
                    who = (top.get("athlete") or {}).get("displayName", "")
                    if who:
                        team = c.get("team", {}).get("displayName", "?")
                        scorers.setdefault(f"{team} top scorer", []).append(f"{who} {top.get('displayValue', '')} pts".strip())
    note = status.get("shortDetail") or status.get("detail") or ""
    try:
        kickoff = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        kickoff = None
    return {
        "id": f"{league}:{ev.get('id')}", "league": league, "kickoff": kickoff,
        "home": home.get("team", {}).get("displayName", "?"), "away": away.get("team", {}).get("displayName", "?"),
        "home_score": home.get("score", "?"), "away_score": away.get("score", "?"),
        "note": "" if note in ("FT", "Final", "Full Time") else note,
        "scorers": scorers,
    }


ESPN_STANDINGS = os.environ.get("ESPN_STANDINGS", "https://site.api.espn.com/apis/v2/sports")


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def league_table(path: str) -> dict[str, str]:
    """Current position of every team in a football league table: {team name: '19th of 20, 4 pts
    from 8 games'}. Empty if ESPN has no single table for it."""
    r = requests.get(f"{ESPN_STANDINGS}/{path}/standings", headers=UA, timeout=20)
    r.raise_for_status()
    data = r.json()
    groups = [c.get("standings", {}) for c in data.get("children", [])] or [data.get("standings", {})]
    if len(groups) != 1:  # split into groups or conferences: no single table to quote
        return {}
    entries = groups[0].get("entries", []) or []
    rows = []
    for e in entries:
        stats = {s.get("name"): s.get("value") for s in e.get("stats", [])}
        name = (e.get("team") or {}).get("displayName")
        if name and stats.get("points") is not None:
            rows.append((name, stats))
    if len(rows) < 4:
        return {}
    if all(s.get("rank") for _, s in rows):
        rows.sort(key=lambda r: r[1]["rank"])
    else:
        rows.sort(key=lambda r: (-(r[1].get("points") or 0), -(r[1].get("pointDifferential") or 0)))
    total = len(rows)
    out = {}
    for pos, (name, s) in enumerate(rows, 1):
        played = int(s.get("gamesPlayed") or 0)
        out[name] = f"{ordinal(int(s.get('rank') or pos))} of {total}, {int(s['points'])} pts from {played} games"
    return out


TABLE_CLAIM = re.compile(r"\b(top of the (table|league|ladder)|bottom of the (table|league|ladder)|"
                         r"(league|table) leaders|relegation zone|top four|top spot|bottom spot|"
                         r"(stay|stays|keeps? \w+|move|moves|go|goes|climb|climbs) (top|bottom|clear)|"
                         r"(first|second|third|last) place|in \w+(st|nd|rd|th) place)\b", re.I)


def results_segment(cfg: dict, today: dt.date, now: dt.datetime, state: dict,
                    later: list[str] | None = None) -> tuple[str, str | None, dict]:
    """Scores for games finished since the last episode. Returns (script, notes bullet, info).

    later: labels of stories that follow in the episode, so their matches only get the score here.
    """
    rc = cfg.get("results", {})
    if not rc.get("enabled"):
        return "", None, {}
    tz = ZoneInfo(cfg["show"]["timezone"])
    seen = set(state.get("results_seen", []))
    follow = [t.lower() for t in rc.get("follow_teams", [])]
    start = (now - dt.timedelta(hours=int(rc.get("lookback_hours", 40)))).astimezone(UTC).date()
    # Games that kicked off well before the last episode were already finished by then.
    prev = previous_run_time(state)
    oldest = prev - dt.timedelta(hours=4.5) if prev else None
    end = now.astimezone(UTC).date()
    blocks: list[str] = []
    new_ids: list[str] = []
    info: dict = {"leagues": {}}
    have_table = False
    for lg in rc.get("leagues", []):
        try:
            events = espn_events(lg["path"], start, end)
        except Exception as e:  # noqa: BLE001 - a missing league never stops the episode
            log(f"  results: {lg['name']} unavailable ({str(e)[:80]})")
            info["leagues"][lg["name"]] = f"error: {str(e)[:80]}"
            continue
        done = [d for d in (describe_event(ev, lg["name"], tz) for ev in events)
                if d and d["id"] not in seen and not (oldest and d["kickoff"] and d["kickoff"] < oldest)]
        info["leagues"][lg["name"]] = len(done)
        mode = lg.get("mode", "notable")
        table: dict[str, str] = {}
        if done and lg.get("table", lg["path"].startswith("soccer/") and "nations" not in lg["path"]):
            try:
                table = league_table(lg["path"])
            except Exception as e:  # noqa: BLE001 - positions are optional
                log(f"  results: no table for {lg['name']} ({str(e)[:80]})")
        rows = []
        for d in done:
            new_ids.append(d["id"])  # seen either way, so it's never considered again
            mine = any(t in d["home"].lower() or t in d["away"].lower() for t in follow)
            if mode == "teams" and not mine:
                continue
            line = f"{d['home']} {d['home_score']}-{d['away_score']} {d['away']}"
            if table.get(d["home"]) or table.get(d["away"]):
                have_table = True
                line += (f" [table now: {d['home']} {table.get(d['home'], 'unknown')}; "
                         f"{d['away']} {table.get(d['away'], 'unknown')}]")
            if d["note"]:
                line += f" ({d['note']})"
            if d["scorers"]:
                line += " | " + "; ".join(f"{k}: {', '.join(v)}" for k, v in d["scorers"].items())
            rows.append(("[ADRIAN'S TEAM] " if mine else "") + line)
        if rows:
            how = {"all": "read out EVERY result", "notable": "only the notable ones",
                   "teams": "only Adrian's team"}.get(mode, "only the notable ones")
            blocks.append(f"{lg['name']} ({how}):\n" + "\n".join(f"  {r}" for r in rows))
    state["results_seen"] = (state.get("results_seen", []) + new_ids)[-800:]
    info["new_results"] = len(new_ids)
    if not blocks:
        log("  results: no new results since the last episode")
        return "", None, info
    facts = "\n\n".join(blocks)
    log("  results facts:\n    " + facts.replace("\n", "\n    "))
    n_lines = sum(b.count("\n  ") for b in blocks)
    later_note = ""
    if later:
        later_note = ("\n- These stories play straight after the scores. If a result below is part of "
                      "one, give just the score and scorers for it and leave the rest to the story: "
                      + "; ".join(later))
    target = int(rc.get("chars_per_result", 110)) * min(n_lines, 14) + 250
    prompt = f"""Write a quick-fire scores roundup for today's episode ({nice_date(today)}). It comes \
after other stories, so its first sentence should make the shift to the scores obvious naturally, \
without announcing a segment.

RULES
- Results marked [ADRIAN'S TEAM] come first in their league with a little more detail: the \
scorers (or top scorers, for basketball) and what it means, in two or three sentences.
- Basketball scores are big numbers: say them naturally ("beat the Knicks one-eighteen to \
one-oh-nine") and never say a basketball game was a thrashing unless the margin is twenty or more.
- For a league marked "read out EVERY result", give every result in one short line each, quick and \
varied like a radio sports desk: "Leeds and Man United drew one-all. Brighton thrashed Palace \
five-nil." Name a scorer only for a standout, like a hat-trick or a late winner.
- For leagues marked "only the notable ones", pick at most two results per league and five in \
total: big clubs, upsets, huge scorelines or title-race results. Skip the rest without mentioning \
them. If none are notable, leave that league out.
- Put the beat pause tag between leagues. Write every score and number in words. Use the names \
fans use (Man United, Spurs, Palace, Bayern, Real, Barca).
- Use only the scores and scorers below. Never invent a scorer, minute, standing or table position.
- Table positions: only from a "[table now: ...]" note, which is the table AFTER that game. Mention \
one only when it's notable (top, bottom, first win, a big climb). With no note, say nothing about \
where a team sits or how its season is going ("keeps the pressure on", "title charge", "struggling").{later_note}

{current_facts(cfg)}

Aim for about {target:,} characters of spoken text.

RESULTS
{facts}
---
Return only the script for this part."""
    try:
        script = clean_script(groq_chat([{"role": "system", "content": WRITER_SYSTEM},
                                         {"role": "user", "content": prompt}], cfg, max_tokens=5000))
    except LLMError as e:
        warn(f"Scores roundup skipped today: {str(e)[:150]}")
        return "", None, info
    if not have_table:  # nothing to back up a table claim, so any such sentence is a guess
        units = split_units(script)
        for u in units:
            if not u[0] and TABLE_CLAIM.search(u[1]):
                FACT_FIXES.append({"part": "Scores roundup", "was": u[1].strip(), "now": "(removed)",
                                   "why": "table position with no table data"})
                log(f"    removed unsupported table claim: {u[1][:90]}")
                u[1] = ""
        script = tidy_breaks(" ".join(u[1] for u in units if u[1]))
    script = fact_check(script, facts, cfg, "Scores roundup")
    if spoken_len(script) < 60:
        warn("Scores roundup skipped today: the writer returned almost nothing")
        return "", None, info
    log(f"  results: {spoken_len(script):,} chars from {n_lines} results")
    return script, "Football scores roundup", info


# ------------------------------------------------------------------ fantasy

FPL_BASE = os.environ.get("FPL_BASE", "https://fantasy.premierleague.com/api")


def fpl_get(path: str):
    r = requests.get(f"{FPL_BASE}/{path}", headers=UA, timeout=25)
    r.raise_for_status()
    if "json" not in r.headers.get("Content-Type", "") and not r.text.lstrip().startswith(("{", "[")):
        raise ValueError("FPL returned a non-data page (the game may be updating)")
    return r.json()


def fpl_facts(team_id: int, tz: ZoneInfo, now: dt.datetime) -> tuple[str, dict]:
    """Collect the facts about the listener's FPL team. Returns (facts text, small summary dict)."""
    boot = fpl_get("bootstrap-static/")
    players = {e["id"]: e for e in boot["elements"]}
    teams = {t["id"]: t for t in boot["teams"]}
    events = boot["events"]
    current = next((e for e in events if e.get("is_current")), None)
    upcoming = next((e for e in events if e.get("is_next")), None)
    if current is None and upcoming is None:
        raise ValueError("no current or upcoming gameweek (off-season)")
    entry = fpl_get(f"entry/{team_id}/")
    lines = [f"Team name: {entry.get('name')}"]
    info = {"team": entry.get("name")}

    def pname(pid: int) -> str:
        pl = players[pid]
        return f"{pl['web_name']} ({teams[pl['team']]['short_name']})"

    squad: list[int] = []
    role: dict[int, str] = {}  # player id -> "starter" or "bench"

    def note_roles(picks_list: list[dict]) -> None:
        for pk in picks_list:
            role[pk["element"]] = "starter" if pk.get("position", 99) <= 11 else "bench"
    if current:
        gw = current["id"]
        state = "finished" if current.get("finished") else "in progress"
        picks = fpl_get(f"entry/{team_id}/event/{gw}/picks/")
        live = {e["id"]: e["stats"] for e in fpl_get(f"event/{gw}/live/")["elements"]}
        hist = fpl_get(f"entry/{team_id}/history/").get("current", [])
        mine = next((h for h in hist if h["event"] == gw), {})
        prev = next((h for h in hist if h["event"] == gw - 1), {})
        squad = [pk["element"] for pk in picks["picks"]]
        note_roles(picks["picks"])
        lines.append(f"Gameweek {gw} ({state}): {mine.get('points', '?')} points. Average manager: "
                     f"{current.get('average_entry_score')}. Highest: {current.get('highest_score')}.")
        if mine.get("overall_rank"):
            move = ""
            if prev.get("overall_rank"):
                move = f" (was {prev['overall_rank']:,} after gameweek {gw - 1})"
            lines.append(f"Overall rank: {mine['overall_rank']:,}{move}.")
        if mine.get("event_transfers"):
            lines.append(f"Transfers this gameweek: {mine['event_transfers']}, points hit: {mine.get('event_transfers_cost', 0)}.")
        if picks.get("active_chip"):
            lines.append(f"Chip played: {picks['active_chip']}.")
        xi, bench = [], []
        for pk in picks["picks"]:
            st = live.get(pk["element"], {})
            pts = st.get("total_points", 0)
            tag = " (captain)" if pk.get("is_captain") else " (vice-captain)" if pk.get("is_vice_captain") else ""
            desc = f"{pname(pk['element'])}{tag}: {pts} pts, {st.get('minutes', 0)} mins"
            if st.get("goals_scored"):
                desc += f", {st['goals_scored']} goal(s)"
            if st.get("assists"):
                desc += f", {st['assists']} assist(s)"
            if st.get("bonus"):
                desc += f", {st['bonus']} bonus"
            (xi if pk["multiplier"] > 0 else bench).append(desc)
            if pk.get("is_captain"):
                lines.append(f"Captain: {pname(pk['element'])} scored {pts}, counted x{pk['multiplier']}.")
        lines.append("Starting players: " + "; ".join(xi))
        lines.append("Bench: " + "; ".join(bench))
        info.update(gameweek=gw, finished=bool(current.get("finished")), points=mine.get("points"),
                    rank=mine.get("overall_rank"))
    if upcoming:
        dl = dt.datetime.fromisoformat(upcoming["deadline_time"].replace("Z", "+00:00")).astimezone(tz)
        hours = (dl - now).total_seconds() / 3600
        when = f"{dl.strftime('%A')} {dl.day} {dl.strftime('%B')} at {dl.strftime('%-I:%M %p').lower()} Sydney time"
        lines.append(f"Gameweek {upcoming['id']} deadline: {when}, {hours / 24:.1f} days from now"
                     + (" — SOON" if hours <= 48 else "") + ".")
        info.update(deadline=dl.isoformat(), deadline_hours=round(hours), next_gameweek=upcoming["id"])
        if not squad:
            try:
                prev_picks = fpl_get(f"entry/{team_id}/event/{upcoming['id'] - 1}/picks/")["picks"]
                squad = [pk["element"] for pk in prev_picks]
                note_roles(prev_picks)
            except Exception:  # noqa: BLE001
                squad = []
        fixtures = fpl_get(f"fixtures/?event={upcoming['id']}")
        fx = []
        for pid in squad:
            tid = players[pid]["team"]
            games = []
            for f in fixtures:
                if f["team_h"] == tid:
                    games.append(f"v {teams[f['team_a']]['name']} (home, difficulty {f['team_h_difficulty']})")
                elif f["team_a"] == tid:
                    games.append(f"at {teams[f['team_h']]['name']} (away, difficulty {f['team_a_difficulty']})")
            fx.append(f"{pname(pid)}: " + (" and ".join(games) if games else "NO FIXTURE (blank)"))
        lines.append(f"Your players' gameweek {upcoming['id']} fixtures (difficulty 1 easy to 5 hard): " + "; ".join(fx))
    flags = []
    for pid in squad:
        pl = players[pid]
        if pl.get("status") != "a" or (pl.get("chance_of_playing_next_round") not in (None, 100)):
            chance = pl.get("chance_of_playing_next_round")
            flags.append(f"{pname(pid)}, {role.get(pid, 'in squad')}: {pl.get('news') or 'flagged'}"
                         + (f" ({chance}% chance of playing)" if chance is not None else ""))
    lines.append("Injury and availability flags in your squad: " + ("; ".join(flags) if flags else "none"))
    info["flags"] = flags
    info["_ctx"] = {"boot": boot, "entry": entry, "squad": squad, "next_gw": upcoming["id"] if upcoming else None}
    return "\n".join(lines), info


# ------------------------------------------------------ fantasy: transfer ideas

POSITIONS = {1: "goalkeeper", 2: "defender", 3: "midfielder", 4: "forward"}


def estimate_free_transfers(history: dict, entry: dict, next_gw: int) -> int:
    """Free transfers available for next_gw, rebuilt from the public transfer history.

    FPL rules since 2024/25: one free transfer each gameweek after your first, unused ones roll
    over up to five, and a Wildcard or Free Hit keeps whatever you had. The public API doesn't
    give the number directly, so this is an estimate (special one-off top-ups aren't known).
    """
    chips = {c.get("event"): c.get("name") for c in history.get("chips", [])}
    played = {h["event"]: h for h in history.get("current", [])}
    start = int(entry.get("started_event") or 1)
    ft = 1  # going into the gameweek after the one you started in
    for gw in range(start + 1, next_gw):
        if chips.get(gw) not in ("wildcard", "freehit"):
            ft = max(0, ft - int(played.get(gw, {}).get("event_transfers", 0)))
        ft = min(5, ft + 1)
    return ft


def fpl_transfer_ideas(team_id: int, info: dict, fcfg: dict) -> tuple[list[str], dict]:
    """Suggest like-for-like swaps for the listener's squad.

    Scores every player on recent form, FPL's own expected points for next gameweek and how easy
    his next few fixtures are, then looks for the weakest squad players with a clearly better
    replacement in the same position that fits the budget and the three-per-club rule.
    Returns (fact lines for the writer, summary for the run record).
    """
    ctx = info.get("_ctx") or {}
    boot, entry, squad, next_gw = ctx.get("boot"), ctx.get("entry"), ctx.get("squad"), ctx.get("next_gw")
    if not (boot and squad and next_gw):
        return [], {}
    players = {e["id"]: e for e in boot["elements"]}
    teams = {t["id"]: t for t in boot["teams"]}
    look = max(1, int(fcfg.get("transfer_lookahead", 3)))
    window = range(next_gw, next_gw + look)
    history = fpl_get(f"entry/{team_id}/history/")
    fixtures = [f for f in fpl_get("fixtures/?future=1") if f.get("event") in window]

    ft = estimate_free_transfers(history, entry, next_gw)
    played = history.get("current", [])
    bank = (played[-1].get("bank") if played else entry.get("last_deadline_bank")) or 0
    bank = bank / 10

    runs: dict[int, list[str]] = {}
    ease: dict[int, float] = {}
    for tid in teams:
        games, total = [], 0.0
        for gw in window:
            mine = [f for f in fixtures if f["event"] == gw and tid in (f["team_h"], f["team_a"])]
            if not mine:
                games.append(f"GW{gw} blank")
            for f in mine:
                home = f["team_h"] == tid
                d = f["team_h_difficulty"] if home else f["team_a_difficulty"]
                opp = teams[f["team_a"] if home else f["team_h"]]["short_name"]
                games.append(f"{opp} ({'H' if home else 'A'}, {d})")
                total += 5 - d  # difficulty 2 is worth 3, difficulty 5 is worth 0
        runs[tid] = games
        ease[tid] = total / look

    def available(pl: dict) -> bool:
        chance = pl.get("chance_of_playing_next_round")
        return pl.get("status") == "a" and (chance is None or chance >= 75)

    def score(pl: dict) -> float:
        s = 0.5 * float(pl.get("form") or 0) + 0.4 * float(pl.get("ep_next") or 0) + 0.6 * ease[pl["team"]]
        chance = pl.get("chance_of_playing_next_round")
        if pl.get("status") in ("i", "s", "u", "n") or chance == 0:
            s -= 6  # out injured, suspended or gone: replacing him is nearly free value
        elif chance is not None and chance < 75:
            s -= 3
        return s

    def describe(pl: dict) -> str:
        return (f"{pl['web_name']} ({teams[pl['team']]['short_name']}, {POSITIONS[pl['element_type']]}, "
                f"£{pl['now_cost'] / 10:.1f}m): form {pl.get('form')}, {pl.get('total_points')} pts this "
                f"season, next fixtures {', '.join(runs[pl['team']])}")

    min_minutes = int(fcfg.get("transfer_min_minutes", 270))
    club_count: dict[int, int] = {}
    for pid in squad:
        club_count[players[pid]["team"]] = club_count.get(players[pid]["team"], 0) + 1

    want = max(1, min(ft, int(fcfg.get("max_transfer_ideas", 2))))
    outs = sorted(squad, key=lambda pid: score(players[pid]))
    taken_in: set[int] = set()
    ideas, money = [], bank
    for out_id in outs:
        if len(ideas) >= want:
            break
        out = players[out_id]
        if out["element_type"] == 1 and score(out) > -1:
            continue  # only swap a keeper if he's actually unavailable
        budget = money + out["now_cost"] / 10
        counts = dict(club_count)
        counts[out["team"]] -= 1
        options = [
            p for p in boot["elements"]
            if p["element_type"] == out["element_type"] and p["id"] not in squad and p["id"] not in taken_in
            and available(p) and p.get("minutes", 0) >= min_minutes
            and p["now_cost"] / 10 <= budget + 1e-9 and counts.get(p["team"], 0) < 3
        ]
        options.sort(key=score, reverse=True)
        if not options or score(options[0]) - score(out) < float(fcfg.get("transfer_min_gain", 1.5)):
            continue
        best = options[0]
        cheaper = next((p for p in options[1:6] if p["now_cost"] < best["now_cost"] and p["team"] != best["team"]), None)
        ideas.append({"out": out, "in": best, "alt": cheaper})
        taken_in.add(best["id"])
        money = budget - best["now_cost"] / 10
        club_count = counts
        club_count[best["team"]] = club_count.get(best["team"], 0) + 1

    lines = [f"Free transfers for gameweek {next_gw}: about {ft} (worked out from his transfer history, "
             f"before any transfers he has already made this week). Money in the bank: £{bank:.1f}m."]
    if ideas:
        lines.append(f"Suggested swaps (same position, within budget, max three per club; fixtures shown as "
                     f"opponent, home or away, difficulty 1 easy to 5 hard, over the next {look} gameweeks):")
        for n, idea in enumerate(ideas, 1):
            lines.append(f"  {n}. OUT {describe(idea['out'])}"
                         + (f" — flagged: {idea['out'].get('news')}" if idea["out"].get("news") else ""))
            lines.append(f"     IN {describe(idea['in'])}")
            if idea["alt"]:
                lines.append(f"     Cheaper alternative: {describe(idea['alt'])}")
    else:
        lines.append("No swap stands out clearly over his current players: say his squad looks fine to roll "
                     "the transfer (and bank it for next week).")
    summary_out = {
        "free_transfers_estimate": ft, "bank": round(bank, 1),
        "ideas": [f"{i['out']['web_name']} → {i['in']['web_name']}"
                  + (f" (or {i['alt']['web_name']})" if i["alt"] else "") for i in ideas],
    }
    return lines, summary_out


def write_fpl(cfg: dict, facts: str, focus: list[str], today: dt.date) -> str:
    f = cfg["fpl"]
    target = int(f.get("segment_chars", 1000)) * max(1, len(focus)) // 3 + 300
    prompt = f"""Write a short Fantasy Premier League segment about the listener's own FPL team for \
today's episode ({nice_date(today)}). It comes after other stories, so its first sentence should make \
the shift to his fantasy team obvious naturally, without announcing a segment.

TODAY'S FOCUS (cover only these, in this order, with the beat pause tag between them):
{chr(10).join(f"- {x}" for x in focus)}

Use the official news wording for injuries. Each flagged player is marked as a starter or on the \
bench in his current lineup: say which, and never call a bench player a starter. Only mention \
transfers when the focus asks for transfer ideas, and then only the swaps listed under "Suggested \
swaps" in the facts: never make up a different swap, and don't suggest captains. Keep prices to one \
decimal and don't read out every fixture: name the one or two that matter. \
Never invent a player, score, rank or fixture. Aim for about {target:,} characters of spoken text. Write all numbers in words.

FACTS
{facts}
---
Return only the script for this part."""
    messages = [{"role": "system", "content": WRITER_SYSTEM}, {"role": "user", "content": prompt}]
    script = clean_script(groq_chat(messages, cfg, max_tokens=5000))
    return fact_check(script, facts, cfg, "FPL")


def fpl_segment(cfg: dict, today: dt.date, now: dt.datetime, fstate: dict) -> tuple[str, str | None, dict]:
    """Returns (script, episode-notes bullet, info). Empty script means nothing worth saying today.

    The segment only plays when something is due: a gameweek has finished and hasn't been recapped,
    the next deadline is close, or a player in the squad has a new injury or availability flag.
    fstate (kept in state.json) remembers what has already been said.
    """
    f = cfg.get("fpl", {})
    if not f.get("enabled") or not int(f.get("team_id", 0)):
        return "", None, {}
    try:
        facts, info = fpl_facts(int(f["team_id"]), ZoneInfo(cfg["show"]["timezone"]), now)
    except Exception as e:  # noqa: BLE001 - FPL problems never stop the episode
        warn(f"FPL segment skipped today: {str(e)[:150]}")
        return "", None, {}
    focus, bullet_bits = [], []
    recap_due = remind_due = False
    gw, nxt = info.get("gameweek"), info.get("next_gameweek")
    if gw and info.get("finished") and fstate.get("recapped_gw") != gw:
        focus.append(f"Recap of gameweek {gw}: score against the average, captain, who carried him and "
                     f"who let him down, rank movement.")
        bullet_bits.append(f"gameweek {gw} recap")
        recap_due = True
    remind_hours = int(f.get("remind_hours_before_deadline", 36))
    if nxt and info.get("deadline_hours", 999) <= remind_hours and fstate.get("reminded_gw") != nxt:
        focus.append(f"Deadline reminder for gameweek {nxt}: when it is, plus notable fixtures for his "
                     f"players (blanks, doubles, very easy or hard games).")
        bullet_bits.append(f"gameweek {nxt} deadline")
        remind_due = True
    def flag_key(flag: str) -> str:  # "Name (TEAM), starter: news (x%)" -> "Name (TEAM): news"
        name, _, rest = flag.partition(": ")
        return f"{name.split(',')[0].strip()}: {rest.split(' (')[0].strip()}"

    known = {flag_key(x) for x in fstate.get("flags", [])}
    new_flags = [x for x in info.get("flags", []) if flag_key(x) not in known]
    old_flags = [x for x in info.get("flags", []) if flag_key(x) in known]
    if new_flags:
        focus.append("NEW injury or availability news in his squad (make clear it's new): " + "; ".join(new_flags))
        bullet_bits.append("new injury news")
    if focus and old_flags:
        names = ", ".join(x.partition(": ")[0] for x in old_flags)
        focus.append(f"A one-sentence reminder only, names and starter or bench, no details: still flagged: {names}")

    # Transfer ideas: on the days set by transfer_ideas ("deadline", "recap", "both" or "off").
    ideas_when = str(f.get("transfer_ideas", "deadline")).lower()
    ideas_due = (remind_due and ideas_when in ("deadline", "both")) or (recap_due and ideas_when in ("recap", "both"))
    if ideas_due:
        try:
            idea_lines, idea_info = fpl_transfer_ideas(int(f["team_id"]), info, f)
        except Exception as e:  # noqa: BLE001 - never stop the segment over this
            idea_lines, idea_info = [], {}
            warn(f"FPL transfer ideas skipped today: {str(e)[:150]}")
        if idea_lines:
            facts += "\n" + "\n".join(idea_lines)
            info["transfers"] = idea_info
            focus.append("Transfer ideas for the coming gameweek: how many free transfers he has (say it's "
                         "roughly that many, as it's worked out from his history) and money in the bank, then "
                         "each suggested swap: who out, who in and why in a sentence (form, fixtures, injury), "
                         "plus the cheaper alternative if there is one. Present them as suggestions; he decides.")
            bullet_bits.append("transfer ideas")
    info.pop("_ctx", None)
    info["focus"] = focus
    if not focus:
        log("  FPL: nothing new worth mentioning today, so no segment")
        fstate["flags"] = info.get("flags", [])
        return "", None, info
    log("  FPL facts:\n    " + facts.replace("\n", "\n    "))
    try:
        script = write_fpl(cfg, facts, focus, today)
    except LLMError as e:
        warn(f"FPL segment skipped today: {str(e)[:150]}")
        return "", None, info
    log(f"  fpl: {spoken_len(script):,} chars")
    if spoken_len(script) < 80:
        warn("FPL segment skipped today: the writer returned almost nothing")
        return "", None, info
    # Only now that it's going in the episode, remember what was said.
    if recap_due:
        fstate["recapped_gw"] = gw
    if remind_due:
        fstate["reminded_gw"] = nxt
    fstate["flags"] = info.get("flags", [])
    return script, "Your FPL team: " + ", ".join(bullet_bits), info


# ---------------------------------------------------------------------- audio

def split_for_tts(script: str, limit: int = ELEVEN_MAX_CHARS) -> list[str]:
    """Pack whole stories into requests under the limit; split only at pause tags, then sentences."""
    def pieces(text: str, sep: str) -> list[str]:
        out = [p.strip() for p in text.split(sep)]
        return [p + (f" {sep}" if i < len(out) - 1 else "") for i, p in enumerate(out) if p]

    units: list[str] = []
    for story in pieces(script, STORY_BREAK):
        if len(story) <= limit:
            units.append(story)
            continue
        for beat in pieces(story, BEAT_BREAK):
            if len(beat) <= limit:
                units.append(beat)
                continue
            sentences = re.split(r"(?<=[.!?])\s+", beat)
            buf = ""
            for s in sentences:
                if buf and len(buf) + len(s) + 1 > limit:
                    units.append(buf)
                    buf = ""
                buf = f"{buf} {s}".strip()
            if buf:
                units.append(buf)
    chunks, buf = [], ""
    for u in units:
        if buf and len(buf) + len(u) + 1 > limit:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{u}".strip()
    if buf:
        chunks.append(buf)
    return chunks


def ffmpeg_concat(parts: list[Path], out: Path) -> None:
    if len(parts) == 1:
        shutil.copyfile(parts[0], out)
        return
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", str(out)], check=True)
    lst.unlink()


def audio_seconds(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


class TTSError(RuntimeError):
    pass


def tts_elevenlabs(script: str, cfg: dict, out: Path, work: Path) -> None:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise TTSError("ELEVENLABS_API_KEY is not set")
    v = cfg["voice"]
    url = f"{ELEVEN_BASE}/v1/text-to-speech/{v['elevenlabs_voice_id']}?output_format=mp3_44100_128"
    parts = []
    chunks = split_for_tts(script)
    log(f"  ElevenLabs: {len(chunks)} request(s), {len(script):,} characters")
    for n, chunk in enumerate(chunks):
        for attempt in range(3):
            try:
                r = requests.post(url, timeout=300,
                                  headers={"xi-api-key": key, "accept": "audio/mpeg"},
                                  json={"text": chunk, "model_id": v.get("elevenlabs_model", "eleven_flash_v2_5")})
            except requests.RequestException as e:
                if attempt == 2:
                    raise TTSError(f"network error: {e}") from e
                time.sleep(10)
                continue
            if r.status_code == 200 and r.content:
                break
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(15)
                continue
            raise TTSError(f"ElevenLabs {r.status_code}: {r.text[:300]}")
        p = work / f"part{n:02d}.mp3"
        p.write_bytes(r.content)
        parts.append(p)
    ffmpeg_concat(parts, out)


def ensure_kokoro() -> None:
    try:
        import kokoro  # noqa: F401
        import soundfile  # noqa: F401
        return
    except ImportError:
        pass
    log("  installing Kokoro (first use only on this run, takes a couple of minutes)")
    pip = [sys.executable, "-m", "pip", "install", "-q"]
    subprocess.run(pip + ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"], check=True)
    subprocess.run(pip + ["kokoro>=0.9.4", "soundfile"], check=True)
    if shutil.which("apt-get"):
        subprocess.run("sudo apt-get install -y -qq espeak-ng >/dev/null 2>&1 || true", shell=True)


def split_pauses(script: str) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    pos = 0
    for m in re.finditer(r'<break time="([0-9.]+)s" />', script):
        text = script[pos:m.start()].strip()
        if text:
            out.append(("text", text))
        out.append(("pause", float(m.group(1))))
        pos = m.end()
    tail = script[pos:].strip()
    if tail:
        out.append(("text", tail))
    return out


_KOKORO_PIPE = None


def tts_kokoro(script: str, cfg: dict, out: Path, work: Path) -> None:
    global _KOKORO_PIPE
    ensure_kokoro()
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

    v = cfg["voice"]
    sr = 24000
    if _KOKORO_PIPE is None:  # loading the model is slow, so do it once per run
        _KOKORO_PIPE = KPipeline(lang_code=v.get("kokoro_lang", "b"), repo_id="hexgrad/Kokoro-82M")
    pipe = _KOKORO_PIPE
    audio_parts = []
    for kind, val in split_pauses(script):
        if kind == "pause":
            audio_parts.append(np.zeros(int(sr * float(val)), dtype=np.float32))
            continue
        for result in pipe(val, voice=v.get("kokoro_voice", "bm_george"), speed=1.0):
            _, _, audio = result
            if audio is None:
                continue
            arr = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            audio_parts.append(arr.astype(np.float32).reshape(-1))
            audio_parts.append(np.zeros(int(sr * 0.15), dtype=np.float32))
    if not audio_parts:
        raise TTSError("Kokoro produced no audio")
    wav = work / f"{out.stem}.wav"
    sf.write(wav, np.concatenate(audio_parts), sr)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-codec:a", "libmp3lame",
                    "-b:a", "96k", str(out)], check=True)


def render_audio(texts: list[str], cfg: dict, out: Path) -> tuple[str, list[float]]:
    """Voice each chapter separately, join them, and return (engine used, seconds per chapter).

    Voicing chapter by chapter costs the same ElevenLabs credits (they're per character) and
    tells us exactly where each topic starts, for the chapter markers.
    """
    v = cfg["voice"]

    def run(engine) -> list[float]:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            parts = []
            for n, text in enumerate(texts):
                part = work / f"chapter{n:02d}.mp3"
                sub = work / f"c{n:02d}"
                sub.mkdir()
                engine(text, cfg, part, sub)
                parts.append(part)
            lengths = [audio_seconds(p) for p in parts]
            if len(parts) > 1:
                # The pause between topics, as real silence in the same format as the voice audio.
                gap = work / "gap.mp3"
                silence_like(parts[0], gap, CHAPTER_GAP)
                gap_len = audio_seconds(gap)
                joined = []
                for n, p in enumerate(parts):
                    joined.append(p)
                    if n < len(parts) - 1:
                        joined.append(gap)
                        lengths[n] += gap_len
                parts = joined
            ffmpeg_concat(parts, out)
            return lengths

    if v.get("engine", "elevenlabs") == "elevenlabs":
        try:
            return "elevenlabs", run(tts_elevenlabs)
        except TTSError as e:
            warn(f"ElevenLabs failed, so Kokoro voiced this episode: {str(e)[:150]}")
            if not v.get("fallback_to_kokoro", True):
                raise
            log("  falling back to Kokoro")
    return "kokoro", run(tts_kokoro)


CHAPTER_GAP = 1.4  # seconds of silence between topics, matching the pause between stories


def silence_like(sample: Path, out: Path, seconds: float) -> None:
    """Silent MP3 with the same sample rate, channels and bitrate as sample, so it joins cleanly."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=sample_rate,channels,bit_rate", "-of", "json", str(sample)],
                           capture_output=True, text=True, check=True)
    st = json.loads(probe.stdout)["streams"][0]
    layout = "mono" if int(st.get("channels", 1)) == 1 else "stereo"
    rate = int(st.get("bit_rate") or 128000) // 1000
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"anullsrc=r={st['sample_rate']}:cl={layout}", "-t", str(seconds),
                    "-codec:a", "libmp3lame", "-b:a", f"{rate}k", str(out)], check=True)


def ffmeta_escape(text: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", text)


def embed_chapters(mp3: Path, chapters: list[dict], total: float) -> None:
    """Write chapter markers (ID3 CHAP frames) into the MP3, which Apple Podcasts shows as Chapters."""
    meta = mp3.with_suffix(".ffmeta")
    lines = [";FFMETADATA1"]
    for n, ch in enumerate(chapters):
        end = chapters[n + 1]["start"] if n + 1 < len(chapters) else total
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(ch['start'] * 1000)}",
                  f"END={int(end * 1000)}", f"title={ffmeta_escape(ch['title'])}"]
    meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tagged = mp3.with_name(mp3.stem + "-chapters.mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-i", str(meta),
                    "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1", "-c", "copy",
                    "-id3v2_version", "3", str(tagged)], check=True)
    tagged.replace(mp3)
    meta.unlink()


def section_chapters(section: dict, stories: list[dict], script: str) -> list[dict]:
    """One chapter per story when the section is cleanly split into stories, else one for the section."""
    if section.get("private"):  # neutral title, and the text is kept out of the published script
        return [{"title": section["name"], "text": script, "private": True}]
    parts = [p.strip() for p in script.split(STORY_BREAK) if p.strip()]
    if section.get("style") != "roundup" and len(stories) > 1 and len(parts) == len(stories):
        return [{"title": s["label"], "text": p} for s, p in zip(stories, parts)]
    title = stories[0]["label"] if len(stories) == 1 and section.get("style") != "roundup" else section["name"]
    return [{"title": title, "text": script}]


# ------------------------------------------------------------------ the site

def load_state(site: Path) -> dict:
    p = site / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            log("  state.json unreadable — starting fresh")
    return {"episodes": [], "covered": []}


def prepare_cover(site: Path) -> str:
    """Copy cover art into the site as a JPEG named by its content hash; return the file name.

    Apple caches artwork by URL, so a new image must get a new name. Images are squared and
    resized into Apple's 1400-3000 px range.
    """
    from PIL import Image

    src = next((ROOT / n for n in ("cover.png", "cover.jpg", "cover.jpeg") if (ROOT / n).exists()), None)
    if src is None:
        raise FileNotFoundError("cover.png is missing from the repo")
    img = Image.open(src).convert("RGB")
    w, h = img.size
    if w != h:
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    if not 1400 <= img.size[0] <= 3000:
        img = img.resize((3000, 3000), Image.LANCZOS)
    tmp = site / "cover.tmp.jpg"
    img.save(tmp, "JPEG", quality=88, optimize=True)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()[:8]
    name = f"cover-{digest}.jpg"
    for old in site.glob("cover-*"):
        if old.name != name:
            old.unlink()
    tmp.replace(site / name)
    return name


def build_feed(cfg: dict, episodes: list[dict], cover: str, now: dt.datetime) -> str:
    s = cfg["show"]
    base = s["site_url"].rstrip("/")
    items = []
    for ep in sorted(episodes, key=lambda e: e["date"], reverse=True):
        bullets = ep.get("bullets", [])
        if ep.get("chapters_file"):  # bullets start with a timestamp: one per line, no bullet point
            desc = "\n".join(bullets)
            rich = "<p>" + "<br>".join(html.escape(b) for b in bullets) + "</p>"
            chap_url = base + "/episodes/" + ep["chapters_file"]
            chap = f'\n      <podcast:chapters url={quoteattr(chap_url)} type="application/json+chapters"/>'
        else:
            desc = "\n".join(f"• {b}" for b in bullets) or ep["title"]
            rich = "<ul>" + "".join(f"<li>{html.escape(b)}</li>" for b in bullets) + "</ul>"
            chap = ""
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(desc)}</description>
      <content:encoded><![CDATA[{rich}]]></content:encoded>
      <pubDate>{ep['pub_date']}</pubDate>
      <guid isPermaLink="false">{escape(ep['guid'])}</guid>
      <enclosure url={quoteattr(f"{base}/episodes/{ep['file']}")} length="{ep['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{ep['duration']}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>{chap}
    </item>""")
    sub = f'<itunes:category text={quoteattr(s["subcategory"])}/>' if s.get("subcategory") else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{escape(s['title'])}</title>
    <link>{escape(base)}/</link>
    <atom:link href={quoteattr(base + '/feed.xml')} rel="self" type="application/rss+xml"/>
    <language>{escape(s.get('language', 'en-au'))}</language>
    <description>{escape(s['description'])}</description>
    <itunes:author>{escape(s['author'])}</itunes:author>
    <itunes:owner><itunes:name>{escape(s['author'])}</itunes:name></itunes:owner>
    <itunes:image href={quoteattr(f"{base}/{cover}")}/>
    <image><url>{escape(f"{base}/{cover}")}</url><title>{escape(s['title'])}</title><link>{escape(base)}/</link></image>
    <itunes:category text={quoteattr(s.get('category', 'News'))}>{sub}</itunes:category>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <lastBuildDate>{format_datetime(now.astimezone(UTC))}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""


def write_site(site: Path, cfg: dict, state: dict, now: dt.datetime) -> None:
    keep = int(cfg["show"].get("keep_episodes", 14))
    state["episodes"] = sorted(state["episodes"], key=lambda e: e["date"], reverse=True)[:keep]
    ep_dir = site / "episodes"
    ep_dir.mkdir(parents=True, exist_ok=True)
    wanted = {e["file"] for e in state["episodes"]} | {e.get("chapters_file") for e in state["episodes"]}
    for f in [*ep_dir.glob("*.mp3"), *ep_dir.glob("*.chapters.json")]:
        if f.name not in wanted:
            log(f"  pruning {f.name}")
            f.unlink()
    cover = prepare_cover(site)
    (site / "feed.xml").write_text(build_feed(cfg, state["episodes"], cover, now), encoding="utf-8")
    (site / "state.json").write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    (site / ".nojekyll").write_text("")
    title = html.escape(cfg["show"]["title"])
    (site / "index.html").write_text(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{title}</title><body style='font-family:system-ui;max-width:36em;margin:3em auto;padding:0 1em'>"
        f"<h1>{title}</h1><p>Feed: <a href='feed.xml'>feed.xml</a> · {len(state['episodes'])} episodes</p>",
        encoding="utf-8")


# ---------------------------------------------------------------------- modes

def probe(cfg: dict, now: dt.datetime) -> None:
    log("Probing feeds…")
    items, report = collect_items(cfg, now)
    summary("## Feed check\n")
    summary("| Section | Feed | Status | Fresh items | Newest (hours ago) | Article text |")
    summary("|---|---|---|---|---|---|")
    for row in report:
        extract = ""
        if row.get("ok"):
            sample = next((i for i in items if i["source"] == row["feed"]), None)
            if sample:
                try:
                    extract = f"{len(extract_article(sample['url'])):,} chars"
                except Exception as e:  # noqa: BLE001
                    extract = f"failed: {str(e)[:60]}"
        status = "✅" if row.get("ok") else f"❌ {row.get('error', '')}"
        summary(f"| {row['section']} | {row['feed']} | {status} | {row.get('kept', '')} | "
                f"{row.get('newest_hours', '')} | {extract} |")
    rc = cfg.get("results", {})
    if rc.get("enabled"):
        summary("\n## Scores sources\n")
        summary("| League | Status | Finished games in the last two days |")
        summary("|---|---|---|")
        start = (now - dt.timedelta(hours=48)).astimezone(UTC).date()
        for lg in rc.get("leagues", []):
            try:
                evs = espn_events(lg["path"], start, now.astimezone(UTC).date())
                done = sum(1 for ev in evs if describe_event(ev, lg["name"], ZoneInfo(cfg["show"]["timezone"])))
                summary(f"| {lg['name']} | ✅ | {done} |")
            except Exception as e:  # noqa: BLE001
                summary(f"| {lg['name']} | ❌ {str(e)[:80]} | |")
    set_output(publish="false")


def previous_run_time(state: dict) -> dt.datetime | None:
    if state.get("last_run_at"):
        try:
            return dt.datetime.fromisoformat(state["last_run_at"])
        except ValueError:
            pass
    from email.utils import parsedate_to_datetime
    times = []
    for e in state.get("episodes", []):
        try:
            times.append(parsedate_to_datetime(e["pub_date"]))
        except (KeyError, TypeError, ValueError):
            pass
    return max(times) if times else None


def make_episode(cfg: dict, site: Path, now: dt.datetime, force: bool) -> None:
    today = now.date()
    date_key = today.isoformat()
    state = load_state(site)
    if any(e["date"] == date_key for e in state["episodes"]) and not force:
        log(f"Episode for {date_key} already exists — nothing to do.")
        set_output(publish="false")
        return
    if force and state.get("last_run_date") == date_key:
        # Remaking today's episode: use the same window and scores as the first attempt.
        state["last_run_at"] = state.get("prev_run_at")
        state["results_seen"] = state.get("prev_results_seen", state.get("results_seen", []))
    state["episodes"] = [e for e in state["episodes"] if e["date"] != date_key]
    cutoff = (today - dt.timedelta(days=4)).isoformat()
    state["covered"] = [c for c in state.get("covered", []) if c["date"] >= cutoff and c["date"] != date_key]

    log("1/5 Fetching feeds…")
    # Only stories published since the previous episode (with an hour's overlap), so nothing that
    # was already available yesterday turns up again today.
    lookback = dt.timedelta(hours=cfg["fetch"].get("lookback_hours", 30))
    since = now - lookback
    prev = previous_run_time(state)
    if prev and prev < now:
        since = min(max(since, prev - dt.timedelta(hours=1)), now - dt.timedelta(hours=12))
    log(f"  stories published since {since.astimezone(now.tzinfo):%a %H:%M}")
    items, report = collect_items(cfg, now, since)
    dead = [r["feed"] for r in report if not r.get("ok")]
    log(f"  {len(items)} fresh items" + (f"; feeds failing: {', '.join(dead)}" if dead else ""))
    if not items:
        raise SystemExit("No items from any feed — check the run log.")

    log("2/5 Ranking…")
    by_section = cluster_and_score(items, cfg, now, state["covered"])

    log("3/5 Picking stories…")
    picks = select_stories(by_section, cfg, today, state["covered"])
    if not picks:
        raise SystemExit("The editor model picked nothing — check the run log.")
    for p in picks:
        if is_private(cfg, p["section"]):
            log(f"  [{p['section']}] (private story) [{p.get('picked_by')}]")
        else:
            log(f"  [{p['section']}] {p['label']}  ({', '.join(p['sources'])}) — {p['why']} [{p.get('picked_by')}]")

    log("4/5 Reading articles and writing the script…")
    for p in picks:
        sec = next(x for x in cfg["sections"] if x["id"] == p["section"])
        p["source_text"] = gather_source_text(p, cfg, sec.get("source_chars_per_story"))
        name = "(private story)" if sec.get("private") else p["label"][:60]
        log(f"  {name}: {len(p['source_text']):,} chars of source")
    chapters, first, written = [], True, []  # chapters: {"title", "text"} in playing order
    results_seen_before = list(state.get("results_seen", []))
    res_before = cfg.get("results", {}).get("before_section", "sport")
    after_ids = [s["id"] for s in cfg["sections"]]
    after_ids = after_ids[after_ids.index(res_before):] if res_before in after_ids else []
    res_script, res_bullet, res_info = results_segment(
        cfg, today, now, state, later=[p["label"] for p in picks if p["section"] in after_ids])
    fpl_script, fpl_bullet, fpl_info = fpl_segment(cfg, today, now, state.setdefault("fpl", {}))
    fpl_after = cfg.get("fpl", {}).get("after_section", "sport")
    res_chapter = {"title": "Scores roundup", "text": res_script} if res_script else None
    fpl_chapter = {"title": fpl_bullet or "Your FPL team", "text": fpl_script} if fpl_script else None
    for section in cfg["sections"]:
        if res_chapter and section["id"] == res_before:
            chapters.append(res_chapter)
            res_chapter = None
        stories = [p for p in picks if p["section"] == section["id"]]
        if stories:
            try:
                text = write_section(section, stories, cfg, today, first)
                chapters += section_chapters(section, stories, text)
                written += stories
                first = False
            except LLMError as e:
                warn(f"{section['name']} left out today: {str(e)[:150]}")
        if fpl_chapter and section["id"] == fpl_after:
            chapters.append(fpl_chapter)
            fpl_chapter = None
    if res_chapter:
        chapters.append(res_chapter)
    if fpl_chapter:  # its section was missing today: play it at the end
        chapters.append(fpl_chapter)
    if not written:
        raise SystemExit("No section could be written — check the run log.")
    picks = written
    intro = f"Morning, {cfg['show']['listener']}. It's {spoken_date(today)}."
    outro = cfg["show"].get("outro", "That's your brief.")
    # The greeting rides on the first chapter and the sign-off on the last. Each chapter is voiced
    # separately and the pause between them is added as silence when the audio is joined.
    chapters[0]["text"] = f"{intro} {BEAT_BREAK}\n{chapters[0]['text']}"
    chapters[-1]["text"] = f"{chapters[-1]['text']} {STORY_BREAK}\n{outro}"
    script = f" {STORY_BREAK}\n".join(ch["text"] for ch in chapters).strip()
    # The published copy leaves out private sections (the greeting or sign-off riding on one stays).
    public_parts = []
    for n, ch in enumerate(chapters):
        if not ch.get("private"):
            public_parts.append(ch["text"])
            continue
        public_parts.append(" ".join(
            ([intro] if n == 0 else []) + [f"[{ch['title']}: not published]"] + ([outro] if n == len(chapters) - 1 else [])))
    public_script = redact(f" {STORY_BREAK}\n".join(public_parts).strip())
    (site / "last_script.txt").write_text(public_script, encoding="utf-8")
    log(f"  script: {spoken_len(script):,} spoken characters, {len(chapters)} chapters")

    log("5/5 Rendering audio…")
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "episode.mp3"
        engine, lengths = render_audio([ch["text"] for ch in chapters], cfg, raw)
        start = 0.0
        for ch, length in zip(chapters, lengths):
            ch["start"] = round(start, 2)
            start += length
        try:
            embed_chapters(raw, chapters, audio_seconds(raw))
        except Exception as e:  # noqa: BLE001 - chapters are nice to have, never worth losing the episode
            warn(f"Chapter markers left out today: {str(e)[:150]}")
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()[:8]
        fname = f"{date_key}-{digest}.mp3"
        (site / "episodes").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(raw, site / "episodes" / fname)
    seconds = audio_seconds(site / "episodes" / fname)
    size = (site / "episodes" / fname).stat().st_size
    log(f"  {engine}: {fmt_duration(seconds)} long, {size / 1e6:.1f} MB")
    chapter_list = [{"start": ch["start"], "title": ch["title"]} for ch in chapters]
    (site / "episodes" / f"{date_key}-{digest}.chapters.json").write_text(json.dumps({
        "version": "1.2.0",
        "chapters": [{"startTime": c["start"], "title": c["title"]} for c in chapter_list],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    for c in chapter_list:
        log(f"  {fmt_duration(c['start']):>5}  {c['title']}")

    state["episodes"].append({
        "date": date_key, "title": nice_date(today), "guid": f"{date_key}-{digest}", "file": fname,
        "bytes": size, "duration": fmt_duration(seconds), "pub_date": format_datetime(now.astimezone(UTC)),
        "bullets": [f"{fmt_duration(c['start'])} {c['title']}" for c in chapter_list],
        "chapters_file": f"{date_key}-{digest}.chapters.json", "engine": engine,
    })
    for p in picks:
        if is_private(cfg, p["section"]):  # remembered as hashed words, so the state file reveals nothing
            state["covered"].append({"date": date_key, "label": "(private)", "title": "", "private": True,
                                     "tokens_h": sorted(private_hash(cfg, t) for t in tokens(p["lead"]["title"]))})
        else:
            state["covered"].append({"date": date_key, "label": p["label"], "title": p["lead"]["title"]})
    if state.get("last_run_date") != date_key:
        state["prev_run_at"] = state.get("last_run_at") or (prev.isoformat() if prev else None)
        state["prev_results_seen"] = results_seen_before
    state["last_run_at"] = now.isoformat(timespec="seconds")
    state["last_run_date"] = date_key
    write_site(site, cfg, state, now)
    # A record of how today's episode was chosen, published with the site for troubleshooting.
    private_ids = {s["id"] for s in cfg["sections"] if s.get("private")}
    private_names = {s["name"] for s in cfg["sections"] if s.get("private")}
    (site / "last_run.json").write_text(redact(json.dumps({
        "date": date_key, "ran_at": now.isoformat(timespec="seconds"), "engine": engine,
        "duration": fmt_duration(seconds), "spoken_chars": spoken_len(script), "warnings": WARNINGS,
        "fact_check_fixes": [f for f in FACT_FIXES if f["part"] not in private_names],
        "fpl": fpl_info, "results": res_info,
        "feeds": [r for r in report if r["section"] not in private_ids],
        "private_sections": {sid: {"feeds_ok": sum(1 for r in report if r["section"] == sid and r.get("ok")),
                                   "feeds": sum(1 for r in report if r["section"] == sid),
                                   "stories": sum(1 for p in picks if p["section"] == sid)} for sid in private_ids},
        "candidates": {sid: [{"title": c["lead"]["title"], "sources": c["sources"], "score": c["score"],
                              "covered": bool(c["covered"])} for c in cands]
                       for sid, cands in by_section.items() if sid not in private_ids},
        "picks": [{"section": p["section"], "label": p["label"], "why": p["why"], "picked_by": p.get("picked_by"),
                   "sources": p["sources"], "url": p["lead"]["url"]} for p in picks if p["section"] not in private_ids],
    }, indent=1, ensure_ascii=False)), encoding="utf-8")

    credits = len(script) * float(cfg["voice"].get("elevenlabs_credits_per_char", 1.0))
    SUMMARY_LINES.insert(0, f"## {nice_date(today)}\n")
    summary(f"**{fmt_duration(seconds)}** · {spoken_len(script):,} spoken characters · voice: {engine}"
            + (f" · about {credits:,.0f} ElevenLabs credits" if engine == "elevenlabs" else "") + "\n")
    summary("| Section | Story | Sources | Why |")
    summary("|---|---|---|---|")
    for p in picks:
        if p["section"] in private_ids:
            summary(f"| {p['section']} | (private) | | |")
        else:
            summary(f"| {p['section']} | {p['label']} | {', '.join(p['sources'])} | {p['why']} |")
    if dead:
        summary(f"\nFeeds that failed today: {', '.join(dead)}")
    summary("\n<details><summary>Script</summary>\n\n" + html.escape(public_script).replace("\n", "\n\n") + "\n</details>")
    set_output(publish="true", date=date_key)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default="site", help="folder holding the published site")
    ap.add_argument("--mode", default="run", choices=["run", "force", "probe"])
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    args = ap.parse_args()
    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    prepare_private_sections(cfg)
    now = dt.datetime.now(ZoneInfo(cfg["show"]["timezone"]))
    site = Path(args.site)
    site.mkdir(parents=True, exist_ok=True)
    log(f"{cfg['show']['title']} — {now:%A %d %B %Y %H:%M %Z} — mode: {args.mode}")
    try:
        if args.mode == "probe":
            probe(cfg, now)
        else:
            make_episode(cfg, site, now, force=args.mode == "force")
    finally:
        write_summary()


if __name__ == "__main__":
    main()
