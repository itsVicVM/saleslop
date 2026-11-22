"""Pure helpers: readable body of an email, "quiet" mail that never becomes a card, failed recipients of a
delivery status notification (RFC 3464), tracker lookup by address/domain."""
from __future__ import annotations

import html
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr

BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery (sub)?system|delivery status", re.I)
BOUNCE_SUBJ = re.compile(r"mail delivery failed|undeliver|delivery status notification|returned mail|failure notice|"
                         r"delivery has failed|не доставлен|недоставлен|сообщение не доставлено", re.I)
AUTO_SUBJ = re.compile(r"^\s*(automatic reply|auto[- ]?reply|autoreply|out of (the )?office|auto:|автоответ|"
                       r"автоматический ответ|abwesenheit|réponse automatique)", re.I)
NOREPLY = re.compile(r"(^|[._-])(no[-_.]?reply|donotreply|do[-_.]not[-_.]reply|mailer|notifications?|bounce|newsletter|news)@",
                     re.I)
# a quoted earlier message starts here: everything below is history, not the new text
QUOTE_START = re.compile(r"^\s*(On .{5,120} wrote:|.{0,80}(написал|пишет)\(?а?\)?:\s*$|-{2,}\s*(Original Message|Исходное сообщение))",
                         re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def parse_message(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", " ", h))


def body_text(msg: EmailMessage) -> str:
    """The new text of the message: plain part (or HTML stripped), without the quoted history."""
    txt = ""
    for kind in ("plain", "html"):
        part = msg.get_body(preferencelist=(kind,))
        if part is None:
            continue
        try:
            txt = part.get_content()
        except (LookupError, ValueError):
            txt = ""
        if txt and kind == "html":
            txt = _html_to_text(txt)
        if txt:
            break
    lines = []
    for ln in txt.replace("\r", "").split("\n"):
        if QUOTE_START.match(ln):
            break
        if ln.lstrip().startswith(">"):
            continue
        lines.append(ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def classify_quiet(msg: EmailMessage, own_addrs: set) -> str:
    """'' for mail that deserves a card, else why it stays in the log only: own | bounce | autoreply | newsletter | noreply."""
    frm = parseaddr(str(msg.get("From", "")))[1].lower()
    subj = str(msg.get("Subject", "") or "")
    if frm in own_addrs:
        return "own"
    ctype = str(msg.get("Content-Type", "")).lower()
    if BOUNCE_FROM.search(str(msg.get("From", ""))) or BOUNCE_SUBJ.search(subj) or ctype.startswith("multipart/report"):
        return "bounce"
    auto = str(msg.get("Auto-Submitted", "")).lower()
    if (auto and auto != "no") or str(msg.get("Precedence", "")).lower() in ("bulk", "auto_reply", "junk", "list") \
            or msg.get("X-Autoreply") or msg.get("X-Autorespond") or msg.get("X-Auto-Response-Suppress") or AUTO_SUBJ.search(subj):
        return "autoreply"
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return "newsletter"
    if NOREPLY.search(frm) or frm.split("@")[0] in ("noreply", "no-reply", "donotreply"):
        return "noreply"
    return ""


def _status_fields(text: str) -> list[dict]:
    """Per-recipient field groups of a message/delivery-status body (groups are separated by blank lines)."""
    groups, cur = [], {}
    for line in text.replace("\r", "").split("\n"):
        if not line.strip():
            if cur:
                groups.append(cur)
                cur = {}
            continue
        k, sep, v = line.partition(":")
        if sep and not line[0].isspace():
            cur[k.strip().lower()] = v.strip()
    if cur:
        groups.append(cur)
    return groups


def _addr(value: str) -> str:
    # "rfc822; lead@x.example" -> lead@x.example
    m = EMAIL_RE.search(value.split(";", 1)[-1] if ";" in value else value)
    return m.group(0).lower() if m else ""


def dsn_failed_recipients(msg: EmailMessage) -> list[str]:
    """Recipients a DSN reports as permanently failed (Action: failed, or Status 5.x.x).
    Delayed (4.x.x) and relayed/delivered notices return nothing. Non-RFC bounces: the original recipient from
    an attached message/rfc822 part or an X-Failed-Recipients header, only when the subject says it failed."""
    out: list[str] = []
    for part in msg.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        payload = part.get_payload()
        if isinstance(payload, list):   # the stdlib parses the status groups into sub-messages
            text = "\n\n".join(str(p) for p in payload)
        else:
            text = payload if isinstance(payload, str) else ""
        for g in _status_fields(text):
            rcpt = _addr(g.get("final-recipient") or g.get("original-recipient") or "")
            action = g.get("action", "").lower()
            status = g.get("status", "")
            if rcpt and (action == "failed" or status.startswith("5")):
                out.append(rcpt)
    if out:
        return sorted(set(out))
    for h in msg.get_all("X-Failed-Recipients") or []:
        out += [a.lower() for a in EMAIL_RE.findall(str(h))]
    if not out and BOUNCE_SUBJ.search(str(msg.get("Subject", ""))):
        for part in msg.walk():
            if part.get_content_type() == "message/rfc822":
                inner = part.get_payload()
                inner = inner[0] if isinstance(inner, list) and inner else inner
                to = parseaddr(str(getattr(inner, "get", lambda *_: "")("To", "")))[1].lower()
                if to:
                    out.append(to)
    return sorted(set(out))


def _site_domain(site: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (site or "").strip().lower()).split("/")[0]


def tracker_match(addr: str, rows: list) -> dict | None:
    """rows: dicts {email, site, company, stage}. Exact address match wins; else the first row whose site domain
    equals the sender's domain (marked exact=False). Free-mail domains never match by domain."""
    addr = (addr or "").lower()
    dom = addr.split("@")[-1]
    by_domain = None
    for r in rows or []:
        emails = [x.strip().lower() for x in re.split(r"[,\s;]+", r.get("email") or "") if "@" in x]
        if addr in emails:
            return {**{k: r.get(k, "") for k in ("company", "stage", "site")}, "exact": True}
        if by_domain is None and dom and dom not in FREE_MAIL and _site_domain(r.get("site", "")) == dom:
            by_domain = {**{k: r.get(k, "") for k in ("company", "stage", "site")}, "exact": False}
    return by_domain


FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "mail.ru",
             "yandex.ru", "proton.me", "protonmail.com", "gmx.de", "web.de"}
