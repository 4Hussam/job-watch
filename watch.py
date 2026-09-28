#!/usr/bin/env python3
"""
job-watch — polls remote-job boards, filters to Hussam's stack, emails a digest.

Runs on GitHub Actions (cron), so it keeps working when his laptop is off.
Pure stdlib on purpose: no pip install, no requirements.txt to rot.
"""

import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

STATE = Path(__file__).parent / "state"
SEEN_PATH = STATE / "seen.json"

TO_EMAIL = os.environ.get("WATCH_TO", "hrs18jan@gmail.com")
FROM_EMAIL = os.environ.get("WATCH_FROM", TO_EMAIL)
GMAIL_USER = os.environ.get("GMAIL_USER", TO_EMAIL)
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# Match the work he can actually deliver and defend in an interview.
WANT = re.compile(
    r"\b(full[\s-]?stack|backend|front[\s-]?end|typescript|javascript|node\.?js|"
    r"astro|react|postgres|postgres(?:ql)?|mysql|firebase|firestore|rest api|"
    r"api development|tailwind|web develop|software engineer|web application)\b",
    re.I,
)

# Noise we never want in a digest.
BLOCK = re.compile(
    r"\b(sales|marketing|recruiter|account executive|teacher|driver|"
    r"nurse|chef|accountant|customer support|call center|warehouse|"
    r"mechanical|electrician|plumber|security guard|"
    # he has stated he has no embedded experience — don't sell him these
    r"embedded|firmware|microcontroller|arduino|fpga|rtos|"
    r"android|ios|swift|kotlin|react native|flutter|"
    r"unity|unreal|game developer|devops|sysadmin|site reliability|"
    # local-language postings are almost always geo-locked
    r"office assistant|virtual assistant|executive assistant|"
    r"softwareentwickler|entwickler|entwicklerin|büro|"
    r"développeur|développeuse|desarrollador|desarrolladora|"
    r"sviluppatore|ontwikkelaar|programador|programadora)\b",
    re.I,
)

# He is honest about being mid-level. Filter out the roles he would be
# filtered out of, so his limited attention goes where he can actually win.
TOO_SENIOR = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|head of|director|architect|"
    r"vp|chief|iii|manager)\b",
    re.I,
)

# Extra words that make a hit *more* worth surfacing.
BOOST = re.compile(
    r"\b(remote|anywhere|worldwide|contract|freelance|part[\s-]?time|"
    r"junior|entry|associate|intern|contract to hire)\b",
    re.I,
)

# Roles that are only open to people who already hold the right to work there.
# He has a Palestinian ID and no passport, so these are dead ends.
GEO_BLOCK = re.compile(
    r"\b(germany|german|berlin|munich|hamburg|frankfurt|uk|united kingdom|england|"
    r"london|scotland|ireland|france|paris|netherlands|amsterdam|spain|madrid|"
    r"switzerland|swiss|zurich|zurich|austria|vienna|belgium|brussels|sweden|"
    r"stockholm|norway|oslo|denmark|copenhagen|finland|helsinki|poland|polish|"
    r"warsaw|portugal|lisbon|italy|milan|rome|italia|czech|prague|canada|toronto|"
    r"australia|sydney|melbourne|new zealand|california|new york|texas|"
    r"florida|seattle|austin|boston|chicago|denver|washington|oregon|"
    r"colorado|arizona|georgia|virginia|carolina|pennsylvania|massachusetts|"
    r"münster|muenster|munster|bielefeld|hannover|nürnberg|nuernberg|"
    r"lille|lyon|marseille|toulouse|rotterdam|utrecht|eindhoven|"
    r"brno|krakow|kraków|wroclaw|wrocław|gdansk|gdańsk)\b",
    re.I,
)

# ...unless the posting explicitly says it is open worldwide.
GEO_OK = re.compile(r"\b(anywhere|worldwide|anywhere in the world|global|"
                    r"no location restriction|location[ -]?independent)\b", re.I)

BOARDS = [
    ("FindAsync", "https://findasync.com/jobs/latest"),
    ("RemoteOK",  "https://remoteok.com/api"),
    ("Remotive",  "https://remotive.com/api/remote-jobs"),
    ("Arbeitnow", "https://www.arbeitnow.com/api/job-board-api"),
    ("Jobicy",    "https://jobicy.com/api/v2/remote-jobs"),
    ("Himalayas", "https://himalayas.app/api/jobs"),
]


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def load_seen():
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_seen(seen):
    STATE.mkdir(parents=True, exist_ok=True)
    # trim so the file cannot grow without bound
    if len(seen) > 2000:
        for k in sorted(seen, key=lambda x: seen[x])[:1000]:
            seen.pop(k, None)
    SEEN_PATH.write_text(json.dumps(seen, indent=0, ensure_ascii=False), encoding="utf-8")


def text_of(job):
    bits = [job.get("title") or job.get("position") or "", job.get("description") or "",
            job.get("company") or "", " ".join(job.get("tags") or [])]
    try:
        d = job.get("description") or ""
        if d and len(d) > 4000:
            bits.append(d[:4000])
    except Exception:
        pass
    return re.sub(r"<[^>]+>", " ", " ".join(str(b) for b in bits))


