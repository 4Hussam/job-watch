#!/usr/bin/env python3
"""
job-watch — polls remote-job boards, filters to Hussam's stack, emails a digest.

Runs on GitHub Actions (cron), so it keeps working when his laptop is off.
Pure stdlib on purpose: no pip install, no requirements.txt to rot.
"""

import json
import os
import re
import imaplib
import smtplib
import ssl
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.header import decode_header, make_header
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

# HARD GATE. "Software Engineer" alone matches WANT for every backend role in
# the world, which is how Go/Kubernetes jobs kept sneaking in through the tags.
# The title itself has to name something he actually builds with.
TITLE_MUST = re.compile(
    r"\b(full[\s-]?stack|front[\s-]?end|frontend|back[\s-]?end|backend|"
    r"typescript|javascript|node\.?js|nodejs|astro|react(?!\s*native)|"
    r"vue|angular|svelte|next\.?js|nuxt|"
    r"postgres(?:ql)?|mysql|firebase|firestore|supabase|"
    r"rest\s*api|api\s*(?:developer|engineer|integrat)|"
    r"web\s*(?:app|develop|design)|software\s*develop|"
    r"mern|mean|lamp|jamstack|ui\s*engineer)\b",
    re.I,
)

# Stacks that keep arriving via the tag list. Not a fit, so they are removed
# outright rather than merely scored down.
OFFSTACK = re.compile(
    r"\b(golang|go\s*developer|kubernetes|k8s|terraform|ansible|"
    r"devops|sre|site reliability|aws|azure|gcp|"
    r"java\b|spring|python|django|flask|fastapi|"
    r"swift|objective[\s-]?c|objective-c|kotlin|"
    r"c\+\+|cpp|\.net|c#|rust|elixir|erlang|haskell|solidity|web3|"
    r"angular|vue\.?js|machine learning|data scientist|data engineering|"
    r"android|ios\b|unity|unreal|embedded|firmware|"
    r"salesforce|sharepoint|sap\b|qa\b|test engineer|"
    r"data analyst|business intelligence|tableau|power\s*bi)\b",
    re.I,
)

# Titles that say nothing about the stack, so the body has to be checked.
GENERIC_TITLE = re.compile(
    r"\b(software|product|design|application|web)\s+(engineer|developer)\b",
    re.I,
)

# Evidence in the body that the role is web work he can do.
STACK_BODY = re.compile(
    r"(typescript|javascript|node\.?js|nodejs|astro|react|vue|next\.?js|"
    r"nuxt|svelte|postgres(?:ql)?|mysql|firebase|firestore|supabase|"
    r"rest\s*api|tailwind|javascript|html|css|front[\s-]?end|back[\s-]?end)",
    re.I,
)

# If the body leans on these, it is not the web work he is after.
NO_WEB = re.compile(
    r"(microservices? on (?:go|golang)|service mesh|operator|"
    r"distributed (?:compute|systems|training)|high[\s-]per?formance computing|"
    r"hpc\b|cuda|driver development|kernel|firmware|"
    r"real[\s-]time systems|control systems|robotics)",
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
    r"security|analyst|developer advocate|forward deployed|"
    r"\biam\b|identity|access management|compliance|privacy|infosec|"
    r"trainee|trainers?|intern|presales|solutions architect|"
    # German/Austrian/Swiss local postings. "(m/w/d)" is a giveaway on its own,
    # and Werkstudent/Praktikant roles require enrolment at a local university,
    # which he cannot have. These leak past the city list because the city
    # appears only in the feed's URL slug, not in any structured field.
    r"working student|werkstudent\w*|praktikant\w*|ausbildung|duales studium|"
    r"projektmanagement|"
    r"softwareentwickler|entwickler|entwicklerin|büro|"
    r"développeur|développeuse|desarrollador|desarrolladora|"
    r"sviluppatore|ontwikkelaar|programador|programadora)\b",
    re.I,
)

