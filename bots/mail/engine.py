"""Mail cards in the ops chat: poll mailboxes -> filter -> card (optionally with a draft) -> operator buttons ->
send only on the Send button -> separate confirmation message.

Runs inside the ops bot process as an `Extension` of kit/bots/ops/core.Engine (one process polls the bot token).
All I/O is injected so tests drive it with fakes:
  llm          .complete(system, user, max_tokens) -> str | None        (same client as the ops bot)
  ops          OpsBot-like: .send/.edit/.delete/.answer
  himalaya     himalaya.Himalaya-like: list_ids/read_raw/send_raw
  gmail        gmail.Gmail-like: inbox_ids/read_raw/send_raw (+ OAuth helpers for connecting)
  rows_provider() -> tracker rows [{email, site, company, stage}]
  bounce_sink(email, source) -> bool   (False = could not record now, retried on the next poll)
  outreach     object with .outreach_off("mail") / .set_outreach_off("mail", id, off)  (ops Store)
  smtp_probe(url) -> bool | None
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import socket
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from . import parse, prompts
from .config import MailConfig, Mailbox
from .state import MailState
from .ui import ui

SEND_CHECK_EVERY_S = 6 * 3600
AUTH_ALERT_EVERY_S = 12 * 3600
PROMPT_TTL_S = 24 * 3600
ADDR_RE = re.compile(r"^[a-z0-9._%+\-]+@([a-z0-9.\-]+\.[a-z]{2,})$")


def tcp_probe(url: str):
    """True/False: a TCP connection to the SMTP host:port in `smtp://host:port` works from this host. Some VPS
    providers block outbound SMTP: then the mailbox still gets cards, but replies are copied and sent by hand
    (or the mailbox moves to the Gmail API / the client's own relay)."""
    m = re.match(r"smtps?://([^:/]+):(\d+)", url or "")
    if not m:
        return None
    try:
        socket.create_connection((m.group(1), int(m.group(2))), timeout=6).close()
        return True
    except OSError:
        return False


def _sid(*parts) -> str:
    return hashlib.sha1(":".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]


class MailEngine:
    prefixes = ("mail",)

    def __init__(self, cfg: MailConfig, ops_cfg, *, llm, ops, himalaya=None, gmail=None, rows_provider=None,
                 bounce_sink=None, outreach=None, smtp_probe=None, clock=time.time, translate=True):
        self.cfg = cfg
        self.ops_cfg = ops_cfg
        self.llm = llm
        self.ops = ops
        self.himalaya = himalaya
        self.gmail = gmail
        self.graph_mailboxes = {}
        self.rows_provider = rows_provider
        self.bounce_sink = bounce_sink
        self.outreach = outreach
        self.smtp_probe = smtp_probe or tcp_probe
        self.clock = clock
        self.translate_on = translate
        self.state = MailState(cfg.state_dir)
        self.t = ui(ops_cfg.operator_language)
        self._lock = threading.RLock()
        self._tr_cache: dict = {}

    # ================================================================== mailboxes
    def mailboxes(self) -> list:
        boxes = list(self.cfg.mailboxes)
        known = {m.id for m in boxes}
        for d in self.state.load("connected.json", []):
            if d.get("id") not in known:
                boxes.append(Mailbox(**{k: v for k, v in d.items() if k in Mailbox.__dataclass_fields__}))
        return boxes

    def mailbox(self, mailbox_id: str):
        return next((m for m in self.mailboxes() if m.id == mailbox_id), None)

    def notify_on(self, mb: Mailbox) -> bool:
        return self.state.box(mb.id).get("notify", mb.notify)

    def reply_mode(self, mb: Mailbox) -> str:
        return self.state.box(mb.id).get("reply", mb.reply)

    def outreach_on(self, mb: Mailbox) -> bool:
        """The pack decides; the operator can only switch an allowed mailbox off (and back on)."""
        if not mb.outreach:
            return False
        return mb.id not in (self.outreach.outreach_off("mail") if self.outreach else set())

    def own_addresses(self) -> set:
        return {m.address.lower() for m in self.mailboxes()}

    # ================================================================== polling
    def poll_once(self) -> dict:
        """One pass over every mailbox. Returns counters (for logs and tests)."""
        stats = {"cards": 0, "quiet": 0, "bounces": 0, "errors": 0}
        own = self.own_addresses()
        self.flush_bounces()
        for mb in self.mailboxes():
            try:
                if mb.provider == "gmail_api":
                    self._poll_gmail(mb, own, stats)
                elif mb.provider == "ms_graph":
                    self._poll_graph(mb, own, stats)
                else:
                    self._refresh_send_status(mb)
                    self._poll_himalaya(mb, own, stats)
            except Exception as e:  # one broken mailbox must not stop the others
                stats["errors"] += 1
                print(f"[mail] poll {mb.id} failed: {e}", file=sys.stderr)
        return stats

    def _graph(self, mb):
        if mb.id not in self.graph_mailboxes:
            from .ms_graph import GraphMailbox
            self.graph_mailboxes[mb.id] = GraphMailbox(mb)
        return self.graph_mailboxes[mb.id]

    def _poll_graph(self, mb, own, stats):
        graph = self._graph(mb)
        box = self.state.box(mb.id)
        same_address = box.get("graph_address") == mb.address
        cursor = box.get("graph_cursor") if same_address else None
        ready = same_address and box.get("graph_ready", False)
        ids, following, more = graph.page(cursor)
        if ready:
            for mid in ids:
                raw = graph.raw(mid)
                if raw is not None:
                    self.process_raw(mb, mid, raw, own, stats)
        self.state.update_box(mb.id, graph_address=mb.address, graph_cursor=following, graph_ready=ready or not more)

    def _refresh_send_status(self, mb: Mailbox) -> None:
        box = self.state.box(mb.id)
        if self.clock() - box.get("send_checked", 0) < SEND_CHECK_EVERY_S and "send_ok" in box:
            return
        smtp = ""
        if self.himalaya is not None and mb.himalaya_account:
            smtp = (self.himalaya.config_accounts().get(mb.himalaya_account) or {}).get("smtp", "")
        self.state.update_box(mb.id, send_ok=self.smtp_probe(smtp) if smtp else None, send_checked=self.clock())

    def _poll_himalaya(self, mb: Mailbox, own: set, stats: dict) -> None:
        if self.himalaya is None or not mb.himalaya_account:
            return
        ids = self.himalaya.list_ids(mb.himalaya_account)
        if not ids:
            return
        last = self.state.box(mb.id).get("last_uid")
        if last is None:  # first run: baseline, nothing from the backlog becomes a card
            self.state.update_box(mb.id, last_uid=ids[-1])
            print(f"[mail] {mb.id}: baseline uid {ids[-1]}", file=sys.stderr)
            return
        for uid in [i for i in ids if i > last]:
            raw = self.himalaya.read_raw(mb.himalaya_account, uid)
            if raw is None:
                stats["errors"] += 1
                break
            self.process_raw(mb, uid, raw, own, stats)
            self.state.update_box(mb.id, last_uid=uid)

    def _poll_gmail(self, mb: Mailbox, own: set, stats: dict) -> None:
        if self.gmail is None:
            return
        from .gmail import AuthError
        box = self.state.box(mb.id)
        try:
            ids = self.gmail.inbox_ids(mb.id)
            if ids is None:
                return
            if box.get("seen_ids") is None:
                self.state.update_box(mb.id, seen_ids=ids[:200])
                print(f"[mail] {mb.id}: gmail baseline ({len(ids)} messages)", file=sys.stderr)
                return
            seen = list(box["seen_ids"])
            for mid in reversed([i for i in ids if i not in seen]):
                got = self.gmail.read_raw(mb.id, mid)
                if got is None:
                    stats["errors"] += 1
                    continue
                raw, thread = got
                self.process_raw(mb, mid, raw, own, stats, {"thread_id": thread, "gmail_id": mid})
                seen.insert(0, mid)
                self.state.update_box(mb.id, seen_ids=seen[:200])
        except AuthError as e:
            self._auth_lost(mb, e)

    def _auth_lost(self, mb: Mailbox, err) -> None:
        box = self.state.box(mb.id)
        if self.clock() - box.get("auth_alert", 0) > AUTH_ALERT_EVERY_S:
            self.state.update_box(mb.id, auth_alert=self.clock())
            self.ops.send(self.t["auth_lost"].format(addr=mb.address, err=str(err)[:80]))

    def process_raw(self, mb: Mailbox, uid, raw: bytes, own: set, stats: dict | None = None, extra: dict | None = None):
        stats = stats if stats is not None else {"cards": 0, "quiet": 0, "bounces": 0, "errors": 0}
        msg = parse.parse_message(raw)
        reason = parse.classify_quiet(msg, own)
        f_name, f_addr = parseaddr(str(msg.get("From", "")))
        _, r_addr = parseaddr(str(msg.get("Reply-To") or ""))
        subject = str(msg.get("Subject", "") or "") or "(no subject)"
        self.state.append("quiet.jsonl" if reason else "seen.jsonl",
                          {"mailbox": mb.id, "uid": uid, "from": f_addr, "subject": subject[:150], "reason": reason or "card"})
        if reason == "bounce":
            for rcpt in parse.dsn_failed_recipients(msg):
                stats["bounces"] += 1
                self._record_bounce(rcpt)
        if reason:
            stats["quiet"] += 1
            return None
        card = {"id": _sid(mb.id, uid, msg.get("Message-ID", "")), "mailbox": mb.id, "uid": uid,
                "from_name": f_name, "from_addr": f_addr.lower(), "reply_to": (r_addr or f_addr).lower(),
                "subject": subject, "body": parse.body_text(msg), "message_id": str(msg.get("Message-ID", "") or ""),
                "references": str(msg.get("References", "") or ""), "status": "new", "tracker": self.tracker_ctx(f_addr),
                "created": self.clock(), "draft": ""}
        card.update(extra or {})
        if self.state.load_card(card["id"]):
            return None  # already carded (cursor reset or duplicate delivery)
        if not self.notify_on(mb):
            return None
        if self.reply_mode(mb) == "draft":
            card["draft"] = self.compose(card)
            if card["draft"]:
                card["status"] = "drafted"
        self._new_card(card)
        stats["cards"] += 1
        return card

    # ================================================================== bounces -> kit/outreach
    def _record_bounce(self, email: str) -> None:
        self.state.append("bounce_queue.jsonl", {"email": email, "source": "dsn"})
        self.flush_bounces()

    def flush_bounces(self) -> int:
        """Write queued failed recipients to the outreach journal; the queue keeps what could not be written."""
        if self.bounce_sink is None:
            return 0
        with self._lock:
            queue = self.state.read_lines("bounce_queue.jsonl")
            if not queue:
                return 0
            left, done = [], 0
            for rec in queue:
                try:
                    ok = self.bounce_sink(rec["email"], rec.get("source", "dsn"))
                except Exception as e:
                    print(f"[mail] bounce sink failed: {e}", file=sys.stderr)
                    ok = False
                if ok:
                    done += 1
                else:
                    left.append(rec)
            p = self.state.dir / "bounce_queue.jsonl"
            p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in left), encoding="utf-8")
            return done

    # ================================================================== tracker
    def tracker_ctx(self, addr: str):
        if not self.rows_provider:
            return None
        try:
            return parse.tracker_match(addr, self.rows_provider())
        except Exception as e:
            print(f"[mail] tracker lookup failed: {e}", file=sys.stderr)
            return None

    # ================================================================== LLM
    def compose(self, card: dict, correction: str = "") -> str:
        out = self.llm.complete(prompts.compose_system(self.ops_cfg), prompts.compose_user(card, correction), max_tokens=1500)
        return (out or "").strip()

    def translate(self, text: str):
        from ..ops.textutil import needs_translation
        text = (text or "").strip()
        lang = self.ops_cfg.operator_language
        if not self.translate_on or not text or not needs_translation(text, lang):
            return None
        k = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if k not in self._tr_cache:
            res = self.llm.complete(prompts.translate_system(lang), text[:3000], max_tokens=1500)
            if not res:
                return None
            self._tr_cache[k] = res.strip()
            if len(self._tr_cache) > 400:
                self._tr_cache.pop(next(iter(self._tr_cache)))
        return self._tr_cache[k]

    # ================================================================== cards
    def can_send(self, mb) -> bool:
        if mb is None:
            return False
        if mb.provider in ("gmail_api", "ms_graph"):
            return True
        return self.state.box(mb.id).get("send_ok") is not False

    def card_text(self, card: dict) -> str:
        t = self.t
        mb = self.mailbox(card["mailbox"])
        if card.get("compose"):
            lines = [t["new_email"].format(box=card["mailbox"], to=card["reply_to"]), f"{t['subject']}: {card['subject'][:200]}"]
        else:
            who = f"{card['from_name']} <{card['from_addr']}>" if card.get("from_name") else card["from_addr"]
            tc = card.get("tracker")
            head = t["card_head"].format(box=card["mailbox"], who=who)
            head += (f" — {tc.get('company', '')}" + ("" if tc.get("exact") else f" ({t['by_domain']})")) if tc else f" — {t['not_in_tracker']}"
            lines = [head, f"{t['subject']}: {card['subject'][:200]}", "", (card.get("body") or "")[:900] or t["no_text"]]
            tr = self.translate((card.get("body") or "")[:900])
            if tr:
                lines += ["", f"({t['translation']}: {tr})"]
        if card.get("draft"):
            lines += ["", f"— {t['draft']} —", card["draft"]]
            tr = self.translate(card["draft"])
            if tr:
                lines += ["", f"({t['draft_tr']}: {tr})"]
            if not self.can_send(mb):
                lines += ["", t["no_send_note"]]
        st = card["status"]
        if st == "sent":
            lines += ["", t["st_sent"]]
        elif st == "skipped":
            lines += ["", t["st_skipped"]]
        elif st == "failed":
            lines += ["", t["st_failed"].format(err=card.get("error", ""))]
        elif st == "unknown":
            lines += ["", t["st_unknown"]]
        elif st == "sending":
            lines += ["", t["st_sending"]]
        return "\n".join(lines)[:3900]

    def card_buttons(self, card: dict):
        t, sid, st = self.t, card["id"], card["status"]
        if st in ("sent", "skipped", "sending"):
            return []
        if st == "unknown":  # no plain retry: the operator checks Sent first
            return [[{"text": t["b_was_sent"], "callback_data": f"mail:wassent:{sid}"}],
                    [{"text": t["b_resend"], "callback_data": f"mail:resend:{sid}"}]]
        if card.get("draft"):
            first = [{"text": t["b_edit"], "callback_data": f"mail:edit:{sid}"}]
            if self.can_send(self.mailbox(card["mailbox"])):
                first.insert(0, {"text": t["b_send"], "callback_data": f"mail:send:{sid}"})
            second = [{"text": t["b_own"], "callback_data": f"mail:own:{sid}"}]
            if not card.get("compose"):
                second.append({"text": t["b_redo"], "callback_data": f"mail:redo:{sid}"})
            second.append({"text": t["b_skip"], "callback_data": f"mail:skip:{sid}"})
            return [first, second]
        return [[{"text": t["b_draft"], "callback_data": f"mail:draft:{sid}"},
                 {"text": t["b_own"], "callback_data": f"mail:own:{sid}"},
                 {"text": t["b_skip"], "callback_data": f"mail:skip:{sid}"}]]

    def _new_card(self, card: dict) -> None:
        card["ops_msg"] = self.ops.send(self.card_text(card), buttons=self.card_buttons(card) or None)
        self.state.save_card(card)

    def _refresh_card(self, card: dict) -> None:
        self.state.save_card(card)
        if card.get("ops_msg"):
            self.ops.edit(card["ops_msg"], self.card_text(card), self.card_buttons(card))

    def _repost_card(self, card: dict) -> None:
        """After the operator typed something, the card goes below their message (old card removed)."""
        self.ops.delete(card.get("ops_msg"))
        self._new_card(card)

    # ================================================================== sending
    def build_mime(self, mb: Mailbox, card: dict) -> tuple[EmailMessage, str]:
        if card.get("compose") or re.match(r"(?i)\s*(re|ответ|aw|sv)\s*:", card["subject"]):
            subject = card["subject"]
        else:
            subject = "Re: " + card["subject"]
        m = EmailMessage()
        m["From"] = formataddr((mb.sender_name or "", mb.address))
        m["To"] = card["reply_to"]
        m["Subject"] = subject
        m["Date"] = formatdate(localtime=True)
        m["Message-ID"] = make_msgid(domain=mb.address.split("@")[-1])
        if card.get("message_id") and not card.get("compose"):
            m["In-Reply-To"] = card["message_id"]
            m["References"] = (card.get("references", "") + " " + card["message_id"]).strip()
        m.set_content(card.get("draft", ""))
        return m, subject

    def send_card(self, sid: str, force: bool = False) -> str:
        """The only path that sends an email. Returns the new card status."""
        with self._lock:
            card = self.state.load_card(sid)
            if not card:
                return "stale"
            if card["status"] in ("sent", "sending", "skipped") or (card["status"] == "unknown" and not force):
                return card["status"]
            mb = self.mailbox(card["mailbox"])
            if mb is None or not self.can_send(mb) or not card.get("draft"):
                return "no_send"
            msg, subject = self.build_mime(mb, card)
            raw = msg.as_bytes()
            card.update(status="sending", sent_message_id=str(msg["Message-ID"]), sent_subject=subject)
            self._refresh_card(card)
        try:
            if mb.provider == "gmail_api":
                outcome, detail = self.gmail.send_raw(mb.id, raw, card.get("thread_id", ""))
            elif mb.provider == "ms_graph":
                from email.policy import SMTP
                outcome, detail = self._graph(mb).send(msg.as_bytes(policy=SMTP))
            else:
                outcome, detail = self.himalaya.send_raw(mb.himalaya_account, raw)
        except Exception as e:  # anything after the bytes may have left: not a safe retry
            outcome, detail = "unknown", str(e)[:300]
        card.update(status=outcome, error=detail, sent_subject=subject, sent_message_id=msg["Message-ID"])
        if outcome in ("sent", "unknown"):
            self.state.append("sent.jsonl", {"mailbox": mb.id, "to": card["reply_to"], "subject": subject[:150],
                                             "card": sid, "outcome": outcome, "message_id": msg["Message-ID"]})
        if outcome == "sent":
            card["sent_at"] = self.clock()
        self._refresh_card(card)
        if outcome == "sent":
            self.ops.send(self.t["sent_confirm"].format(box=mb.id, to=card["reply_to"], subject=subject[:200]))
        return outcome

    def recover_sends(self):
        """The listener calls this once before accepting callbacks."""
        interrupted = [card for card in self.state.cards() if card.get("status") == "sending"]
        if not interrupted:
            return
        confirmed = {(row.get("card"), row.get("message_id")) for row in self.state.read_lines("sent.jsonl")
                     if row.get("outcome") == "sent"}
        for card in interrupted:
            key = (card["id"], card.get("sent_message_id"))
            card["status"] = "sent" if key[1] and key in confirmed else "unknown"
            self._refresh_card(card)

    # ================================================================== ops bot extension: callbacks
    def menu_rows(self) -> list:
        return [[{"text": self.t["menu_row"], "callback_data": "mail:nav:main"}]]

    async def handle_callback(self, cq: dict) -> str:
        return await asyncio.to_thread(self.callback, cq)

    def callback(self, cq: dict) -> str:
        t = self.t
        cq_id = cq.get("id")
        msg_id = (cq.get("message") or {}).get("message_id")
        parts = cq.get("data", "").split(":")
        if len(parts) != 3:
            self.ops.answer(cq_id, t["unknown"])
            return "unknown"
        _, act, arg = parts
        if act == "nav":
            text, rows = self.screen(arg)
            self.ops.answer(cq_id, "")
            self.ops.edit(msg_id, text, rows)
            return "nav"
        if act == "tog":
            return self._toggle(cq_id, msg_id, arg)
        if act == "new":
            mb = self.mailbox(arg)
            if not mb:
                self.ops.answer(cq_id, t["stale"])
                return "stale"
            self.ops.answer(cq_id, "")
            self._prompt(t["compose_prompt"].format(box=mb.id), {"type": "compose", "mailbox": mb.id})
            return "prompt_compose"
        if act == "del":
            self.ops.answer(cq_id, "")
            ok = self.disconnect(arg)
            self.ops.send(t["disconnected"] if ok else t["not_disconnectable"])
            return "disconnected" if ok else "not_disconnectable"
        if act == "conn":
            self.ops.answer(cq_id, "")
            if not self.cfg.gmail_client_secret:
                self.ops.send(t["connect_no_client"])
                return "no_client"
            self._prompt(t["connect_addr"], {"type": "connect_addr"})
            return "prompt_connect"

        card = self.state.load_card(arg)
        if not card:
            self.ops.answer(cq_id, t["stale"])
            return "stale"
        if card["status"] == "skipped":
            self.ops.answer(cq_id, t["skipped"])
            return "skipped"
        if act == "skip":
            if card["status"] in ("sent", "sending", "unknown"):
                self.ops.answer(cq_id, t["already_sent"])
                return card["status"]
            card["status"] = "skipped"
            self.ops.answer(cq_id, t["skipped"])
            self._refresh_card(card)
            return "skipped"
        if act in ("draft", "redo"):
            if card["status"] in ("sent", "sending", "unknown", "skipped"):
                self.ops.answer(cq_id, t["already_sent"])
                return card["status"]
            self.ops.answer(cq_id, t["drafting"])
            draft = self.compose(card)
            if not draft:
                self.ops.send(t["draft_failed"])
                return "draft_failed"
            card.update(draft=draft, status="drafted")
            self._refresh_card(card)
            return "drafted"
        if act in ("edit", "own"):
            if card["status"] in ("sent", "sending", "unknown", "skipped"):
                self.ops.answer(cq_id, t["already_sent"])
                return card["status"]
            self.ops.answer(cq_id, "")
            key = "edit_prompt" if act == "edit" else "own_prompt"
            self._prompt(t[key].format(box=card["mailbox"], to=card["reply_to"]), {"type": act, "sid": card["id"]})
            return f"prompt_{act}"
        if act == "send":
            if card["status"] in ("sent", "sending"):
                self.ops.answer(cq_id, t["already_sent"])
                return card["status"]
            if card["status"] == "unknown":
                self.ops.answer(cq_id, t["st_unknown"][:190])
                return "unknown"
            if not self.can_send(self.mailbox(card["mailbox"])):
                self.ops.answer(cq_id, t["no_send"])
                return "no_send"
            self.ops.answer(cq_id, t["sending"])
            return self.send_card(card["id"])
        if act == "wassent" and card["status"] == "unknown":
            card["status"] = "sent"
            self.state.append("sent.jsonl", {"mailbox": card["mailbox"], "to": card["reply_to"], "card": card["id"],
                                             "outcome": "confirmed_by_operator"})
            self.ops.answer(cq_id, t["marked_sent"])
            self._refresh_card(card)
            return "sent"
        if act == "resend" and card["status"] == "unknown":
            self.ops.answer(cq_id, t["resend_confirm"])
            return self.send_card(card["id"], force=True)
        self.ops.answer(cq_id, t["unknown"])
        return "unknown"

    def _toggle(self, cq_id, msg_id, arg) -> str:
        kind, _, box_id = arg.partition("@")
        mb = self.mailbox(box_id)
        if not mb or kind not in ("n", "r", "o"):
            self.ops.answer(cq_id, self.t["unknown"])
            return "unknown"
        if kind == "n":
            self.state.update_box(mb.id, notify=not self.notify_on(mb))
        elif kind == "r":
            self.state.update_box(mb.id, reply="off" if self.reply_mode(mb) == "draft" else "draft")
        else:
            if not mb.outreach or self.outreach is None:
                self.ops.answer(cq_id, self.t["out_locked"])
                return "locked"
            self.outreach.set_outreach_off("mail", mb.id, self.outreach_on(mb))
        self.ops.answer(cq_id, "")
        text, rows = self.screen(f"box@{mb.id}")
        self.ops.edit(msg_id, text, rows)
        return "saved"

    # ================================================================== settings screens
    def _send_label(self, mb: Mailbox) -> str:
        if mb.provider == "ms_graph":
            return "Microsoft Graph API"
        if mb.provider == "gmail_api":
            return self.t["send_ok"] + " (Gmail API)"
        ok = self.state.box(mb.id).get("send_ok")
        return {True: self.t["send_ok"], False: self.t["send_bad"]}.get(ok, self.t["send_unknown"])

    def _out_label(self, mb: Mailbox) -> str:
        if not mb.outreach:
            return self.t["out_locked"]
        return self.t["out_on"] if self.outreach_on(mb) else self.t["out_off"]

    def screen(self, name: str):
        t = self.t
        if name.startswith("box@"):
            mb = self.mailbox(name[4:])
            if mb:
                text = t["box_text"].format(addr=mb.address) + "\n\n" + self._send_label(mb)
                rows = [
                    [{"text": f"{t['notify']}: {t['on'] if self.notify_on(mb) else t['off']}", "callback_data": f"mail:tog:n@{mb.id}"}],
                    [{"text": f"{t['reply']}: {t['reply_draft'] if self.reply_mode(mb) == 'draft' else t['reply_off']}",
                      "callback_data": f"mail:tog:r@{mb.id}"}],
                    [{"text": f"{t['outreach']}: {self._out_label(mb)}", "callback_data": f"mail:tog:o@{mb.id}"}],
                    [{"text": t["b_new"], "callback_data": f"mail:new:{mb.id}"}],
                ]
                if mb.source == "connect":
                    rows.append([{"text": t["b_disconnect"], "callback_data": f"mail:del:{mb.id}"}])
                rows.append([{"text": t["back"], "callback_data": "mail:nav:main"}])
                return text, rows
        boxes = self.mailboxes()
        lines = [f"{'🔔' if self.notify_on(m) else '🔕'} {m.address} · {t['reply']}: "
                 f"{t['reply_draft'] if self.reply_mode(m) == 'draft' else t['reply_off']} · {t['outreach']}: {self._out_label(m)} · "
                 f"{self._send_label(m)}" for m in boxes]
        text = f"{t['mail_title']}\n\n{t['mail_hint']}\n\n" + ("\n".join(lines) or t["no_boxes"])
        rows = [[{"text": f"{'🔔' if self.notify_on(m) else '🔕'} {m.address}", "callback_data": f"mail:nav:box@{m.id}"}] for m in boxes]
        rows.append([{"text": t["b_connect_google"], "callback_data": "mail:conn:google"}])
        rows.append([{"text": t["back"], "callback_data": "nav:main"}])
        return text, rows

    # ================================================================== ops bot extension: text replies
    def _prompt(self, text: str, payload: dict):
        mid = self.ops.send(text, force_reply=True)
        if mid:
            self.state.set_prompt(mid, {**payload, "at": self.clock()})
        return mid

    def _live_prompts(self) -> dict:
        p = self.state.prompts()
        live = {k: v for k, v in p.items() if self.clock() - v.get("at", 0) < PROMPT_TTL_S}
        if len(live) != len(p):
            self.state.drop_prompts(live)
        return live

    def _card_by_message(self, message_id):
        return next((c for c in self.state.cards() if message_id and c.get("ops_msg") == message_id), None)

    def owns_message(self, message_id) -> bool:
        return str(message_id) in self._live_prompts() or self._card_by_message(message_id) is not None

    def waiting_count(self) -> int:
        return len(self._live_prompts())

    async def handle_text(self, msg: dict) -> str:
        return await asyncio.to_thread(self.text, msg)

    def text(self, msg: dict) -> str:
        """A reply to one of our prompts; plain text (no reply) is accepted when exactly one prompt waits
        (the ops engine checks that nothing else waits)."""
        prompts_ = self._live_prompts()
        rt = (msg.get("reply_to_message") or {}).get("message_id")
        key = str(rt) if rt and str(rt) in prompts_ else (next(iter(prompts_)) if len(prompts_) == 1 and not rt else None)
        if key is None and rt:
            card = self._card_by_message(rt)
            if card is not None:  # a reply to the card itself: use its open prompt, else explain the buttons
                own = [k for k, v in prompts_.items() if v.get("sid") == card["id"]]
                if len(own) == 1:
                    key = own[0]
                else:
                    self.ops.send(self.t["reply_hint"])
                    return "hint"
        if key is None:
            return "ignored"
        pr = self.state.pop_prompt(key)
        text = (msg.get("text") or "").strip()
        kind = pr.get("type")
        if kind in ("edit", "own"):
            card = self.state.load_card(pr["sid"])
            if not card or card["status"] in ("sent", "sending", "unknown", "skipped"):
                return "stale"
            self.ops.delete(int(key) if key.isdigit() else key)
            if kind == "own":
                card.update(draft=text, status="drafted", operator_text=True)
            else:
                new = self.compose(card, correction=text)
                if not new:
                    self.ops.send(self.t["redraft_failed"])
                    return "redraft_failed"
                card.setdefault("corrections", []).append(text)
                card.update(draft=new, status="drafted")
            self._repost_card(card)
            return "redrafted" if kind == "edit" else "own_text"
        if kind == "compose":
            head, _, body = text.partition("\n")
            to, _, subj = head.partition("|")
            to, subj, body = to.strip().lower(), subj.strip(), body.strip()
            if not ADDR_RE.match(to) or not body:
                self.ops.send(self.t["compose_format"])
                return "bad_format"
            card = {"id": _sid("new", pr["mailbox"], to, self.clock()), "mailbox": pr["mailbox"], "uid": 0, "from_name": "",
                    "from_addr": to, "reply_to": to, "subject": subj or "(no subject)", "body": "", "message_id": "",
                    "references": "", "status": "drafted", "tracker": self.tracker_ctx(to), "created": self.clock(),
                    "draft": body, "compose": True}
            self._new_card(card)
            return "compose_card"
        if kind == "connect_addr":
            return self._connect_addr(text)
        if kind == "oauth_code":
            self.ops.delete(msg.get("message_id"))
            return self._connect_finish(pr, text)
        return "ignored"

    # ================================================================== connecting Gmail from the bot
    def detect_google(self, domain: str) -> bool:
        from .connect import mx_hosts
        if domain in ("gmail.com", "googlemail.com"):
            return True
        return any(h.endswith(("google.com", "googlemail.com")) for h in mx_hosts(domain))

    def _connect_addr(self, text: str) -> str:
        addr = text.strip().lower()
        m = ADDR_RE.match(addr)
        if not m:
            self.ops.send(self.t["connect_bad_addr"])
            return "bad_addr"
        if not self.detect_google(m.group(1)):
            self.ops.send(self.t["connect_not_google"].format(domain=m.group(1), addr=addr))
            return "not_google"
        from . import gmail as g
        try:
            client = g.load_client(self.cfg.gmail_client_secret)
        except (OSError, ValueError) as e:
            self.ops.send(f"{self.t['connect_no_client']} ({e})")
            return "no_client"
        url, verifier = g.auth_url(client, addr)
        self.ops.send(self.t["connect_steps"].format(addr=addr, url=url))
        self._prompt(self.t["connect_wait"], {"type": "oauth_code", "addr": addr, "verifier": verifier})
        return "oauth_started"

    def _connect_finish(self, pr: dict, text: str) -> str:
        from .connect import finish_google
        ok, res = finish_google(self, pr["addr"], text, pr["verifier"])
        hint = self.t["connect_delete_hint"]
        if not ok:
            self.ops.send(f"⚠️ {res}{hint}")
            return "connect_failed"
        real = self.mailbox(res).address
        other = self.t["connect_other"].format(typed=pr["addr"], real=real) if real != pr["addr"] else ""
        self.ops.send(self.t["connect_ok"].format(addr=real) + other + hint)
        return "connected"

    def register_connected(self, mb: Mailbox) -> None:
        with self._lock:
            data = [d for d in self.state.load("connected.json", []) if d.get("id") != mb.id]
            data.append(mb.to_dict())
            self.state.save("connected.json", data)

    def disconnect(self, mailbox_id: str) -> bool:
        """Only mailboxes added with the connect CLI/bot; pack mailboxes are changed in the pack."""
        mb = self.mailbox(mailbox_id)
        if not mb or mb.source != "connect":
            return False
        with self._lock:
            self.state.save("connected.json", [d for d in self.state.load("connected.json", []) if d.get("id") != mailbox_id])
            self.state.drop_box(mailbox_id)
        if mb.provider == "gmail_api" and self.gmail is not None:
            self.gmail.revoke(mailbox_id)
        elif self.himalaya is not None and mb.himalaya_account:
            from .connect import remove_secret
            self.himalaya.remove_account(mb.himalaya_account)
            remove_secret(self.himalaya, mb.himalaya_account)
        return True

    # ================================================================== loop (inside the ops bot process)
    async def poll_loop(self, first_delay: float = 25.0):
        await asyncio.sleep(first_delay)
        print("[mail] poll loop started", file=sys.stderr)
        while True:
            if (self.cfg.state_dir / "mail_off").exists():  # kill switch: touch KIT_HOME/mail_state/mail_off
                await asyncio.sleep(self.cfg.poll_seconds)
                continue
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception as e:
                print(f"[mail] poll error: {e}", file=sys.stderr)
            await asyncio.sleep(self.cfg.poll_seconds)

