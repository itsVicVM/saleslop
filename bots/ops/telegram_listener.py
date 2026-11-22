#!/usr/bin/env python3
"""Telethon glue: one client per watched account in one process, events -> core.Engine, ops-bot polling.

Run (never on a host where these sessions are also used elsewhere: a session on two hosts gets the
account's auth key revoked):
    python3 kit/runner/kit_env.py --pack <pack> --module ops --env-file <.env> run -- \
        python3 -m bots.ops.telegram_listener          # from the kit/ directory

Sessions: $KIT_HOME/telegram_sender/sessions/<account>.session (created by sender.py login).
Only 1:1 chats are handled in this version; group chats are ignored.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import time

from .config import ConfigError, OpsConfig
from .core import Engine, Inbound, TransportPaused
from .crm import get_adapter
from .llm import ChatLLM
from .opsbot import OpsBot

OUTGOING_SETTLE_S = 8   # our own sends log themselves right after Telegram confirms; wait before calling it "manual"


class TelethonTransport:
    def __init__(self, clients: dict, notify):
        self.clients = clients
        self.notify = notify
        self.paused_until = {}

    async def send(self, account, peer, text, typing_s=0.0):
        until = self.paused_until.get(account)
        if until and until > dt.datetime.now(dt.timezone.utc):
            raise TransportPaused(account)
        client = self.clients.get(account)
        if client is None:
            raise RuntimeError(f"no connected client for account {account!r}")
        from telethon.errors import FloodWaitError, PeerFloodError
        target = int(peer) if str(peer).lstrip("-").isdigit() else peer
        try:
            entity = await client.get_entity(target)
            if typing_s > 0:
                async with client.action(entity, "typing"):
                    await asyncio.sleep(typing_s)
            msg = await client.send_message(entity, text)
            return msg.id
        except (FloodWaitError, PeerFloodError) as e:
            secs = max(int(getattr(e, "seconds", 0) or 0), 3600)
            self.paused_until[account] = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=secs)
            self.notify(f"⏸ [{account}] Telegram limited this account ({type(e).__name__}); sending paused for {secs // 60} min.")
            raise TransportPaused(account) from e

    async def history(self, account, peer, limit):
        client = self.clients.get(account)
        if client is None:
            return []
        target = int(peer) if str(peer).lstrip("-").isdigit() else peer
        msgs = await client.get_messages(target, limit=limit)
        rows = []
        for m in reversed(msgs):
            t = (m.raw_text or "").strip() or ("[media]" if m.media else "")
            if t:
                rows.append((m.date.strftime("%Y-%m-%d %H:%M"), "out" if m.out else "in", t))
        return rows


def _peer_label(entity) -> str:
    username = getattr(entity, "username", None)
    return f"@{username}" if username else str(getattr(entity, "id", ""))


def _name_blob(entity) -> str:
    return " ".join(str(getattr(entity, a, "") or "") for a in ("first_name", "last_name", "username"))


async def _guarded_loop(name, client, engine_notify):
    """Keeps ONE account connected; a failing account reconnects with backoff without stopping the others."""
    delay, streak, alerted = 30, 0, False
    while True:
        started = time.monotonic()
        try:
            await client.run_until_disconnected()
            reason = "disconnected"
        except Exception as e:
            reason = str(e)[:200]
        if time.monotonic() - started > 600:
            delay, streak, alerted = 30, 0, False
        streak += 1
        print(f"[ops/listener] {name}: {reason}; reconnecting in {delay}s (streak {streak})")
        if streak >= 5 and not alerted:
            alerted = True
            engine_notify(f"[listener] {name}: connection lost {streak} times in a row ({reason}); other accounts keep working.")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 900)
        try:
            if client.is_connected():
                await client.disconnect()
            await client.connect()
            if not await client.is_user_authorized():
                engine_notify(f"[listener] {name}: session logged out; re-login with sender.py login. Other accounts keep working.")
                return
        except Exception as e:
            print(f"[ops/listener] {name}: reconnect failed: {e}")


async def _poll_ops(engine: Engine, ops: OpsBot):
    loop = asyncio.get_running_loop()
    offset = 0
    while True:
        try:
            updates = await loop.run_in_executor(None, ops.get_updates, offset)
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        await engine.handle_callback(u["callback_query"])
                    elif "message" in u and str(u["message"].get("chat", {}).get("id")) == str(ops.chat_id):
                        await engine.handle_text(u["message"])
                except Exception as e:
                    print(f"[ops/listener] update handling error: {e}")
        except Exception as e:
            print(f"[ops/listener] ops poll error: {e}")
            await asyncio.sleep(5)


def _mail_extension(cfg: OpsConfig, llm, ops):
    """Mail cards (kit/bots/mail) in the same ops chat when mail.bot.enabled is set in the pack. A broken mail config
    is reported and the Telegram part still starts."""
    if not (cfg.mail or {}).get("enabled"):
        return None
    import os

    from ..mail import gmail as g
    from ..mail.config import MailConfig, MailConfigError
    from ..mail.engine import MailEngine
    from ..mail.himalaya import Himalaya
    from ..mail.sinks import outreach_bounce_sink, sheet_rows_provider
    try:
        mcfg = MailConfig.from_dict(cfg.mail, home=cfg.home)
    except MailConfigError as e:
        ops.send(f"[mail] disabled: {e}")
        return None
    for p in mcfg.problems():
        print(f"[mail] {p}")
    client = None
    if mcfg.gmail_client_secret:
        try:
            client = g.load_client(mcfg.gmail_client_secret)
        except (OSError, ValueError) as e:
            print(f"[mail] OAuth client unreadable: {e}")
    rows = None
    if os.environ.get("SHEET_ID") and mcfg.tracker_tab and mcfg.tracker_columns.get("email"):
        rows = sheet_rows_provider(os.environ["SHEET_ID"], mcfg.tracker_tab, mcfg.tracker_columns)
    from .store import Store
    return MailEngine(mcfg, cfg, llm=llm, ops=ops,
                      himalaya=Himalaya(mcfg.himalaya_bin, config_path=mcfg.himalaya_config or None),
                      gmail=g.Gmail(client, g.TokenStore(mcfg.state_dir / "tokens")), rows_provider=rows,
                      bounce_sink=outreach_bounce_sink(mcfg.outreach_state_dir) if mcfg.outreach_state_dir else None,
                      outreach=Store(cfg.state_dir, cfg.send_log_path))


async def main() -> int:
    try:
        cfg = OpsConfig.from_env()
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not cfg.accounts:
        print("ERROR: no accounts to watch (bot.ops.accounts / team.members[].telegram.accounts / KIT_OPS_ACCOUNTS)", file=sys.stderr)
        return 2
    if not cfg.ops_bot_token or not cfg.ops_chat_id:
        print("ERROR: KIT_OPS_BOT_TOKEN and KIT_OPS_CHAT_ID are required (bot.ops.bot_token / owner_chat in the pack)", file=sys.stderr)
        return 2
    import os

    from telethon import TelegramClient, events

    api_cfg_path = cfg.sender_dir / "config.json"
    try:
        api_cfg = json.loads(api_cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"ERROR: Telegram API config {api_cfg_path} unreadable ({e}); run sender.py init", file=sys.stderr)
        return 2

    ops = OpsBot(cfg.ops_bot_token, cfg.ops_chat_id)
    clients, own_ids = {}, set()
    if ops.bot_id:
        own_ids.add(ops.bot_id)
    for name in cfg.accounts:
        app = (api_cfg.get("apps") or {}).get(name) or api_cfg
        client = TelegramClient(str(cfg.sender_dir / "sessions" / name), int(app["api_id"]), app["api_hash"])
        # connect() + is_user_authorized() instead of start(): start() falls back to an interactive login
        # prompt for a logged-out session, which blocks forever under systemd and takes every account down
        try:
            await client.connect()
            if not await client.is_user_authorized():
                print(f"[ops/listener] {name}: session not authorized, skipped (others still start)")
                await client.disconnect()
                continue
            me = await client.get_me()
        except Exception as e:
            print(f"[ops/listener] {name}: connect failed ({e}), skipped")
            continue
        own_ids.add(me.id)
        clients[name] = client
    if not clients:
        print("ERROR: no account could be connected", file=sys.stderr)
        return 1

    transport = TelethonTransport(clients, ops.send)
    llm = ChatLLM(os.environ.get("OPENAI_API_KEY"), cfg.llm_model, cfg.llm_base_url)
    mail = _mail_extension(cfg, llm, ops)
    engine = Engine(cfg, llm=llm, ops=ops, transport=transport, crm=get_adapter(cfg.crm), own_ids=own_ids,
                    extensions=[mail] if mail else [])
    engine.store.ensure()
    engine.recover_sends()
    if mail:
        mail.recover_sends()
        asyncio.create_task(mail.poll_loop())

    for name, client in clients.items():
        def incoming(account):
            async def handler(event):
                if not event.is_private:
                    return
                sender = await event.get_sender()
                await engine.handle_incoming(Inbound(
                    account=account, peer=_peer_label(sender), text=event.raw_text or ("[media]" if event.media else ""),
                    message_id=event.id, peer_id=getattr(sender, "id", None), sender_is_bot=bool(getattr(sender, "bot", False)),
                    sender_name=_name_blob(sender)))
            return handler

        def outgoing(account):
            async def handler(event):
                if not event.is_private:
                    return
                await asyncio.sleep(OUTGOING_SETTLE_S)
                chat = await event.get_chat()
                if getattr(chat, "bot", False):
                    return
                await engine.handle_outgoing(account, _peer_label(chat), event.id, event.raw_text or "",
                                             peer_id=getattr(chat, "id", None))
            return handler

        client.add_event_handler(incoming(name), events.NewMessage(incoming=True))
        client.add_event_handler(outgoing(name), events.NewMessage(outgoing=True))
        asyncio.create_task(_guarded_loop(name, client, ops.send))
        print(f"[ops/listener] watching {name}")
    await _poll_ops(engine, ops)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