# He is honest about being mid-level. Filter out the roles he would be
# filtered out of, so his limited attention goes where he can actually win.
# Its own regex on purpose. BLOCK ends with a \b, which can only be satisfied by
# an alternative that finishes on a word character, so a marker ending in ")"
# placed inside that alternation silently never matches. This stays separate.
GERMAN_MARKER = re.compile(
    r"\(\s*(?:m|w|f)\s*/\s*(?:w|m|f)\s*/\s*[dx]\b"   # (m/w/d) (m/w/x) (w/m/d)
    r"|\bhybrid\s+(?:role|modell|model)\b"
    r"|\bvor\s+ort\b|\bam\s+standort\b",             # German boilerplate
    re.I,
)

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
    r"grünwald|gruenwald|grunwald|stuttgart|köln|koeln|cologne|dresden|leipzig|"
    r"bremen|erfurt|potsdam|rostock|kiel|mainz|wiesbaden|ulm|reutlingen|esslingen|"
    r"münchen|muenchen|munchen|munich|garbsen|niedersachsen|"
    r"deutschland|germany|allemagne|españa|espana|italia|"
    r"lille|lyon|marseille|toulouse|rotterdam|utrecht|eindhoven|"
    r"brno|krakow|kraków|wroclaw|wrocław|gdansk|gdańsk|"
    # APAC: hiring there means a local work right he does not have
    r"singapore|malaysia|kuala lumpur|hong kong|thailand|bangkok|vietnam|"
    r"philippines|manila|indonesia|jakarta|taiwan|taipei|india|mumbai|"
    r"bengaluru|bangalore|hyderabad|delhi|chennai|pakistan|karachi|lahore|"
    r"dubai|uae|abu dhabi|qatar|saudi|riyadh|kuwait|bahrain|oman|israel|"
    r"tel aviv|jerusalem|haifa|"
    # Region words: "Remote - Europe" excludes him as surely as a country name
    r"europe|emea|americas?|north america|latam|latin america|"
    r"apac|asia pacific|asia-pacific|anz)\b",
    re.I,
)

# Titles often carry the country as a suffix ("Frontend Engineer | Singapore")
# while the structured location field stays empty. Check those too.
GEO_TITLE = re.compile(r"\|\s*[A-Z][A-Za-z .]{2,30}\s*$|\(([A-Z][A-Za-z .]{2,30})\)",
                       re.M)

# ...unless the posting explicitly says it is open worldwide.
GEO_OK = re.compile(r"\b(anywhere|worldwide|anywhere in the world|global|"
                    r"no location restriction|location[ -]?independent)\b", re.I)

BOARDS = [
    ("RemoteOK",  "https://remoteok.com/api"),
    ("Remotive",  "https://remotive.com/api/remote-jobs"),
    ("Arbeitnow", "https://www.arbeitnow.com/api/job-board-api"),
    ("Jobicy",    "https://jobicy.com/api/v2/remote-jobs"),
    ("WWR",       "https://weworkremotely.com/remote-jobs.rss"),
]

# Feed shapes, so one scorer can be reused for JSON and RSS alike.
RSS_BOARDS = {"WWR"}


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
    title = job.get("title") or job.get("position") or ""
    if BLOCK.search(title):
        return 0
    if GERMAN_MARKER.search(title):
        return 0
    if TOO_SENIOR.search(title):
        return 0
    # A stack he does not have named in the title itself is a dead end, even
    # when the title also advertises one he does have ("Web3 (Rust/TypeScript)").
    if OFFSTACK.search(title):
        return 0
    # The stack must be visible in the title, or proven by the body if the
    # title is a generic "Software Engineer".
    if not TITLE_MUST.search(title):
        if not GENERIC_TITLE.search(title):
            return 0
        # Generic title: the description has to name his stack, and must not
        # name a stack he does not have.
        if OFFSTACK.search(t):
            return 0
        if not (STACK_BODY.search(t) and not NO_WEB.search(t)):
            return 0
    # ...and the tag list must not say it is a stack he does not have.
    tags = job.get("tags")
    if tags is None:
        tags = job.get("categories") or []
    if isinstance(tags, str):
        tags = [tags]
    if OFFSTACK.search(" ".join(str(x) for x in tags)):
        return 0

    # Only judge location on the explicit field, not free-text noise.
    loc = " ".join(str(job.get(k) or "") for k in
                   ("candidate_required_location", "location", "job_type",
                    "region", "country"))
    if loc.strip() and GEO_BLOCK.search(loc) and not GEO_OK.search(loc):
        return 0

    # No structured location at all is its own signal: board feeds routinely
    # put the town in the link slug and nowhere else, so the check above has
    # nothing to look at. Only trust the body when the structured field is
    # missing -- if it says "Remote", that answer wins.
    if not loc.strip() and not GEO_OK.search(t) and GEO_BLOCK.search(t):
        return 0

    # A country in the title ("Frontend Engineer | Singapore") is geo-locked
    # even when the structured location field is empty.
    if not GEO_OK.search(t):
        suffix = GEO_TITLE.search(title)
        if suffix and GEO_BLOCK.search(suffix.group(0)):
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


def pull_json(board, url):
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


def pull_rss(board, url):
    """WeWorkRemotely and friends: RSS, not JSON. Map <item> onto the same dicts."""
    out, err = [], None
    try:
        raw = fetch(url)
    except Exception as e:
        return out, f"{board}: {type(e).__name__}"
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as e:
        return out, f"{board}: bad xml ({e})"

    for item in root.iter("item"):
        def tag(name):
            el = item.find(name)
            return (el.text or "").strip() if el is not None and el.text else ""

        title = tag("title")
        # WWR packs "Company: Position" into <title>.
        company, _, position = title.partition(":")
        if not position:
            company, position = "", title
        cats = [c.text.strip() for c in item.findall("category") if c.text]
        job = {
            "title": position.strip() or title,
            "company": company.strip(),
            "description": tag("description"),
            "url": tag("link"),
            # WWR puts region in <region> or in the category list.
            "candidate_required_location": tag("region") or " ".join(cats),
            "date_published": tag("pubDate"),
        }
        if TOO_SENIOR.search(job["title"]):
            continue
        if relevance(job) >= 3:
            out.append(norm(job, board))
    return out, err


