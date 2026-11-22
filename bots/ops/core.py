"""Ops bot engine: incoming DM -> filters -> category -> route -> off/draft/auto -> card or send.

All I/O is injected, so tests drive it with fakes:
  llm        .complete(system, user, max_tokens) -> str | None
  ops        OpsBot-like: .send/.edit/.delete/.answer
  transport  async .send(account, peer, text, typing_s) -> message_id (raises TransportPaused on FloodWait/PeerFlood)
             async .history(account, peer, limit) -> [(ts, "in"|"out", text)]
  crm        crm.base.CRMAdapter
  extensions objects that own their own cards in the same ops chat (kit/bots/mail): see `Extension` below
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from dataclasses import dataclass

from . import cards, prompts
from .config import OpsConfig
from .crm import get_adapter
from .store import OPEN_STATUSES, Store
from .textutil import (is_autoreply_text, is_bot_menu_text, needs_translation, normalize_handle,
                       parse_compose_output, sanitize_markdown)

COLD_INBOUND = "cold_inbound"
FIRST_REPLY = "first_reply"
HISTORY_LIMIT = 20
MEDIA_PLACEHOLDER = "[media]"


class TransportPaused(Exception):
    """The account is paused by Telegram (FloodWait/PeerFlood); nothing can be sent from it right now."""


@dataclass
class Inbound:
    account: str
    peer: str                  # "@username" or the numeric user id as a string
    text: str
    message_id: int
    peer_id: int | None = None
    sender_is_bot: bool = False
    sender_name: str = ""      # first/last name + username, used for the internal-marker check


class Extension:
    """What the engine needs from a module that shares the ops chat (one process polls the bot token).
    `prefixes`: first callback_data segments it owns; `menu_rows()`: rows for /settings."""
    prefixes: tuple = ()

    def menu_rows(self) -> list:
        return []

    async def handle_callback(self, cq: dict) -> str:
        return "unknown"

    def owns_message(self, message_id) -> bool:
        return False

    def waiting_count(self) -> int:
        """Prompts waiting for operator text: counts towards the "exactly one card waits" rule."""
        return 0

    async def handle_text(self, msg: dict) -> str:
        return "ignored"


def short_id(item_id: str) -> str:
    # callback_data is capped at 64 bytes by Telegram, item ids can be longer
    return hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:12]


class Engine:
    def __init__(self, cfg: OpsConfig, *, llm, ops, transport, crm=None, store: Store | None = None,
                 now=None, sleep=None, context_provider=None, own_ids=None, extensions=None):
        self.cfg = cfg
        self.llm = llm
        self.ops = ops
        self.transport = transport
        self.crm = crm or get_adapter(cfg.crm)
        self.store = store or Store(cfg.state_dir, cfg.send_log_path)
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.sleep = sleep or asyncio.sleep
        self.context_provider = context_provider
        self.own_ids = set(own_ids or ())   # our own accounts' user ids + our bots' ids: never "incoming"
        self.t = cards.ui(cfg.operator_language)
        self._seen = set()
        self._sent_ids = set()                # (account, message_id) sent by the bot itself
        self._batches = {}
        self.extensions = list(extensions or [])

    # ================================================================== settings
    def settings(self) -> dict:
        return self.store.settings()

    def mode_for(self, account: str) -> str:
        s = self.settings()
        return ((s.get("account_modes") or {}).get(account) or self.cfg.account_modes.get(account)
                or s.get("default_mode") or self.cfg.default_mode)

    def notify_enabled(self) -> bool:
        return self.settings().get("notify", True)

    def _menu(self, screen: str):
        if screen == "accounts":
            return cards.accounts_menu(self.t, self.cfg, self.settings(), self.store.outreach_off("telegram"))
        extra = [row for ext in self.extensions for row in ext.menu_rows()]
        return cards.settings_menu(self.t, self.cfg, self.settings(), extra)

    def _today(self) -> str:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.cfg.timezone)
        except Exception:
            tz = dt.timezone.utc
        return self.now().astimezone(tz).date().isoformat()

    def _cat_label(self, category):
        return self.t.get(f"cat_{category}", category) if category else None

    # ================================================================== incoming
    def _is_internal(self, msg: Inbound) -> bool:
        if msg.peer_id is not None and (msg.peer_id in self.own_ids or msg.peer_id in set(self.cfg.internal_ids)):
            return True
        blob = (msg.sender_name or "").lower()
        return any(m and m in blob for m in self.cfg.internal_markers)

    async def handle_incoming(self, msg: Inbound) -> str:
        key = (msg.account, msg.message_id, normalize_handle(msg.peer))
        if key in self._seen:
            return "duplicate"
        self._seen.add(key)
        if len(self._seen) > 5000:
            self._seen = {key}
        text = (msg.text or "").strip() or MEDIA_PLACEHOLDER
        if self._is_internal(msg):
            return "internal"
        if self.store.is_muted(msg.peer):
            return "muted"
        has_out = self.store.has_prior(msg.peer, "out")
        if msg.sender_is_bot and not (has_out and not is_bot_menu_text(text)):
            self.store.log({"direction": "in", "account": msg.account, "peer": msg.peer, "kind": "bot",
                            "text": text, "message_id": msg.message_id})
            return "quiet_bot"
        if is_autoreply_text(text):
            self.store.log({"direction": "in", "account": msg.account, "peer": msg.peer, "kind": "autoreply",
                            "text": text, "message_id": msg.message_id})
            return "quiet_autoreply"

        if not has_out:
            category = COLD_INBOUND
        elif not self.store.has_prior(msg.peer, "in", msg.account):
            category = FIRST_REPLY
        else:
            category = self.classify(text)
        self.store.log({"direction": "in", "account": msg.account, "peer": msg.peer, "kind": "reply",
                        "text": text, "message_id": msg.message_id})

        mode = self.mode_for(msg.account)
        if mode == "off":
            if not self.notify_enabled():
                return "notify_suppressed"
            label = self._cat_label(category)
            self.ops.send(f"[{msg.account}] {self.t['new_dm']} {msg.peer}" + (f" ({label})" if label else "")
                          + f":\n\n{text[:700]}")
            return "notified"
        self._queue(msg.account, msg.peer, text, msg.message_id, category, mode)
        return "queued"

    def classify(self, text: str):
        if not self.cfg.reply_categories:
            return None
        raw = self.llm.complete(prompts.classify_prompt(self.cfg), text[:1000], max_tokens=200) or ""
        low = raw.strip().lower()
        return next((c for c in self.cfg.reply_categories if c.lower() in low), None)

    # ================================================================== batching
    def _queue(self, account, peer, text, message_id, category, mode) -> None:
        """A burst of messages from one contact gets ONE draft, `batching_seconds` after the last one."""
        key = (account, normalize_handle(peer))
        b = self._batches.get(key)
        if b and b.get("task") and not b["task"].done():
            b["task"].cancel()
        if not b:
            b = {"texts": [], "category": category}
            self._batches[key] = b
        b["texts"].append(text)
        b["last_id"] = message_id
        b["mode"] = mode

        async def fire():
            try:
                await self.sleep(self.cfg.batching_seconds)
            except asyncio.CancelledError:
                return
            done = self._batches.pop(key, None) or b
            try:
                await self.process(account, peer, "\n".join(done["texts"]), done["last_id"], done["category"], done["mode"])
            except Exception as e:  # one bad batch must not stop the listener
                print(f"[ops/{account}] batch for {peer} failed: {e}")

        b["task"] = asyncio.ensure_future(fire())

    async def flush(self) -> None:
        """Wait for every pending batch (tests and graceful shutdown)."""
        while True:
            tasks = [b["task"] for b in self._batches.values() if b.get("task") and not b["task"].done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    # ================================================================== routes
    def match_route(self, category, text: str):
        low = (text or "").lower()
        for r in self.cfg.routes:
            m = r.get("match") or {}
            if category and category in (m.get("categories") or []):
                return r
            if any(k.lower() in low for k in m.get("keywords") or []):
                return r
        return None

    # ================================================================== compose
    async def _history_text(self, account, peer) -> str:
        rows = []
        try:
            rows = await self.transport.history(account, peer, HISTORY_LIMIT)
        except Exception as e:
            print(f"[ops/{account}] history fetch failed for {peer}: {e}")
        if not rows:
            rows = self.store.history(peer, HISTORY_LIMIT)
        return "\n".join(f"[{ts}] {'We' if d == 'out' else 'Contact'}: {txt[:600]}" for ts, d, txt in rows)

    def compose(self, item: dict, route=None) -> dict:
        raw = self.llm.complete(prompts.compose_system(self.cfg, (route or {}).get("hint", "")),
                                prompts.compose_user(self.cfg, item), max_tokens=6000)
        out = parse_compose_output(raw or "")
        op = self.cfg.operator_language
        if out["reply"] and not out["translation"] and needs_translation(out["reply"], op):
            out["translation"] = self.translate(out["reply"]) or ""
        if not out["incoming_translation"] and needs_translation(item["text"], op):
            out["incoming_translation"] = self.translate(item["text"]) or ""
        return out

    def translate(self, text: str):
        res = self.llm.complete(prompts.translate_system(self.cfg), text[:2500], max_tokens=1500)
        return sanitize_markdown(res) if res else None

    def gate(self, item: dict, reply: str):
        """None = pass, else a reason. Fails closed: no controller answer means no auto-send."""
        if not self.cfg.gate_enabled:
            return None
        raw = (self.llm.complete(prompts.gate_system(self.cfg), prompts.gate_user(self.cfg, item, reply), max_tokens=300) or "").strip()
        if raw.upper().startswith("OK"):
            return None
        return raw[:200] or "no controller answer"

    # ================================================================== processing one batch
    async def process(self, account, peer, text, message_id, category, mode) -> dict:
        route = self.match_route(category, text)
        item = {
            "id": f"{account}_{normalize_handle(peer)}_{message_id}", "account": account, "peer": peer,
            "message_id": message_id, "text": text, "category": self._cat_label(category), "category_id": category,
            "mode": mode, "route": (route or {}).get("id"), "status": "waiting",
            "created_at": self.now().isoformat(), "drafts": [], "card_message_id": None, "prompt_message_id": None,
        }
        item["short_id"] = short_id(item["id"])
        existing = self.store.load_item(item["id"])
        if existing and existing.get("status") != "waiting":
            return existing
        if self.context_provider:
            try:
                item["context"] = self.context_provider(peer) or ""
            except Exception as e:
                print(f"[ops] context lookup failed for {peer}: {e}")
        self._crm_offer(peer)
        item["history"] = await self._history_text(account, peer)
        self.store.save_item(item)
        self._retire(peer, account=account, keep_id=item["id"], statuses=("waiting", "drafted", "awaiting_approval"))

        out = self.compose(item, route)
        if (self.store.load_item(item["id"]) or {}).get("status") == "superseded":
            return item  # a newer batch for this contact took over while we were composing
        item["incoming_translation"] = out["incoming_translation"]
        item["stage"] = out["stage"]
        if not out["reply"]:
            self.store.save_item(item)
            if self.notify_enabled():
                self.ops.send(f"[{account}] {self.t['new_dm']} {peer}:\n\n{text[:700]}\n\n({self.t['compose_failed']})")
            return item
        item["drafts"] = [{"text": out["reply"], "cta": out["cta"], "translation": out["translation"]}]
        item["status"] = "drafted"
        self.store.save_item(item)

        if route and route.get("action") == "handoff":
            self.ops.send(cards.lead_card(self.t, item, route.get("id", "")))
        if mode == "auto":
            reason = self._auto_block_reason(item, route)
            if reason is None:
                reason = await self._auto_send(item)
                if reason is None:
                    return item
            item["hold_reason"] = self.t["reasons"].get(reason, reason)
        self._send_card(item)
        return item

    def _auto_block_reason(self, item: dict, route):
        if self.store.takeover_until(item["peer"], self.now()):
            return "takeover"
        if route and route.get("action") in ("draft", "handoff"):
            return "route"
        if self.store.auto_sent_today(self._today()) >= self.cfg.daily_limit:
            return "daily_limit"
        blocked = self.gate(item, item["drafts"][-1]["text"])
        if blocked:
            item["gate_reason"] = blocked
            return "gate"
        return None

    async def _auto_send(self, item: dict):
        text = item["drafts"][-1]["text"]
        outcome = await self._send_text(item, text, auto=True)
        if outcome == "paused":
            return "paused"
        if outcome == "unknown":
            return None  # Held for manual reconciliation; do not offer another send.
        self.store.count_auto_sent(self._today())
        self.ops.send(f"{self.t['auto_sent']} [{item['account']}] {item['peer']}:\n\n{text}")
        self._crm_writeback(item)
        return None

    def _send_card(self, item: dict) -> None:
        mid = self.ops.send(cards.card_body(self.t, item),
                            buttons=cards.approval_buttons(self.t, item["short_id"], bool(item["drafts"][-1].get("cta"))))
        if mid:
            item["card_message_id"] = mid
            item["status"] = "awaiting_approval"
        self.store.save_item(item)

    def _record_sent(self, item: dict, text: str, msg_id, auto: bool = False) -> None:
        self._sent_ids.add((item["account"], str(msg_id)))
        rec = {"direction": "out", "account": item["account"], "peer": item["peer"], "kind": "reply",
               "hypothesis": None, "message_id": msg_id, "text": text}
        if auto:
            rec["auto"] = True
        self.store.log(rec)

    def _hold_delivery(self, item: dict) -> None:
        item["status"] = "delivery_unknown"
        self.store.save_item(item)
        self.ops.send(f"[{item['account']}] {item['peer']}: {self.t['check_delivery']}\n\n{item.get('sending_text', '')}")

    def recover_sends(self) -> None:
        for item in self.store.items():
            if item.get("status") == "sending":
                self._hold_delivery(item)

    async def _send_text(self, item: dict, text: str, *, auto=False, delay=0) -> str:
        """Persist the exact message before its delivery can become ambiguous."""
        clean = text if auto else sanitize_markdown(text)
        previous = item["status"]
        item.update(status="sending", sending_text=clean)
        self.store.save_item(item)
        if delay:
            await self.sleep(delay)
        try:
            typing = self.cfg.typing_seconds(clean) if auto else 0.0
            msg_id = await self.transport.send(item["account"], item["peer"], clean, typing)
        except TransportPaused:
            item["status"] = previous
            self.store.save_item(item)
            return "paused"
        except Exception as e:
            print(f"[ops/{item['account']}] send failed for {item['peer']}: {e}")
            self._hold_delivery(item)
            return "unknown"
        self._record_sent(item, clean, msg_id, auto=auto)
        item.update(status="sent", sent_message_id=msg_id)
        self.store.save_item(item)
        return "sent"

    def _retire(self, peer, account=None, keep_id=None, statuses=OPEN_STATUSES) -> int:
        """Older open cards for the same contact are noise once a newer one exists or someone replied."""
        n = 0
        want = normalize_handle(peer)
        for it in self.store.items():
            if it.get("id") == keep_id or normalize_handle(it.get("peer")) != want:
                continue
            if account and it.get("account") != account:
                continue
            if it.get("status") not in statuses:
                continue
            for mid in (it.get("card_message_id"), it.get("prompt_message_id")):
                if mid:
                    self.ops.delete(mid)
            it.update(status="superseded", card_message_id=None, prompt_message_id=None)
            self.store.save_item(it)
            n += 1
        return n

    # ================================================================== CRM
    def _crm_peer_key(self, peer):
        handle = normalize_handle(peer)
        if self.crm.name in ("hubspot", "bitrix24"):
            return f"{self.crm.name}:{handle}"
        return handle  # Preserve links and outstanding offers from the original adapter.

    def _crm_offer(self, peer: str) -> None:
        key = self._crm_peer_key(peer)
        if self.crm.name == "none" or self.store.crm_link(key):
            return
        offers = self.store.load("crm_offers.json", {})
        sid = short_id("crm_" + key)
        if sid in offers:
            return
        candidates = self.crm.search_leads(normalize_handle(peer))[:5]
        if not candidates:
            return
        offers[sid] = {"peer": peer, "ids": [c["id"] for c in candidates], "crm": self.crm.name}
        self.store.save("crm_offers.json", offers)
        names = "; ".join(f"#{c['id']} «{c.get('name', '')}»" for c in candidates)
        rows = [[{"text": f"#{c['id']}", "callback_data": f"crm:{sid}:{c['id']}"}] for c in candidates]
        rows.append([{"text": self.t["crm_skip"], "callback_data": f"crm:{sid}:skip"}])
        self.ops.send(f"{self.t['crm_offer']} {peer}:\n{names}", buttons=rows)

    def _crm_writeback(self, item: dict) -> None:
        crm_cfg = self.cfg.crm
        if self.crm.name == "none" or not crm_cfg.get("writeback"):
            return
        lead_id = self.store.crm_link(self._crm_peer_key(item["peer"]))
        if not lead_id:
            return
        self.crm.add_note(lead_id, f"{item['peer']}: {item['text'][:500]}\n\n-> {item['drafts'][-1]['text'][:500]}")
        if crm_cfg.get("advance_on_reply"):
            self.crm.advance_from_unsorted(lead_id)
        if crm_cfg.get("task_on_reply"):
            self.crm.create_task(lead_id, f"Reply to {item['peer']}")

    # ================================================================== manual outgoing (human takeover)
    async def handle_outgoing(self, account: str, peer: str, message_id, text: str, peer_id=None) -> str:
        """A message the human typed from a bot-managed account: log it, pause auto for this contact,
        drop the now-obsolete cards. Messages the bot sent itself are recognised and ignored."""
        if (account, str(message_id)) in self._sent_ids or self.store.outgoing_logged(account, message_id):
            return "own_send"
        if peer_id is not None and (peer_id in self.own_ids or peer_id in set(self.cfg.internal_ids)):
            return "internal"
        self.store.log({"direction": "out", "account": account, "peer": peer, "kind": "manual", "hypothesis": None,
                        "message_id": message_id, "text": (text or "").strip() or MEDIA_PLACEHOLDER, "manual": True})
        if self.cfg.takeover_hours > 0:
            self.store.set_takeover(peer, self.now() + dt.timedelta(hours=self.cfg.takeover_hours))
        self._retire(peer)
        return "takeover"

    # ================================================================== ops bot: buttons
    async def handle_callback(self, cq: dict) -> str:
        cq_id, data = cq.get("id"), cq.get("data", "")
        msg_id = (cq.get("message") or {}).get("message_id")
        parts = data.split(":")
        if parts[0] == "nav" and len(parts) == 2:
            if parts[1] == "close":
                self.ops.delete(msg_id)
                self.ops.answer(cq_id, self.t["closed"])
                return "closed"
            text, rows = self._menu(parts[1])
            self.ops.answer(cq_id, "")
            self.ops.edit(msg_id, text, rows)
            return "nav"
        for ext in self.extensions:
            if parts[0] in ext.prefixes:
                return await ext.handle_callback(cq)
        if parts[0] == "set" and len(parts) == 3:
            return self._apply_setting(cq_id, msg_id, parts[1], parts[2])
        if parts[0] == "crm" and len(parts) == 3:
            return self._crm_callback(cq_id, msg_id, parts[1], parts[2])
        if parts[0] != "appr" or len(parts) != 3:
            self.ops.answer(cq_id, self.t["unknown"])
            return "unknown"
        _, action, sid = parts
        item = self.store.find_item(short_id=sid)
        if not item:
            self.ops.answer(cq_id, self.t["not_found"])
            return "not_found"
        if item.get("status") in ("delivery_unknown", "sending"):
            self.ops.answer(cq_id, self.t["check_delivery"])
            return "unknown"
        if item.get("status") not in ("awaiting_approval", "awaiting_edit_text", "awaiting_own_text"):
            self.ops.answer(cq_id, self.t["done_already"])
            return "done_already"

        if action in ("send", "sendcta"):
            draft = item["drafts"][-1]
            outcome = await self._send_text(item, draft["text"])
            if outcome == "unknown":
                self.ops.answer(cq_id, self.t["check_delivery"])
                return "unknown"
            if outcome != "sent":
                self.ops.answer(cq_id, self.t["send_failed"])
                return "send_failed"
            cta = draft.get("cta") if action == "sendcta" else ""
            if cta:
                if await self._send_text(item, cta, delay=3) != "sent":
                    cta = ""
                    self.ops.send(f"⚠️ CTA {item['peer']}: {self.t['send_failed']}")
            self.ops.answer(cq_id, self.t["sent"])
            self.ops.edit(item.get("card_message_id"), f"{self.t['sent']} ({item['peer']}):\n\n{draft['text']}"
                          + (f"\n\n+ CTA:\n{cta}" if cta else ""))
            self._retire(item["peer"], account=item["account"], keep_id=item["id"])
            self._crm_writeback(item)
            return "sent"
        if action == "skip":
            item["status"] = "skipped"
            self.store.save_item(item)
            self.ops.answer(cq_id, self.t["skipped"])
            self.ops.edit(item.get("card_message_id"), f"{self.t['skipped']} ({item['peer']})")
            return "skipped"
        if action in ("edit", "owntext"):
            item["status"] = "awaiting_edit_text" if action == "edit" else "awaiting_own_text"
            prompt = (f"[{item['account']}] {self.t['what_to_fix']} {item['peer']}?" if action == "edit"
                      else f"[{item['account']}] {self.t['own_prompt']} {item['peer']}")
            item["prompt_message_id"] = self.ops.send(prompt, force_reply=True)
            self.store.save_item(item)
            self.ops.answer(cq_id, "")
            return item["status"]
        if action == "mute":
            self.store.mute(item["peer"])
            item["status"] = "skipped"
            self.store.save_item(item)
            self.ops.answer(cq_id, self.t["muted"])
            self._retire(item["peer"])
            if item.get("card_message_id"):
                self.ops.delete(item["card_message_id"])
            return "muted"
        self.ops.answer(cq_id, self.t["unknown"])
        return "unknown"

    def _apply_setting(self, cq_id, msg_id, kind, value) -> str:
        s = self.settings()
        if kind == "mode" and value == "cycle":
            self.store.update_settings(default_mode=cards.next_mode(s.get("default_mode") or self.cfg.default_mode))
            screen = "main"
        elif kind == "notify" and value == "toggle":
            self.store.update_settings(notify=not s.get("notify", True))
            screen = "main"
        elif kind == "acc" and value in self.cfg.accounts:
            modes = dict(s.get("account_modes") or {})
            modes[value] = cards.next_mode(self.mode_for(value))
            self.store.update_settings(account_modes=modes)
            screen = "accounts"
        elif kind == "tgout" and value in self.cfg.accounts:
            if value in self.cfg.outreach_disabled:  # the operator can narrow the pack, never widen it
                self.ops.answer(cq_id, self.t["outreach_locked"])
                return "locked"
            self.store.set_outreach_off("telegram", value, value not in self.store.outreach_off("telegram"))
            screen = "accounts"
        else:
            self.ops.answer(cq_id, self.t["unknown"])
            return "unknown"
        self.ops.answer(cq_id, self.t["saved"])
        text, rows = self._menu(screen)
        self.ops.edit(msg_id, text, rows)
        return "saved"

    def _crm_callback(self, cq_id, msg_id, sid, choice) -> str:
        offers = self.store.load("crm_offers.json", {})
        offer = offers.pop(sid, None)
        if not offer or offer.get("crm", "amocrm") != self.crm.name:
            self.ops.answer(cq_id, self.t["done_already"])
            return "done_already"
        self.store.save("crm_offers.json", offers)
        if choice == "skip":
            self.ops.answer(cq_id, self.t["crm_skipped"])
            self.ops.edit(msg_id, f"{self.t['crm_skipped']}: {offer['peer']}")
            return "crm_skipped"
        if choice not in {str(i) for i in offer["ids"]}:
            self.ops.answer(cq_id, self.t["unknown"])
            return "unknown"
        self.store.set_crm_link(self._crm_peer_key(offer["peer"]), int(choice) if choice.isdigit() else choice)
        self.ops.answer(cq_id, self.t["crm_linked"])
        self.ops.edit(msg_id, f"{self.t['crm_linked']} #{choice}: {offer['peer']}")
        return "crm_linked"

    # ================================================================== ops bot: text
    async def handle_text(self, msg: dict) -> str:
        text = (msg.get("text") or "").strip()
        if not text:
            return "empty"
        if text.split()[0] in ("/settings", "/start"):
            body, rows = self._menu("main")
            self.ops.send(body, buttons=rows)
            return "menu"
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        item = self.store.find_by_ops_message(reply_to) if reply_to else None
        if item and item.get("status") not in ("awaiting_edit_text", "awaiting_own_text"):
            item = None
        if item is None and reply_to:
            for ext in self.extensions:
                if ext.owns_message(reply_to):
                    return await ext.handle_text(msg)
            return "ignored"
        if item is None:
            waiting = [i for i in self.store.items() if i.get("status") in ("awaiting_edit_text", "awaiting_own_text")]
            ext_waiting = [ext for ext in self.extensions for _ in range(ext.waiting_count())]
            if len(waiting) + len(ext_waiting) > 1:
                self.ops.send(self.t["ambiguous"])
                return "ambiguous"
            if ext_waiting:
                return await ext_waiting[0].handle_text(msg)
            if not waiting:
                return "ignored"
            item = waiting[0]  # exactly one card waits for text: plain text (no reply) is accepted for it

        if item["status"] == "awaiting_own_text":
            outcome = await self._send_text(item, text)
            if outcome == "unknown":
                return "unknown"
            if outcome != "sent":
                self.ops.send(self.t["send_failed"], reply_to=reply_to)
                return "send_failed"
            item["drafts"].append({"text": text, "operator_edited": True})
            item["status"] = "sent"
            self.ops.delete(item.get("prompt_message_id"))
            self.ops.edit(item.get("card_message_id"), f"{self.t['own_sent']} ({item['peer']}):\n\n{sanitize_markdown(text)}")
            item["prompt_message_id"] = None
            self.store.save_item(item)
            self._retire(item["peer"], account=item["account"], keep_id=item["id"])
            self._crm_writeback(item)
            return "sent_own"

        ctx = dict(item, previous_draft=item["drafts"][-1]["text"], correction=text)
        route = next((r for r in self.cfg.routes if r.get("id") == item.get("route")), None)
        out = self.compose(ctx, route)
        if not out["reply"]:
            self.ops.send(self.t["redraft_failed"], reply_to=reply_to)
            return "redraft_failed"
        prev_cta = item["drafts"][-1].get("cta") or ""
        item["drafts"].append({"text": out["reply"], "cta": out["cta"] or prev_cta, "translation": out["translation"],
                               "correction": text})
        if out["stage"]:
            item["stage"] = out["stage"]
        # the new card goes below the operator's correction; the old card and the prompt are deleted
        self.ops.delete(item.get("card_message_id"))
        self.ops.delete(item.get("prompt_message_id"))
        item["prompt_message_id"] = None
        item["status"] = "awaiting_approval"
        self._send_card(item)
        return "redrafted"