def relevance(job):
    t = text_of(job)
    if not WANT.search(t):
        return 0
    if BLOCK.search(job.get("title") or job.get("position") or ""):
        return 0

    # Only judge location on the explicit field, not free-text noise.
    loc = " ".join(str(job.get(k) or "") for k in
                   ("candidate_required_location", "location", "job_type",
                    "region", "country"))
    if loc.strip() and GEO_BLOCK.search(loc) and not GEO_OK.search(loc):
        return 0

    score = 2
    if GEO_OK.search(t) or GEO_OK.search(loc):
        score += 3  # genuinely open to him
    if BOOST.search(t):
        score += 1
    low = t.lower()
    for strong in ("typescript", "node.js", "nodejs", "astro", "firebase", "postgres"):
        if strong in low:
            score += 1
    # Prefer jobs that are actually open, not ancient postings.
    when = parse_date(job)
    if when:
        age = (datetime.now(timezone.utc) - when).days
        if age <= 3:
            score += 2
        elif age <= 14:
            score += 1
        elif age > 45:
            score -= 2
    return score


def parse_date(job):
    """Boards give ISO strings, unix ints, ms ints, or garbage. Normalise to UTC."""
    raw = (job.get("date_published") or job.get("created_at")
           or job.get("publication_date") or job.get("date") or "")
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        try:
            n = int(raw)
            if n > 10_000_000_000:   # milliseconds
                n //= 1000
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(raw, str) and len(raw) >= 10:
        try:
            d = datetime.fromisoformat(raw[:19].replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def norm(job, board):
    # Boards disagree on field names; RemoteOK uses `position`, Arbeitnow `title`.
    title = (job.get("title") or job.get("position") or job.get("name") or "").strip()
    title = re.sub(r"\s+", " ", title)[:120]
    link = (job.get("url") or job.get("apply_url") or job.get("link") or "").strip()
    key = f"{board}|{title.lower()[:60]}|{link[:60]}"
    salary = job.get("salary") or ""
    if not salary:
        lo, hi = job.get("salary_min"), job.get("salary_max")
        if lo or hi:
            salary = f"{lo or 0}-{hi or 0}"
    return {
        "key": key,
        "title": title,
        "company": (job.get("company") or job.get("company_name") or "").strip()[:60],
        "url": link,
        "loc": (job.get("candidate_required_location")
                or job.get("location") or job.get("job_type") or "").strip()[:60],
        "salary": str(salary)[:40],
    }


def pull(board, url):
    out, err = [], None
    try:
        raw = fetch(url)
    except Exception as e:
        return out, f"{board}: {type(e).__name__}"
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        return out, f"{board}: bad json ({type(e).__name__})"

    jobs = data
    if isinstance(data, dict):
        for key in ("jobs", "data", "results", "listings"):
            if isinstance(data.get(key), list):
                jobs = data[key]
                break
    if not isinstance(jobs, list):
        return out, f"{board}: unexpected shape"

    for j in jobs:
        if not isinstance(j, dict):
            continue
        # Country-specific mirrors (arbeitnow.ch/.fr/...) are geo-locked by definition.
        link = str(j.get("url") or j.get("apply_url") or j.get("link") or "")
        m = re.search(r"https?://(?:www\.)?arbeitnow\.[a-z.]+/", link)
        if m and not re.search(r"arbeitnow\.com/", link):
            continue
        title = j.get("title") or j.get("position") or ""
        if TOO_SENIOR.search(str(title)):
            continue
        if relevance(j) >= 3:
            out.append(norm(j, board))
    return out, err


def send_digest(subject, lines):
    if not GMAIL_PASS:
        print("!! no GMAIL_APP_PASSWORD — printing digest to stdout only")
        print("\n".join(lines))
        return False
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL
    msg["Subject"] = subject
    msg.set_content("\n".join(lines))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.send_message(msg)
    return True


def main():
    seen = load_seen()
    fresh, problems = [], []

    for board, url in BOARDS:
        jobs, err = pull(board, url)
        if err:
            problems.append(err)
        for j in jobs:
            if j["key"] not in seen and j["url"]:
                seen[j["key"]] = datetime.now(timezone.utc).isoformat()
                fresh.append(j)

    save_seen(seen)

    now = datetime.now(timezone.utc) + timedelta(hours=3)
    stamp = now.strftime("%Y-%m-%d %H:%M")

    if problems:
        print("board issues: " + " | ".join(problems))

    if not fresh:
        send_digest(f"Job watch — nothing new ({stamp})",
                    ["No new matching roles this cycle.", "",
                     f"Tracked {len(seen)} roles so far."])
        print(f"no new roles ({len(seen)} tracked)")
        return 0

    fresh.sort(key=lambda j: j["title"])
    lines = [f"{len(fresh)} new matching role(s):", ""]
    for j in fresh:
        lines.append(f"- {j['title']}")
        bits = [b for b in (j["company"], j["loc"], j["salary"]) if b]
        if bits:
            lines.append(f"  {' | '.join(bits)}")
        lines.append(f"  {j['url']}")
        lines.append("")

    lines += ["", "—", "job-watch: automated digest, no reply."]
    send_digest(f"Job watch — {len(fresh)} new ({stamp})", lines)
    print(f"{len(fresh)} new roles, digest sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