def pull(board, url):
    return pull_rss(board, url) if board in RSS_BOARDS else pull_json(board, url)


# Only real recruiter / employer / application traffic is worth his attention.
# Automated mail is filtered out regardless of subject, or the digest fills up
# with security notices and newsletters.
MAILWATCH = re.compile(
    r"(recruit|recruiter|recruitment|talent acquisition|talent partner|"
    r"hiring manager|head of (?:engineering|development|it)|"
    r"careers?\b|career[s]?@|job[s]?@|hr@|people@|"
    r"interview|your application|application (?:received|status|update)|"
    r"we(?:'ve| have) (?:received|reviewed)|"
    r"offer (?:letter|extended)|next steps|assessment|"
    r"foras\.ps|tapcareers|gazatalents|skillbridge|forlanso)",
    re.I,
)

MAILIGNORE = re.compile(
    r"(no-?reply|noreply|mailer-daemon|donotreply|notifications@|"
    r"accounts\.google|github\.com|linkedin|dub\.ai|newsletter|"
    r"unsubscribe|marketing|promotion|sales@|billing|security alert|"
    r"calendar-invitation|facebook|instagram|twitter|x\.com)",
    re.I,
)


def inbox_new():
    """Unread inbox headers worth showing. Never auto-replies, never marks read."""
    if not GMAIL_PASS:
        return []
    out = []
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as M:
            M.login(GMAIL_USER, GMAIL_PASS)
            M.select("INBOX")
            typ, data = M.search(None, "UNSEEN")
            if typ != "OK":
                return []
            for num in reversed(data[0].split()[-40:]):
                typ, msg = M.fetch(
                    num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if typ != "OK":
                    continue
                head = msg[0][1].decode("utf-8", "replace")
                frm = re.search(r"^From:\s*(.+)$", head, re.M)
                sub = re.search(r"^Subject:\s*(.+)$", head, re.M)
                date = re.search(r"^Date:\s*(.+)$", head, re.M)
                sender = frm.group(1).strip() if frm else "?"
                subject = sub.group(1).strip() if sub else "(no subject)"
                try:
                    subject = str(make_header(decode_header(subject)))
                except Exception:
                    pass
                if "hrs18jan" in sender or MAILIGNORE.search(sender):
                    continue
                out.append({
                    "from": sender[:70],
                    "subject": subject[:90],
                    "date": (date.group(1).strip() if date else "")[:40],
                })
    except Exception as e:
        print(f"inbox check failed: {type(e).__name__}")
        return []
    return out


def send_digest(subject, lines, dry=False):
    if dry:
        print("--- DRY RUN, NOT SENT ---")
        print("Subject:", subject)
        print("\n".join(lines))
        return False
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
    # --dry-run prints the digest instead of mailing it, and does not touch
    # the seen-state, so testing the filters never consumes a real cycle.
    dry = "--dry-run" in sys.argv

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

    if dry:
        print("DRY RUN — nothing will be sent, seen-state untouched")
    else:
        save_seen(seen)

    now = datetime.now(timezone.utc) + timedelta(hours=3)
    stamp = now.strftime("%Y-%m-%d %H:%M")

    if problems:
        print("board issues: " + " | ".join(problems))

    inbox = [m for m in inbox_new()
             if MAILWATCH.search(m["from"] + " " + m["subject"])]

    def mailblock():
        if not inbox:
            return []
        b = ["", "=" * 46,
             f"INBOX — {len(inbox)} unread that look like real replies:", ""]
        for m in inbox:
            b.append(f"- {m['subject']}")
            b.append(f"  from: {m['from']}")
            b.append(f"  {m['date']}")
            b.append("")
        b.append("Open in Gmail, or tell me here and I will draft the reply.")
        return b

    if not fresh:
        send_digest(f"Job watch — nothing new ({stamp})",
                    ["No new matching roles this cycle.", "",
                     f"Tracked {len(seen)} roles so far."] + mailblock(), dry)
        print(f"no new roles ({len(seen)} tracked), {len(inbox)} inbox match(es)")
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

    lines += mailblock()
    lines += ["", "—", "job-watch: automated digest, no reply."]
    send_digest(f"Job watch — {len(fresh)} new ({stamp})", lines, dry)
    print(f"{len(fresh)} new roles, {len(inbox)} inbox match(es), "
          + ("printed only" if dry else "digest sent"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
