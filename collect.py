#!/usr/bin/env python3
"""Daily brand-marketing job collector for Boston-area roles.

Sources (all official APIs, no scraping):
  * Greenhouse / Lever / Ashby public job-board APIs (employer career pages)
  * JSearch (RapidAPI) - Google for Jobs index, which includes LinkedIn, Indeed,
    Glassdoor, ZipRecruiter and Built In listings          [needs JSEARCH_API_KEY]
  * Adzuna API                                            [needs ADZUNA_APP_ID / ADZUNA_APP_KEY]

Writes jobs.json (read by index.html) and sends alerts for brand-new roles by
email (Gmail SMTP) and/or phone push (ntfy.sh).

Environment variables (set as GitHub Actions secrets):
  JSEARCH_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY        optional sources
  ALERT_EMAIL_TO, GMAIL_USER, GMAIL_APP_PASSWORD        email alerts
  NTFY_TOPIC                                            phone push alerts
  DASHBOARD_URL                                         link used in alerts
  ANTHROPIC_API_KEY                                     optional: nicer "how to land it" tips
Run `python collect.py --dry-run` to fetch and print without writing or alerting.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yml").read_text())
SKILLS = json.loads((ROOT / "skills.json").read_text())
JOBS_FILE = ROOT / "jobs.json"
TZ = dt.timezone(dt.timedelta(hours=-5))  # US Eastern (standard); good enough for "today"
TODAY = dt.datetime.now(TZ).date().isoformat()
UA = {"User-Agent": "boston-brand-job-tracker/1.0 (personal job search)"}
LOG: list[str] = []
RESPONDED: set[str] = set()  # employer boards that answered this run


def log(msg: str) -> None:
    LOG.append(msg)
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- helpers
def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(html.unescape(s))
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|the|group|cranberries)\b\.?", " ", s)
    s = re.sub(r"\(.*?\)", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def match_city(*texts: str) -> str | None:
    blob = " ".join(t for t in texts if t)
    for c in CFG["cities"]:
        if re.search(rf"\b{re.escape(c)}\b", blob, re.I):
            return c
    return None


def title_ok(title: str) -> bool:
    return bool(re.search(CFG["title_include"], title, re.I)) and not re.search(CFG["title_exclude"], title, re.I)


def detect_arrangement(text: str, hint: str | None = None) -> tuple[str, str]:
    h = (hint or "").lower()
    if h in ("hybrid",):
        return "Hybrid", "Marked hybrid by employer."
    if h in ("remote",):
        return "Remote", "Marked remote by employer."
    if h in ("onsite", "on-site", "in-office"):
        return "On-site", "Marked on-site by employer."
    t = text.lower()
    m = re.search(r"[^.\n]{0,80}\bhybrid\b[^.\n]{0,100}", t)
    if m:
        return "Hybrid", m.group(0).strip()[:160]
    if re.search(r"\b(fully remote|100% remote|remote-first|work from anywhere)\b", t):
        return "Remote", "Description says remote."
    if re.search(r"\b(on-?site|in office five days|in-office|5 days (a|per) week in (the )?office)\b", t):
        return "On-site", "Description says on-site."
    return "Unknown", "Not stated."


SAL_RE = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])")


def _num(s: str) -> float:
    s = s.replace(",", "").replace(" ", "")
    return float(s[:-1]) * 1000 if s.lower().endswith("k") else float(s)


def parse_salary_text(text: str) -> tuple[int | None, int | None]:
    """Only stated ranges are used - we never estimate."""
    for m in SAL_RE.finditer(text or ""):
        lo, hi = _num(m.group(1)), _num(m.group(2))
        if 30000 <= lo <= 600000 and lo <= hi:
            return int(lo), int(hi)
    return None, None


def annualize(lo, hi, period: str | None):
    if not (lo or hi):
        return None, None
    p = (period or "year").lower()
    mult = 2080 if "hour" in p else 260 if "day" in p else 52 if "week" in p else 12 if "month" in p else 1
    return (int(float(lo) * mult) if lo else None), (int(float(hi) * mult) if hi else None)


def skills_in(text: str) -> list[str]:
    t = " " + (text or "").lower() + " "
    return [name for name, syns in SKILLS.items() if any(s in t for s in syns)]


REQ_HINT = re.compile(r"(\d+\+?\s*(?:-|–|to)?\s*\d*\+?\s*years|experience|proficien|familiar|degree|ability to|track record|strong (?:understanding|knowledge))", re.I)


def extract_requirements(desc: str) -> list[str]:
    out = []
    for line in re.split(r"\n|•", desc):
        line = line.strip(" -*·\t")
        if 25 <= len(line) <= 220 and REQ_HINT.search(line):
            out.append(line)
        if len(out) >= 6:
            break
    return out


def years_min(desc: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*\d*\s*\+?\s*years", desc or "", re.I)
    return int(m.group(1)) if m and int(m.group(1)) < 25 else None


def seniority(title: str) -> str:
    t = title.lower()
    if re.search(r"\b(vp|vice president|head of|chief)\b", t):
        return "Executive"
    if "director" in t:
        return "Director"
    if re.search(r"\b(senior|sr\.?|lead|principal)\b", t):
        return "Senior"
    if re.search(r"\b(associate|assistant|coordinator|specialist)\b", t):
        return "Associate"
    return "Manager"


def suggested_contact_title(sen: str) -> str:
    return {"Associate": "Brand Manager", "Manager": "Director of Brand Marketing",
            "Senior": "Head of Brand / VP Marketing", "Director": "VP Marketing / CMO",
            "Executive": "Chief Marketing Officer"}.get(sen, "Director of Brand Marketing")


def li_search(company: str, title: str) -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"{company} {title}")


def how_to_land(job: dict, skills: list[str]) -> list[str]:
    tips = []
    if skills:
        tips.append("Mirror the posting's language for: " + ", ".join(skills[:4]) + " — put matching wins in the top third of the resume.")
    if job.get("yearsMin"):
        tips.append(f"They ask for {job['yearsMin']}+ years; make the count obvious (dates + scope of each brand role).")
    if any(s in skills for s in ("Budget management", "P&L ownership")):
        tips.append("Quantify the budget or P&L she has owned — size, results, and a trade-off she made.")
    if "Agency management" in skills:
        tips.append("Prepare one agency story: the brief, how she pushed the work, and the outcome.")
    if "Brand health & measurement" in skills:
        tips.append("Bring a metric that moved (awareness, consideration, penetration, sales) and how it was measured.")
    tips.append("Apply within 48 hours of posting, then message the suggested contact with a 3-line note tied to their current campaign.")
    return tips[:5]


def claude_tips(job: dict, desc: str) -> list[str] | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not desc:
        return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=40, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"), "max_tokens": 400,
                  "messages": [{"role": "user", "content":
                                f"Job: {job['title']} at {job['company']}.\nDescription:\n{desc[:6000]}\n\n"
                                "Write 4 short, specific bullets on how a brand marketer should land THIS role, "
                                "based only on the description. One bullet per line, no preamble, no markdown symbols."}]})
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        lines = [l.strip("•-* ").strip() for l in text.splitlines() if l.strip()]
        return lines[:4] or None
    except Exception as e:  # never fail the run for this
        log(f"[claude] tips skipped: {e}")
        return None


# --------------------------------------------------------------------------- sources
def get_json(url, **kw):
    r = requests.get(url, timeout=30, headers={**UA, **kw.pop("headers", {})}, **kw)
    r.raise_for_status()
    return r.json()


def src_greenhouse(token: str) -> list[dict]:
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
    company = (data.get("meta") or {}).get("name") or token.title()
    out = []
    for j in data.get("jobs", []):
        desc = strip_html(j.get("content"))
        out.append(dict(company=j.get("company_name") or company, title=j["title"], location=(j.get("location") or {}).get("name", ""),
                        url=j["absolute_url"], description=desc, posted=(j.get("first_published") or j.get("updated_at") or "")[:10],
                        source="greenhouse", source_id=f"gh-{token}-{j['id']}", authoritative=True, workplace=None))
    return out


def src_lever(token: str) -> list[dict]:
    data = get_json(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    out = []
    for j in data:
        parts = [j.get("descriptionPlain", "")] + [f"{l.get('text','')}\n{strip_html(l.get('content'))}" for l in j.get("lists", [])] + [j.get("additionalPlain", "")]
        sr = j.get("salaryRange") or {}
        out.append(dict(company=token.title(), title=j["text"], location=(j.get("categories") or {}).get("location", ""),
                        url=j["hostedUrl"], description="\n".join(parts),
                        posted=dt.datetime.fromtimestamp(j.get("createdAt", 0) / 1000, TZ).date().isoformat() if j.get("createdAt") else "",
                        salary=(sr.get("min"), sr.get("max"), sr.get("interval")), workplace=j.get("workplaceType"),
                        source="lever", source_id=f"lv-{token}-{j['id']}", authoritative=True))
    return out


def src_ashby(token: str) -> list[dict]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}", params={"includeCompensation": "true"})
    out = []
    for j in data.get("jobs", []):
        sal = (None, None, None)
        for c in ((j.get("compensation") or {}).get("summaryComponents") or []):
            if c.get("compensationType") == "Salary":
                sal = (c.get("minValue"), c.get("maxValue"), c.get("interval"))
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []]
        out.append(dict(company=token.title(), title=j["title"], location=" / ".join(l for l in locs if l),
                        url=j.get("jobUrl") or j.get("applyUrl"), description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
                        posted=(j.get("publishedAt") or "")[:10], salary=sal, workplace=(j.get("workplaceType") or "").replace("OnSite", "onsite"),
                        source="ashby", source_id=f"ab-{token}-{j['id']}", authoritative=True))
    return out


def src_jsearch(query: str) -> list[dict]:
    key = os.getenv("JSEARCH_API_KEY")
    if not key:
        return []
    data = get_json("https://jsearch.p.rapidapi.com/search", params={"query": query, "page": 1, "num_pages": 1, "date_posted": "week", "country": "us"},
                    headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
    out = []
    for j in data.get("data", []):
        exp = j.get("job_offer_expiration_datetime_utc")
        if exp and exp[:10] < TODAY:
            continue
        out.append(dict(company=j.get("employer_name", ""), title=j.get("job_title", ""),
                        location=f"{j.get('job_city') or ''}, {j.get('job_state') or ''}",
                        url=j.get("job_apply_link") or j.get("job_google_link"), description=j.get("job_description", ""),
                        posted=(j.get("job_posted_at_datetime_utc") or "")[:10],
                        salary=(j.get("job_min_salary"), j.get("job_max_salary"), j.get("job_salary_period")),
                        workplace="remote" if j.get("job_is_remote") else None,
                        source=f"jsearch ({j.get('job_publisher') or 'web'})", source_id=f"js-{j.get('job_id')}", authoritative=False))
    return out


def src_adzuna(q: dict) -> list[dict]:
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    data = get_json("https://api.adzuna.com/v1/api/jobs/us/search/1", params={
        "app_id": app_id, "app_key": app_key, "what": q["what"], "where": q["where"], "distance": 10,
        "max_days_old": 7, "results_per_page": 50, "content-type": "application/json"})
    out = []
    for j in data.get("results", []):
        predicted = str(j.get("salary_is_predicted", "0")) == "1"  # Adzuna estimates -> ignore (we never guess)
        out.append(dict(company=(j.get("company") or {}).get("display_name", ""), title=j.get("title", "").replace("<strong>", "").replace("</strong>", ""),
                        location=(j.get("location") or {}).get("display_name", ""), url=j.get("redirect_url"),
                        description=j.get("description", ""), posted=(j.get("created") or "")[:10],
                        salary=(None, None, None) if predicted else (j.get("salary_min"), j.get("salary_max"), "year"),
                        workplace=None, source="adzuna", source_id=f"az-{j.get('id')}", authoritative=False))
    return out


# --------------------------------------------------------------------------- pipeline
def to_job(raw: dict) -> dict | None:
    if not raw.get("url") or not title_ok(raw.get("title", "")):
        return None
    desc = raw.get("description") or ""
    city = match_city(raw.get("location", ""))
    if not city:
        return None
    arr, arr_note = detect_arrangement(desc, raw.get("workplace"))
    lo, hi, per = (tuple(raw.get("salary") or ()) + (None, None, None))[:3]
    lo, hi = annualize(lo, hi, per)
    if not (lo or hi):
        lo, hi = parse_salary_text(desc)
    sk = skills_in(raw["title"] + " " + desc)
    sen = seniority(raw["title"])
    ckey = raw["company"].lower().strip()
    job = dict(
        id=re.sub(r"[^a-z0-9]+", "-", f"{norm(raw['company'])} {norm(raw['title'])} {city}".lower()).strip("-"),
        company=raw["company"].strip(), title=raw["title"].strip(), city=city,
        address=CFG.get("addresses", {}).get(ckey), addressNote=raw.get("location") or "",
        arrangement=arr, arrangementNote=arr_note, salaryMin=lo, salaryMax=hi, seniority=sen,
        yearsMin=years_min(desc), datePosted=raw.get("posted") or None,
        datePostedNote="" if raw.get("posted") else "Not provided by source.",
        url=raw["url"], source=raw["source"], sourceIds=[raw["source_id"]], authoritative=raw["authoritative"],
        status="verified" if raw["authoritative"] else "listed",
        statusNote="Pulled from the employer's own job board today." if raw["authoritative"] else f"Listed via {raw['source']}; confirm on the employer site.",
        contacts=CFG.get("contacts", {}).get(ckey, []),
        requirements=extract_requirements(desc), text=" ".join(sk).lower() + " " + desc[:4000].lower(),
        summary=re.sub(r"\s+", " ", desc[:280]).strip() + ("…" if len(desc) > 280 else ""),
    )
    if not job["address"]:
        job["addressNote"] = (raw.get("location") or job["city"]) + " — street address not listed (add it under `addresses` in config.yml)."
    ct = suggested_contact_title(sen)
    job["contactSearch"] = {"title": ct, "url": li_search(job["company"], ct)}
    job["contactNote"] = "" if job["contacts"] else "No named contact found automatically — use the LinkedIn search link."
    job["howToLand"] = how_to_land(job, sk)
    job["_desc"] = desc
    return job


def collect(sources_today: set[str]) -> tuple[list[dict], dict]:
    raws, stats = [], {}
    prefix = {"greenhouse": "gh", "lever": "lv", "ashby": "ab"}

    def run(name, fn, arg):
        try:
            got = fn(arg)
            if name in prefix:
                RESPONDED.add(f"{prefix[name]}-{arg}")
            stats[name] = stats.get(name, 0) + len(got)
            raws.extend(got)
        except Exception as e:
            log(f"[warn] {name} '{arg}' skipped: {e}")

    for t in CFG.get("greenhouse", []):
        run("greenhouse", src_greenhouse, t)
    for t in CFG.get("lever", []):
        run("lever", src_lever, t)
    for t in CFG.get("ashby", []):
        run("ashby", src_ashby, t)
    if "aggregators" in sources_today:
        for q in CFG.get("jsearch_queries", []):
            run("jsearch", src_jsearch, q)
        for q in CFG.get("adzuna_queries", []):
            run("adzuna", src_adzuna, q)
    jobs = [j for j in (to_job(r) for r in raws) if j]
    return jobs, stats


def dkey(j: dict) -> str:
    title = re.sub(r"\b(sr\.?|senior)\b", "senior", j["title"], flags=re.I)
    title = re.sub(r"[–—-].*$", "", title)  # drop suffixes like "– Preschool, US Marketing"
    return f"{norm(j['company'])}|{norm(title)}|{j['city']}"


def dedupe(jobs: list[dict]) -> list[dict]:
    """Same company + similar title + city = one listing. Employer-board copy wins."""
    best: dict[str, dict] = {}
    for j in jobs:
        key = dkey(j)
        cur = best.get(key)
        if not cur:
            best[key] = j
            continue
        winner, loser = (j, cur) if (j["authoritative"] and not cur["authoritative"]) else (cur, j)
        winner["sourceIds"] = sorted(set(winner["sourceIds"] + loser["sourceIds"]))
        for f in ("salaryMin", "salaryMax", "datePosted", "address"):
            winner[f] = winner.get(f) or loser.get(f)
        if winner["arrangement"] == "Unknown" and loser["arrangement"] != "Unknown":
            winner["arrangement"], winner["arrangementNote"] = loser["arrangement"], loser["arrangementNote"]
        best[key] = winner
    return list(best.values())


def link_alive(url: str) -> bool:
    try:
        r = requests.get(url, timeout=15, headers=UA, allow_redirects=True, stream=True)
        return r.status_code not in (404, 410)
    except Exception:
        return True  # network hiccup: don't drop the job


def merge(prev: dict, fresh: list[dict], ran_aggregators: bool) -> tuple[list[dict], list[dict], list[dict]]:
    prev_jobs = {j["id"]: j for j in prev.get("jobs", [])}
    prev_by_key = {dkey(j): j for j in prev.get("jobs", [])}
    for j in fresh:  # a live copy of a hand-added/seed job takes over its id (no duplicate, no false "new")
        old = prev_by_key.get(dkey(j))
        if old and old["id"] != j["id"] and old["id"] not in {f["id"] for f in fresh}:
            j["id"] = old["id"]
    fresh_by_id = {j["id"]: j for j in fresh}
    authoritative_boards_seen = set(RESPONDED)
    out, new, expired = [], [], []
    for jid, j in fresh_by_id.items():
        old = prev_jobs.get(jid)
        j["firstSeen"] = old["firstSeen"] if old else TODAY
        j["lastSeen"] = TODAY
        if old and old.get("contacts") and not j["contacts"]:
            j["contacts"] = old["contacts"]
        if not old:
            new.append(j)
        out.append(j)
    today = dt.date.fromisoformat(TODAY)
    for jid, old in prev_jobs.items():
        if jid in fresh_by_id:
            continue
        age_seen = (today - dt.date.fromisoformat(old.get("lastSeen", TODAY))).days
        age_first = (today - dt.date.fromisoformat(old.get("firstSeen", TODAY))).days
        board_ids = {sid.rsplit("-", 1)[0] for sid in old.get("sourceIds", [])}
        if old.get("source") == "manual":
            gone = age_first > CFG.get("manual_max_age_days", 21) or not link_alive(old["url"])
        elif old.get("authoritative") and board_ids & authoritative_boards_seen:
            gone = True  # its employer board responded today and the job wasn't on it
        else:
            gone = age_seen >= CFG.get("expire_after_days", 3)
        (expired if gone else out).append(old)
    return out, new, expired


# --------------------------------------------------------------------------- alerts
def fmt_salary(j):
    if not (j.get("salaryMin") or j.get("salaryMax")):
        return "Salary not listed"
    lo, hi = j.get("salaryMin"), j.get("salaryMax")
    return f"${lo:,.0f}–${hi:,.0f}" if lo and hi else f"${(lo or hi):,.0f}"


def alert(new: list[dict]) -> None:
    if not new:
        return
    url = os.getenv("DASHBOARD_URL", "")
    floor = CFG.get("salary_floor", 80000)
    new = sorted(new, key=lambda j: (j["arrangement"] != "Hybrid", -(j.get("salaryMax") or 0)))
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        lines = [f"{j['company']} — {j['title']} ({j['city']}, {j['arrangement']}, {fmt_salary(j)})" for j in new[:8]]
        try:
            requests.post(f"https://ntfy.sh/{topic}", data="\n".join(lines).encode(), timeout=20, headers={
                "Title": f"{len(new)} new brand role{'s' if len(new) > 1 else ''} in Boston area",
                "Tags": "briefcase", "Click": url or new[0]["url"], "Priority": "high"})
            log("[alert] ntfy sent")
        except Exception as e:
            log(f"[alert] ntfy failed: {e}")
    to, user, pw = os.getenv("ALERT_EMAIL_TO"), os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    if to and user and pw:
        rows = "".join(
            f"<tr><td style='padding:8px 0;border-bottom:1px solid #eee'><b>{html.escape(j['company'])}</b> — "
            f"<a href='{html.escape(j['url'])}'>{html.escape(j['title'])}</a><br>"
            f"<span style='color:#555'>{j['city']} · {j['arrangement']} · {fmt_salary(j)}"
            f"{' · ⭐ priority' if j['arrangement']=='Hybrid' and (j.get('salaryMax') or 0) >= floor else ''}</span></td></tr>"
            for j in new)
        body = (f"<div style='font-family:Arial,sans-serif;max-width:640px'><h2 style='margin:0 0 8px'>New brand roles today</h2>"
                f"<table style='width:100%;border-collapse:collapse'>{rows}</table>"
                + (f"<p><a href='{url}'>Open the dashboard</a> for fit scores, contacts and how-to-land notes.</p>" if url else "") + "</div>")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"{len(new)} new brand marketing role{'s' if len(new) > 1 else ''} — Boston area"
        msg["From"], msg["To"] = user, to
        msg.attach(MIMEText(body, "html"))
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(user, pw)
                s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
            log("[alert] email sent")
        except Exception as e:
            log(f"[alert] email failed: {e}")


# --------------------------------------------------------------------------- main
def main(argv):
    dry = "--dry-run" in argv
    prev = json.loads(JOBS_FILE.read_text()) if JOBS_FILE.exists() else {"meta": {}, "jobs": []}
    last_agg = (prev.get("meta") or {}).get("aggregatorsRunOn")
    run_agg = "--force-aggregators" in argv or last_agg != TODAY
    fresh, stats = collect({"aggregators"} if run_agg else set())
    fresh = dedupe(fresh)
    for j in fresh:  # optional AI tips only for jobs we haven't seen before
        if j["id"] not in {p["id"] for p in prev.get("jobs", [])}:
            tips = claude_tips(j, j["_desc"])
            if tips:
                j["howToLand"] = tips
    for j in fresh:
        j.pop("_desc", None)
    removed = set(CFG.get("remove_ids") or [])
    cutoff = (dt.date.fromisoformat(TODAY) - dt.timedelta(days=CFG.get("max_posting_age_days", 30))).isoformat()
    too_old = lambda j: bool(j.get("datePosted")) and str(j["datePosted"])[:10] < cutoff
    fresh = [j for j in fresh if not too_old(j) and not (j["id"] in removed and not j.get("authoritative"))]
    prev["jobs"] = [j for j in prev.get("jobs", []) if j["id"] not in removed]
    jobs, new, expired = merge(prev, fresh, run_agg)
    stale = [j for j in jobs if too_old(j)]
    jobs = [j for j in jobs if not too_old(j)]
    expired += stale
    # The seed baseline counts as "seen", so the first live run only alerts on truly new roles.
    meta = {"generated": TODAY, "generatedAt": dt.datetime.now(TZ).isoformat(timespec="minutes"), "baseline": False,
            "aggregatorsRunOn": TODAY if run_agg else last_agg, "sources": stats,
            "newToday": sorted({*(prev.get("meta", {}).get("newToday", []) if prev.get("meta", {}).get("generated") == TODAY else []), *[j["id"] for j in new]}),
            "expiredToday": [j["id"] for j in expired], "log": LOG[-30:]}
    log(f"[done] {len(jobs)} active, {len(new)} new, {len(expired)} expired, sources={stats}")
    if dry:
        for j in new:
            print(f"NEW  {j['company']} | {j['title']} | {j['city']} | {j['arrangement']} | {fmt_salary(j)} | {j['url']}")
        return
    JOBS_FILE.write_text(json.dumps({"meta": meta, "jobs": jobs}, indent=1, ensure_ascii=False))
    alert(new)


if __name__ == "__main__":
    main(sys.argv[1:])
