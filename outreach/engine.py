"""Planning and sending: window, caps + warmup, round-robin, bounce gate, pauses, dedup.

The same Allocator drives the dry-run plan and the real send loop, so the plan printed by
--dry-run is what --send would do (minus live events: bounces, mailbox errors).
"""
from __future__ import annotations

import random
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import company_stop
from .config import Mailbox, OutreachConfig
from .journal import Journal, MailboxStats, View, bounce_rate
from .leads import Lead
from .templates import Template, TemplateError, pick_template
from .transport import MailboxError, RecipientRejected, Transport, build_message


# --------------------------------------------------------------------------- clock

class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        _time.sleep(seconds)


class SimClock:
    """Fixed start time; sleep() advances virtual time instantly. Tests and offline demos."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("SimClock needs an aware datetime")
        self.t = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


# --------------------------------------------------------------------------- window

def local(cfg: OutreachConfig, now: datetime) -> datetime:
    return now.astimezone(cfg.tz)


def local_day(cfg: OutreachConfig, now: datetime) -> date:
    return local(cfg, now).date()


def in_window(cfg: OutreachConfig, now: datetime) -> bool:
    t = local(cfg, now)
    wd, tod = t.weekday(), t.time().replace(tzinfo=None)
    if cfg.window_from < cfg.window_to:
        return wd in cfg.days and cfg.window_from <= tod < cfg.window_to
    # overnight window (e.g. 22:00-02:00): the part after midnight belongs to the previous day
    return (wd in cfg.days and tod >= cfg.window_from) or ((wd - 1) % 7 in cfg.days and tod < cfg.window_to)


def _at(cfg: OutreachConfig, d: date, t) -> datetime:
    return datetime.combine(d, t, tzinfo=cfg.tz)


def window_bounds(cfg: OutreachConfig, now: datetime) -> tuple[datetime, datetime]:
    """(open, close) of the current window, or of the next one if closed now."""
    today = local_day(cfg, now)
    for offset in range(-1, 9):
        d = today + timedelta(days=offset)
        if d.weekday() not in cfg.days:
            continue
        start = _at(cfg, d, cfg.window_from)
        end_day = d if cfg.window_from < cfg.window_to else d + timedelta(days=1)
        end = _at(cfg, end_day, cfg.window_to)
        if end > now:
            return start, end
    raise ValueError("send window never opens (check KIT_OUTREACH_WINDOW_DAYS)")


# --------------------------------------------------------------------------- caps / gate

def cap_for(cfg: OutreachConfig, mb: Mailbox, st: MailboxStats, today: date) -> int:
    if cfg.warmup is None:
        return cfg.daily_cap
    first = mb.warmup_from or (date.fromisoformat(st.first_day) if st.first_day else today)
    return cfg.warmup.cap(cfg.daily_cap, (today - first).days)


def gate_tripped(cfg: OutreachConfig, st: MailboxStats, now: datetime) -> str | None:
    bounces, sends = bounce_rate(st, now, cfg.bounce_window_days)
    if sends < cfg.bounce_min_sent or sends == 0:
        return None
    rate = 100.0 * bounces / sends
    if rate > cfg.bounce_gate_percent:
        return (f"bounce rate {rate:.1f}% ({bounces}/{sends} in {cfg.bounce_window_days}d) "
                f"> gate {cfg.bounce_gate_percent:g}%")
    return None


@dataclass
class MailboxState:
    mailbox: Mailbox
    cap: int
    used: int
    stop_reason: str | None = None     # paused by gate / disabled for this run
    disabled: bool = False             # outreach not allowed (pack or ops bot): never a bounce-gate pause

    @property
    def remaining(self) -> int:
        return 0 if self.stop_reason else max(self.cap - self.used, 0)


class Allocator:
    """Round-robin over mailboxes with capacity left today (or fill-in-order if disabled)."""

    def __init__(self, cfg: OutreachConfig, view: View, now: datetime):
        today = local_day(cfg, now)
        self.cfg = cfg
        self.day = today
        self.states: list[MailboxState] = []
        switched_off = cfg.switched_off()
        for mb in cfg.mailboxes:
            st = view.mb(mb.id)
            ms = MailboxState(mb, cap_for(cfg, mb, st, today), st.by_day.get(today.isoformat(), 0))
            if not mb.outreach:
                ms.stop_reason, ms.disabled = "outreach disabled for this mailbox in the pack", True
            elif mb.id in switched_off:
                ms.stop_reason, ms.disabled = "outreach switched off in the ops bot", True
            elif st.paused:
                ms.stop_reason = f"paused: {st.paused.get('reason', '')}"
            else:
                ms.stop_reason = gate_tripped(cfg, st, now)
            self.states.append(ms)
        ids = [s.mailbox.id for s in self.states]
        self.cursor = (ids.index(view.last_mailbox) + 1) % len(ids) if view.last_mailbox in ids else 0

    def next(self) -> MailboxState | None:
        n = len(self.states)
        start = self.cursor if self.cfg.round_robin else 0
        for i in range(n):
            ms = self.states[(start + i) % n]
            if ms.remaining > 0:
                if self.cfg.round_robin:
                    self.cursor = (start + i + 1) % n
                return ms
        return None

    def get(self, mailbox_id: str) -> MailboxState:
        return next(s for s in self.states if s.mailbox.id == mailbox_id)


# --------------------------------------------------------------------------- filtering

@dataclass
class Skip:
    lead: Lead
    reason: str


def filter_leads(cfg: OutreachConfig, leads: list[Lead], templates: dict[str, Template], view: View,
                 suppression: set[str]) -> tuple[list[tuple[Lead, Template]], list[Skip]]:
    ok: list[tuple[Lead, Template]] = []
    skips: list[Skip] = []
    seen: set[str] = set()
    stopped = company_stop.snapshot(cfg.deployment_home)
    for lead in leads:
        if not lead.valid_email:
            skips.append(Skip(lead, f"invalid email {lead.raw_email!r}"))
        elif lead.email in seen:
            skips.append(Skip(lead, "duplicate in source"))
        elif company_stop.blocks_email(stopped, lead.email):
            skips.append(Skip(lead, "company opt-out"))
        elif lead.email in suppression:
            skips.append(Skip(lead, "suppressed"))
        elif lead.email in view.bounced:
            skips.append(Skip(lead, "bounced earlier"))
        elif lead.email in view.uncertain:
            skips.append(Skip(lead, f"uncertain earlier send via {view.contacted.get(lead.email)} (not retried)"))
        elif lead.email in view.contacted:
            skips.append(Skip(lead, f"already contacted via {view.contacted[lead.email]}"))
        else:
            try:
                tpl = pick_template(templates, lead.email, lead.template)
                tpl.render({"first_name": lead.first_name, "company": lead.company, "sender_name": "x"})
            except TemplateError as e:
                skips.append(Skip(lead, str(e)))
            else:
                ok.append((lead, tpl))
        if lead.valid_email:
            seen.add(lead.email)
    return ok, skips


def render(tpl: Template, lead: Lead, mb: Mailbox) -> tuple[str, str]:
    return tpl.render({"first_name": lead.first_name, "company": lead.company, "sender_name": mb.sender_name})


# --------------------------------------------------------------------------- plan (dry-run)

@dataclass
class PlannedSend:
    lead: Lead
    mailbox: Mailbox
    template: str
    subject: str
    body: str
    eta: datetime


@dataclass
class Plan:
    now: datetime
    window_open: bool
    opens_at: datetime
    closes_at: datetime
    sends: list[PlannedSend] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    deferred: list[tuple[Lead, str]] = field(default_factory=list)
    mailboxes: list[MailboxState] = field(default_factory=list)


def build_plan(cfg: OutreachConfig, leads: list[Lead], templates: dict[str, Template], view: View,
               suppression: set[str], now: datetime) -> Plan:
    opens, closes = window_bounds(cfg, now)
    start = now if in_window(cfg, now) else opens
    plan = Plan(now=now, window_open=in_window(cfg, now), opens_at=opens, closes_at=closes)
    ok, plan.skips = filter_leads(cfg, leads, templates, view, suppression)
    alloc = Allocator(cfg, view, start)
    plan.mailboxes = alloc.states
    t = start
    mean_pause = (cfg.pause_min + cfg.pause_max) / 2
    for lead, tpl in ok:
        if cfg.max_per_run and len(plan.sends) >= cfg.max_per_run:
            plan.deferred.append((lead, "max per run reached"))
            continue
        if t >= closes:
            plan.deferred.append((lead, "window closes before this send"))
            continue
        ms = alloc.next()
        if ms is None:
            usable = any(not s.disabled for s in alloc.states)
            plan.deferred.append((lead, "no mailbox capacity today" if usable else "no mailbox allowed for outreach"))
            continue
        ms.used += 1
        subject, body = render(tpl, lead, ms.mailbox)
        plan.sends.append(PlannedSend(lead, ms.mailbox, tpl.id, subject, body, t))
        t += timedelta(seconds=mean_pause)
    return plan


# --------------------------------------------------------------------------- send loop

@dataclass
class RunResult:
    sent: int = 0
    bounced: int = 0
    failed: int = 0
    uncertain: int = 0
    stop_reason: str = ""
    events: list[str] = field(default_factory=list)
    all_gated: bool = False


def run(cfg: OutreachConfig, leads: list[Lead], templates: dict[str, Template], journal: Journal,
        suppression: set[str], transport: Transport, clock, rng: random.Random | None = None,
        log: Callable[[str], None] = print) -> RunResult:
    """Real send loop. Caller holds the journal lock."""
    rng = rng or random.Random()
    res = RunResult()

    def note(msg: str) -> None:
        res.events.append(msg)
        log(msg)

    now = clock.now()
    if not in_window(cfg, now):
        opens, _ = window_bounds(cfg, now)
        res.stop_reason = f"outside send window; next opens {opens.isoformat()}"
        note(res.stop_reason)
        return res

    queue, skips = filter_leads(cfg, leads, templates, journal.view, suppression)
    for s in skips:
        log(f"skip row {s.lead.row} {s.lead.email or s.lead.raw_email!r}: {s.reason}")
    alloc = Allocator(cfg, journal.view, now)
    for ms in alloc.states:
        if ms.stop_reason and not ms.disabled and not journal.view.mb(ms.mailbox.id).paused:
            _pause(journal, ms, ms.stop_reason, now, note)
    queue = list(queue)
    attempts = 0
    while queue:
        if cfg.max_per_run and attempts >= cfg.max_per_run:
            res.stop_reason = "max per run reached"
            break
        now = clock.now()
        if not in_window(cfg, now):
            res.stop_reason = "send window closed"
            break
        day = local_day(cfg, now)
        if day != alloc.day:   # crossed midnight inside the window: fresh caps, keep run-level errors
            errors = {s.mailbox.id: s.stop_reason for s in alloc.states if (s.stop_reason or "").startswith("error")}
            alloc = Allocator(cfg, journal.view, now)
            for s in alloc.states:
                s.stop_reason = errors.get(s.mailbox.id, s.stop_reason)
        ms = alloc.next()
        if ms is None:
            usable = [s for s in alloc.states if not s.disabled]
            res.all_gated = bool(usable) and all(s.stop_reason and s.stop_reason.startswith(("paused", "bounce"))
                                                 for s in usable)
            res.stop_reason = ("all mailboxes paused by bounce gate" if res.all_gated else
                               "no mailbox allowed for outreach" if not usable else "no mailbox capacity today")
            break
        lead, tpl = queue.pop(0)
        if company_stop.blocks_email(company_stop.snapshot(cfg.deployment_home), lead.email):
            note(f"skip row {lead.row} {lead.email}: company opt-out")
            continue
        mb = ms.mailbox
        subject, body = render(tpl, lead, mb)
        msg = build_message(mb, lead.email, subject, body, now)
        base = {"mailbox": mb.id, "email": lead.email, "message_id": msg["Message-ID"]}
        journal.append({"event": "attempt", "ts": now.isoformat(), "day": day.isoformat(),
                        "template": tpl.id, "row": lead.row, **base})
        ms.used += 1
        attempts += 1
        try:
            transport.send(mb, msg)
        except RecipientRejected as e:
            journal.append({"event": "bounce", "ts": clock.now().isoformat(), "source": "smtp",
                            "detail": str(e), **base})
            res.bounced += 1
            note(f"bounce {lead.email} via {mb.id}: {e}")
        except MailboxError as e:
            ms.stop_reason = f"error: {e}"
            if e.retryable:
                journal.append({"event": "failed", "ts": clock.now().isoformat(), "error": str(e), **base})
                ms.used -= 1
                queue.insert(0, (lead, tpl))   # another mailbox may take it
                res.failed += 1
            else:
                journal.append({"event": "error", "ts": clock.now().isoformat(), "error": str(e), **base})
                res.uncertain += 1
            note(f"mailbox {mb.id} disabled for this run: {e}")
            continue
        else:
            journal.append({"event": "sent", "ts": clock.now().isoformat(), **base})
            res.sent += 1
            log(f"sent {lead.email} via {mb.id} ({tpl.id})")
        reason = gate_tripped(cfg, journal.view.mb(mb.id), clock.now())
        if reason:
            ms.stop_reason = reason
            _pause(journal, ms, reason, clock.now(), note)
        if queue:
            clock.sleep(rng.uniform(cfg.pause_min, cfg.pause_max))
    else:
        res.stop_reason = "queue empty"
    journal.write_snapshot({"last_run": clock.now().isoformat(), "last_stop": res.stop_reason})
    return res


def _pause(journal: Journal, ms: MailboxState, reason: str, now: datetime, note) -> None:
    journal.append({"event": "paused", "ts": now.isoformat(), "mailbox": ms.mailbox.id, "reason": reason})
    note(f"mailbox {ms.mailbox.id} PAUSED: {reason} (resume: `cli.py resume {ms.mailbox.id}`)")


def record_bounce(journal: Journal, email: str, now: datetime, source: str = "manual",
                  mailbox: str | None = None) -> str:
    """Entry point for async bounces (DSN reader in the mail module, operator). Returns status."""
    from .leads import normalize_email
    email = normalize_email(email)
    if email in journal.view.bounced:
        return "already recorded"
    mailbox = mailbox or journal.view.contacted.get(email)
    journal.append({"event": "bounce", "ts": now.isoformat(), "email": email, "mailbox": mailbox,
                    "source": source})
    return f"recorded (mailbox {mailbox or 'unknown'})"
