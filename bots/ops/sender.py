#!/usr/bin/env python3
"""Telethon CLI for the team's own Telegram user accounts: login, send with guards, draft, poll, group creation.

Commands:
  init [--name N]                 store Telegram API credentials (default app, or a named app for one account)
  login --name N --phone P        step 1: send the login code; step 2: --code C; step 3 (2FA): --password X
  qr-login-start / qr-login-check --name N   login by confirming a tg://login link in an already logged-in app
  send --name N --peer P --text T [--file F] [--kind opener|followup1|followup2|followup3|reply|message]
  draft --name N --peer P --text T            save a Telegram draft (not sent), same text checks as send
  poll --name N                   log new 1:1 incoming messages (local cursor, never marks anything read)
  creategroup --name N --title T --member P [--member P2] [--photo F]
  whoami --name N | list

Every send is logged to $KIT_HOME/outreach/send_log.jsonl (the ground truth for outreach counts).
Guards: follow-ups need a logged opener for the same peer (any account); cold sends stop for an account that
Telegram limited (PeerFlood/FloodWait) and respect a per-account daily cap and a minimal gap between sends;
openers and follow-ups are refused for an account with outreach switched off (pack or ops bot), replies are not.
Login codes and 2FA passwords are typed on the VPS by the operator, never sent through a chat.

Env: KIT_HOME (required), KIT_COMPANY_NAME, KIT_TIMEZONE, KIT_TG_DAILY_CAP (default 20),
KIT_TG_MIN_GAP_S ("120,180"), KIT_TG_TEXT_GUARD_IGNORE (comma list of brand/product words),
KIT_TG_OUTREACH_DISABLED (comma list of accounts, from bot.sender.outreach_disabled),
KIT_OPS_BOT_TOKEN + KIT_OPS_CHAT_ID (pause notifications).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import json
import os
import random
import re
import sys
from getpass import getpass
from pathlib import Path

COLD_KINDS = ("opener", "message", "followup1", "followup2", "followup3")
NEW_CONTACT_KINDS = ("opener", "message")


def _home() -> Path:
    home = (os.environ.get("KIT_HOME") or os.environ.get("HERMES_HOME") or "").strip()
    if not home:
        raise SystemExit("KIT_HOME (or HERMES_HOME) must be set: each deployment keeps its own sessions and log")
    return Path(home)


def base_dir() -> Path:
    return _home() / "telegram_sender"


def config_path() -> Path:
    return base_dir() / "config.json"


def sessions_dir() -> Path:
    return base_dir() / "sessions"


def send_log_path() -> Path:
    return _home() / "outreach" / "send_log.jsonl"


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("KIT_TIMEZONE", "UTC"))
    except Exception:
        return dt.timezone.utc


# ------------------------------------------------------------------------------ send log

def _append_log(record: dict) -> None:
    p = send_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "channel": "telegram", **record},
                           ensure_ascii=False) + "\n")


def log_send(*, account, peer, kind, hypothesis, message_id, text="", provenance_override=None) -> None:
    rec = {"direction": "out", "account": account, "peer": peer, "kind": kind, "hypothesis": hypothesis,
           "message_id": message_id, "text": text}
    if provenance_override:
        rec["provenance_override"] = provenance_override
    _append_log(rec)


def log_incoming(*, account, peer, text, message_id) -> None:
    _append_log({"direction": "in", "account": account, "peer": peer, "kind": "reply", "text": text, "message_id": message_id})


def _log_rows():
    p = send_log_path()
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _norm(peer) -> str:
    return str(peer or "").strip().lstrip("@").lower()


def has_logged_opener(peer: str) -> bool:
    """Hard gate for follow-ups, enforced in the tool (an ad-hoc script can ignore a written rule, not this)."""
    target = _norm(peer)
    return any(r.get("channel") == "telegram" and r.get("direction") == "out" and r.get("kind") == "opener"
               and _norm(r.get("peer")) == target for r in _log_rows())


# ------------------------------------------------------------------------------ config and clients

def load_config() -> dict:
    p = config_path()
    if not p.exists():
        raise SystemExit(f"Config not found: {p}\nRun: python3 sender.py init")
    return json.loads(p.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    sessions_dir().mkdir(parents=True, exist_ok=True)
    p = config_path()
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def session_path(name: str) -> Path:
    return sessions_dir() / f"{name}.session"


def _session_locked(path: Path) -> bool:
    """True when another process (the listener) holds the write lock on this Telethon session DB."""
    import sqlite3
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(str(path), timeout=0.5)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
        finally:
            con.close()
        return False
    except sqlite3.OperationalError:
        return True


def _session_for_use(name: str, allow_copy: bool = True) -> str:
    """The always-on listener keeps sessions open; other commands work on a temporary copy of the same
    session on the same host. Login must write the real file (allow_copy=False)."""
    real = session_path(name)
    if not allow_copy or not _session_locked(real):
        return str(real)
    import atexit
    import shutil
    import tempfile
    tmp = Path(tempfile.gettempdir()) / f"tgs_{name}_{os.getpid()}.session"
    shutil.copy2(real, tmp)
    os.chmod(tmp, 0o600)
    atexit.register(lambda: tmp.unlink(missing_ok=True))
    print(f"[sender] {name}: session is held by the listener, using a temporary copy", file=sys.stderr)
    return str(tmp)


def _app(cfg: dict, name: str) -> dict:
    # a named app per account (cfg["apps"][name]) wins over the default app
    return (cfg.get("apps") or {}).get(name) or cfg


def get_client(name: str, cfg: dict, allow_copy: bool = True):
    from telethon import TelegramClient
    app = _app(cfg, name)
    return TelegramClient(_session_for_use(name, allow_copy), int(app["api_id"]), app["api_hash"])


def _peer_ref(peer: str):
    # Telethon resolves cached numeric entities only from an int
    return int(peer) if str(peer).strip().lstrip("-").isdigit() else peer


def _not_authorized(name: str) -> None:
    print(json.dumps({"ok": False, "name": name, "authorized": False,
                      "error": "Session exists but is not authorized - finish login for this account first."}, ensure_ascii=False))


def pending_login_path(name: str) -> Path:
    return sessions_dir() / f"{name}.pending_login.json"


# ------------------------------------------------------------------------------ login

async def login_interactive(name: str) -> None:
    """Sign in on the deployment host without putting credentials in shell history."""
    if not sys.stdin.isatty():
        raise SystemExit("interactive login requires a terminal")
    client = get_client(name, load_config(), allow_copy=False)
    try:
        await client.start(phone=lambda: input("Telegram phone: ").strip(),
                           code_callback=lambda: getpass("Telegram login code: ").strip(),
                           password=lambda: getpass("Telegram two-step password: "))
        print(f"Signed in: {name}")
    finally:
        await client.disconnect()

async def cmd_login(name, phone, code, password) -> None:
    """Non-interactive split login. Uses connect() directly: `async with client` would call Telethon's
    interactive start(), which hangs or dies on a closed stdin."""
    from telethon.errors import PhoneMigrateError, SessionPasswordNeededError
    cfg = load_config()
    client = get_client(name, cfg, allow_copy=False)
    pending = pending_login_path(name)
    await client.connect()
    try:
        if password is not None:
            try:
                await client.sign_in(password=password)
            except Exception as e:
                print(json.dumps({"ok": False, "step": "password", "error": str(e)}, ensure_ascii=False))
                return
            pending.unlink(missing_ok=True)
        elif code is not None:
            saved = json.loads(pending.read_text()) if pending.exists() else {}
            phone = phone or saved.get("phone")
            if not phone:
                print(json.dumps({"ok": False, "error": "no --phone and no pending login; start again with --phone"}))
                return
            try:
                if saved.get("phone_code_hash"):
                    await client.sign_in(phone=phone, code=code, phone_code_hash=saved["phone_code_hash"])
                else:
                    await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                print(json.dumps({"ok": True, "need_password": True, "name": name}))
                return
            except Exception as e:
                print(json.dumps({"ok": False, "step": "code", "error": str(e)}, ensure_ascii=False))
                return
            pending.unlink(missing_ok=True)
        else:
            if not phone:
                print(json.dumps({"ok": False, "error": "--phone is required for step 1"}))
                return
            try:
                sent = await client.send_code_request(phone)
            except PhoneMigrateError as e:
                # the number lives on another data center: switch there and retry once
                await client._switch_dc(e.new_dc)
                sent = await client.send_code_request(phone)
            pending.write_text(json.dumps({"phone": phone, "phone_code_hash": sent.phone_code_hash}))
            os.chmod(pending, 0o600)
            delivery = type(sent.type).__name__
            note = {"SentCodeTypeApp": "Code was pushed into an existing Telegram app session of this number, not SMS.",
                    "SentCodeTypeSms": "Code sent by SMS.",
                    "SentCodeTypeCall": "Code delivered by a voice call.",
                    "SentCodeTypeFlashCall": "Code delivered by a flash call."}.get(delivery, f"Delivered via {delivery}.")
            print(json.dumps({"ok": True, "code_sent": True, "name": name, "delivery_type": delivery, "delivery_note": note,
                              "next": "run again with --code <code> (and --password if it reports need_password)"}))
            return
        me = await client.get_me()
        print(json.dumps({"ok": True, "name": name, "id": me.id, "username": me.username, "session": str(session_path(name))}))
    finally:
        await client.disconnect()


async def cmd_qr_login_start(name: str) -> None:
    """Returns a tg://login link immediately (no waiting): open it in an app already logged into this account.
    The link expires in about 30 s; confirm with qr-login-check."""
    from telethon.tl.functions.auth import ExportLoginTokenRequest
    cfg = load_config()
    client = get_client(name, cfg, allow_copy=False)
    await client.connect()
    try:
        app = _app(cfg, name)
        res = await client(ExportLoginTokenRequest(api_id=int(app["api_id"]), api_hash=app["api_hash"], except_ids=[]))
        token = getattr(res, "token", None)
        if token is None:
            print(json.dumps({"ok": True, "confirmed": True, "name": name}))
            return
        url = "tg://login?token=" + base64.urlsafe_b64encode(token).decode("utf-8").rstrip("=")
        print(json.dumps({"ok": True, "qr_url": url, "name": name, "expires_in_seconds": 30}))
    finally:
        await client.disconnect()


async def cmd_qr_login_check(name: str) -> None:
    cfg = load_config()
    client = get_client(name, cfg, allow_copy=False)
    await client.connect()
    try:
        ok = await client.is_user_authorized()
        print(json.dumps({"ok": True, "confirmed": bool(ok), "name": name}))
    finally:
        await client.disconnect()


# ------------------------------------------------------------------------------ text checks

_TRANSLIT = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                           "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                           "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
                           "ы": "y", "э": "e", "ю": "yu", "я": "ya", "ь": "", "ъ": ""})


def _same_name(a: str, b: str) -> bool:
    def norm(x):
        x = x.lower().translate(_TRANSLIT)
        for k, v in (("ph", "f"), ("c", "k"), ("y", "i"), ("w", "v"), ("x", "ks"), ("q", "k")):
            x = x.replace(k, v)
        return x
    a, b = norm(a), norm(b)
    return bool(a and b) and a[:3] == b[:3]


def outreach_text_problems(text: str, persona: str | None) -> list:
    """Wrong self-introduction name, mixed Cyrillic/Latin text, unfilled template slots."""
    t = text or ""
    probs = []
    if re.search(r"\[(?:Имя|Name|Я|Me|компания|company|провайдер|provider|коллега|colleague)[^\]]*\]|\{[a-z_]+\}", t, re.I):
        probs.append("unfilled template slot ([Name] / {name})")
    company = os.environ.get("KIT_COMPANY_NAME", "").strip()
    first = (persona or "").strip().split()[0] if (persona or "").strip() else ""
    if first and first.isalpha():
        pats = [r"(?:\bI['’]m|\bI am|\b[Tt]his is|\b[Mm]y name is|\b[Ээ]то|\b[Мм]еня зовут|\bЯ)\s+"
                r"([A-ZА-ЯЁ][a-zа-яё]+)\s*(?:,|-|—)?\s*(?:from|из|with|представляю)\b"]
        if company:
            pats.append(r"(?:^|[!.,?]\s+|\n)([A-ZА-ЯЁ][a-zа-яё]+)\s+(?:from|из)\s+" + re.escape(company))
        for m in (m for p in pats for m in re.finditer(p, t)):
            if not _same_name(m.group(1), first):
                probs.append(f"signed as {m.group(1)!r} but the account introduces itself as {first!r}")
                break
    ignore = [w.strip() for w in os.environ.get("KIT_TG_TEXT_GUARD_IGNORE", "").split(",") if w.strip()]
    if company:
        ignore.append(company)
    clean = re.sub(r"https?://\S+|@\w+|\b\w*\d\w*\b|\b[A-Z]{2,}\b|\b[A-Z][a-z]+[A-Z]\w*\b|Telegram", " ", t)
    for w in ignore:
        clean = re.sub(re.escape(w), " ", clean, flags=re.I)
    lat, cyr = len(re.findall(r"[A-Za-z]", clean)), len(re.findall(r"[А-Яа-яЁё]", clean))
    if lat + cyr >= 40 and min(lat, cyr) / (lat + cyr) > 0.2:
        probs.append("Cyrillic and Latin text mixed in one message: write it entirely in the lead's language")
    return probs


def _text_guard(name, text, persona, allow_text) -> None:
    probs = outreach_text_problems(text or "", persona)
    if probs and not allow_text:
        raise SystemExit("REFUSED (text check): " + "; ".join(probs) +
                         f". Account {name!r} introduces itself as {persona!r}. If intended, repeat with --allow-text \"<reason>\".")


async def _persona_name(client) -> str:
    me = await client.get_me()
    return (me.first_name or "").strip()


# ------------------------------------------------------------------------------ flood lock, daily cap, gap

def _guard_path() -> Path:
    return base_dir() / "send_guard.json"


def _guard_state() -> dict:
    try:
        return json.loads(_guard_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pause_text(name, until) -> str:
    u = until.astimezone(_tz()).strftime("%d.%m %H:%M %Z")
    return (f"account {name!r} is paused until {u}: Telegram limited new messages (PeerFlood/FloodWait). "
            "New outgoing messages from it are stopped; replies to incoming messages keep working. "
            "Do not retry with pauses and do not switch to another account without the operator's explicit request.")


def _gap_bounds():
    raw = os.environ.get("KIT_TG_MIN_GAP_S", "120,180")
    try:
        lo, hi = (float(x) for x in raw.split(","))
    except ValueError:
        lo, hi = 120.0, 180.0
    return lo, max(lo, hi)


def outreach_allowed(name: str) -> bool:
    """False when the pack (bot.sender.outreach_disabled) or the operator (ops bot -> Accounts) switched
    outreach off for this account. Same runtime file as kit/bots/ops/store.py."""
    pack_off = {a.strip() for a in os.environ.get("KIT_TG_OUTREACH_DISABLED", "").split(",") if a.strip()}
    if name in pack_off:
        return False
    try:
        data = json.loads((_home() / "ops_state" / "outreach_off.json").read_text(encoding="utf-8"))
        return name not in set((data or {}).get("telegram") or [])
    except (OSError, ValueError, AttributeError):
        return True


def cold_guard(name: str, kind: str, now: dt.datetime | None = None) -> float:
    """Raises SystemExit when outreach is switched off for the account, it is flood-locked or over its daily cap
    of new contacts. Returns seconds to wait before the send (minimal gap). Replies are never limited."""
    if kind not in COLD_KINDS:
        return 0.0
    if not outreach_allowed(name):
        raise SystemExit(f"REFUSED: outreach is switched off for account {name!r} (pack bot.sender.outreach_disabled "
                         "or ops bot -> Accounts). Use an account with outreach allowed or ask the operator.")
    now = now or dt.datetime.now(dt.timezone.utc)
    st = _guard_state()
    until = (st.get("locks") or {}).get(name)
    if until:
        try:
            u = dt.datetime.fromisoformat(until)
            if u > now:
                raise SystemExit("REFUSED: " + _pause_text(name, u))
        except ValueError:
            pass
    if kind not in NEW_CONTACT_KINDS:
        return 0.0
    try:
        cap = int((st.get("caps") or {}).get(name, os.environ.get("KIT_TG_DAILY_CAP", "20")))
    except ValueError:
        cap = 20
    today = now.astimezone(_tz()).date()
    n, last = 0, None
    for r in _log_rows():
        if r.get("account") != name or r.get("kind") not in NEW_CONTACT_KINDS or r.get("direction", "out") == "in":
            continue
        try:
            ts = dt.datetime.fromisoformat(str(r["ts"]))
        except (KeyError, ValueError):
            continue
        if ts.astimezone(_tz()).date() == today:
            n += 1
            last = ts if last is None or ts > last else last
    if n >= cap:
        raise SystemExit(f"REFUSED: account {name!r} already sent {n} new-contact messages today (cap {cap}/day).")
    if last is None:
        return 0.0
    lo, hi = _gap_bounds()
    return max(0.0, min(random.uniform(lo, hi) - (now - last).total_seconds(), hi))


def _notify_ops(text: str) -> None:
    import urllib.parse
    import urllib.request
    tok, chat = os.environ.get("KIT_OPS_BOT_TOKEN"), os.environ.get("KIT_OPS_CHAT_ID")
    if not tok or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=15).read()
    except Exception:
        pass


def lock_on_flood(name: str, exc: Exception) -> None:
    if type(exc).__name__ not in ("PeerFloodError", "FloodWaitError"):
        return
    secs = max(int(getattr(exc, "seconds", 0) or 0), 86400)
    st = _guard_state()
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=secs)
    already = (st.get("locks") or {}).get(name)
    st.setdefault("locks", {})[name] = until.isoformat()
    _guard_path().parent.mkdir(parents=True, exist_ok=True)
    _guard_path().write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    if not already:
        _notify_ops("⏸ " + _pause_text(name, until).split(" Do not retry")[0])


# ------------------------------------------------------------------------------ commands

def peer_stopped(peer) -> bool:
    if __package__ in (None, ""):
        kit_dir = str(Path(__file__).resolve().parents[2])
        if kit_dir not in sys.path:
            sys.path.insert(0, kit_dir)
    from outreach import company_stop
    return ("telegram", company_stop.normalize_peer(peer)) in company_stop.snapshot(_home())


async def cmd_send(name, peer, text, kind, hypothesis, file, allow_no_opener=None, allow_text=None) -> None:
    if kind in COLD_KINDS and peer_stopped(peer):
        raise SystemExit("REFUSED: company opt-out")
    wait = cold_guard(name, kind)
    if kind.startswith("followup") and not has_logged_opener(peer) and not allow_no_opener:
        raise SystemExit(f"REFUSED: --kind {kind} for {peer!r} but no 'opener' to this peer is logged in {send_log_path()} "
                         "(any account). If a first message was verifiably sent another way, re-run with "
                         "--allow-no-opener \"<reason, who verified it>\"; the override is logged for audit.")
    if wait > 0:
        await asyncio.sleep(wait)
    cfg = load_config()
    client = get_client(name, cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            _not_authorized(name)
            return
        if text and kind in COLD_KINDS + ("reply",):
            _text_guard(name, text, await _persona_name(client), allow_text)
        try:
            entity = await client.get_entity(_peer_ref(peer))
            if kind in COLD_KINDS and any(peer_stopped(alias) for alias in (
                peer, getattr(entity, "username", ""), getattr(entity, "id", "")
            ) if alias):
                raise SystemExit("REFUSED: company opt-out")
            if file:
                fp = Path(file).expanduser()
                if not fp.is_file():
                    raise SystemExit(f"--file not found: {fp}")
                msg = await client.send_file(entity, str(fp), caption=text or None)
            else:
                msg = await client.send_message(entity, text)
        except Exception as e:
            lock_on_flood(name, e)
            raise
        log_send(account=name, peer=peer, kind=kind, hypothesis=hypothesis, message_id=msg.id, text=text or "",
                 provenance_override=allow_no_opener)
        print(json.dumps({"ok": True, "name": name, "peer": peer, "message_id": msg.id, "kind": kind,
                          "hypothesis": hypothesis, "file": str(file) if file else None}, ensure_ascii=False))
    finally:
        await client.disconnect()


async def cmd_draft(name, peer, text, allow_text=None) -> None:
    from telethon.tl.functions.messages import SaveDraftRequest
    cfg = load_config()
    client = get_client(name, cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            _not_authorized(name)
            return
        persona = await _persona_name(client)
        _text_guard(name, text, persona, allow_text)
        entity = await client.get_entity(_peer_ref(peer))
        await client(SaveDraftRequest(peer=entity, message=text))
        print(json.dumps({"ok": True, "name": name, "peer": peer, "draft_saved": True}, ensure_ascii=False))
    finally:
        await client.disconnect()


async def cmd_creategroup(name, title, photo, members) -> None:
    """Basic group for a lead/partner hand-off chat. Prints the chat id as a negative int."""
    from telethon.tl.functions.messages import CreateChatRequest, EditChatPhotoRequest
    from telethon.tl.types import InputChatUploadedPhoto
    if not members:
        raise SystemExit("creategroup needs at least one --member (Telegram cannot create a group with only the creator)")
    cfg = load_config()
    client = get_client(name, cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            _not_authorized(name)
            return
        users = [await client.get_entity(_peer_ref(m)) for m in members]
        res = await client(CreateChatRequest(users=users, title=title))
        # newer API layers wrap Updates in messages.InvitedUsers
        updates = getattr(res, "updates", res)
        chat = updates.chats[0]
        missing = [str(getattr(m, "user_id", m)) for m in (getattr(res, "missing_invitees", None) or [])]
        photo_set = False
        if photo:
            fp = Path(photo).expanduser()
            if not fp.is_file():
                raise SystemExit(f"--photo not found: {fp}")
            uploaded = await client.upload_file(str(fp))
            await client(EditChatPhotoRequest(chat_id=chat.id, photo=InputChatUploadedPhoto(file=uploaded)))
            photo_set = True
        print(json.dumps({"ok": True, "name": name, "chat_id": -chat.id, "title": title, "members": members,
                          "missing_invitees": missing, "photo_set": photo_set}, ensure_ascii=False))
    finally:
        await client.disconnect()


async def cmd_poll(name: str) -> None:
    """Log new 1:1 messages. The first sight of a peer only sets a baseline, so a fresh deployment does not
    dump the whole backlog as new. Reading via the API does not touch read receipts."""
    cfg = load_config()
    client = get_client(name, cfg)
    state_path = base_dir() / f"poll_state_{name}.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    results = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            _not_authorized(name)
            return
        async for dialog in client.iter_dialogs():
            if not dialog.is_user or dialog.message is None:
                continue
            key, top = str(dialog.entity.id), dialog.message.id
            last = state.get(key)
            if last is None:
                state[key] = top
                continue
            if top <= last:
                continue
            msgs = await client.get_messages(dialog.entity, min_id=last, limit=200)
            for m in reversed(msgs):
                if m.out or not (m.text or "").strip():
                    continue
                label = getattr(dialog.entity, "username", None) or str(dialog.entity.id)
                log_incoming(account=name, peer=label, text=m.text, message_id=m.id)
                results.append({"peer": label, "text": m.text, "message_id": m.id,
                                "date": m.date.isoformat() if m.date else None})
            state[key] = top
    finally:
        await client.disconnect()
        try:
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except OSError:
            pass
    print(json.dumps({"ok": True, "name": name, "new_messages": results}, ensure_ascii=False))


async def cmd_whoami(name: str) -> None:
    cfg = load_config()
    client = get_client(name, cfg)
    await client.connect()
    try:
        me = await client.get_me()
        if me is None:
            _not_authorized(name)
            return
        print(json.dumps({"ok": True, "name": name, "id": me.id, "username": me.username}, ensure_ascii=False))
    finally:
        await client.disconnect()


def cmd_init(api_id, api_hash, name) -> None:
    api_id = api_id or input("Telegram api_id (from my.telegram.org/apps): ").strip()
    api_hash = api_hash or getpass("Telegram api_hash: ").strip()
    cfg = json.loads(config_path().read_text(encoding="utf-8")) if config_path().exists() else {}
    if name:
        cfg.setdefault("apps", {})[name] = {"api_id": api_id, "api_hash": api_hash}
    else:
        cfg["api_id"], cfg["api_hash"] = api_id, api_hash
    save_config(cfg)
    print(f"Saved {config_path()}" + (f" (app {name!r})" if name else " (default app)"))


def cmd_list() -> None:
    sessions = sorted(p.stem for p in sessions_dir().glob("*.session")) if sessions_dir().exists() else []
    apps = []
    if config_path().exists():
        try:
            apps = sorted((json.loads(config_path().read_text(encoding="utf-8")).get("apps") or {}).keys())
        except ValueError:
            pass
    print(json.dumps({"config_exists": config_path().exists(), "config_path": str(config_path()),
                      "sessions": sessions, "named_apps": apps,
                      "outreach_allowed": {n: outreach_allowed(n) for n in sessions}}, ensure_ascii=False, indent=2))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--api-id")
    i.add_argument("--api-hash")
    i.add_argument("--name", help="named app for one account; omit for the default app")
    lg = sub.add_parser("login")
    lg.add_argument("--name", required=True)
    lg.add_argument("--interactive", action="store_true", help="prompt for phone, code and two-step password")
    lg.add_argument("--phone")
    lg.add_argument("--code")
    lg.add_argument("--password")
    for c in ("qr-login-start", "qr-login-check", "poll", "whoami"):
        sub.add_parser(c).add_argument("--name", required=True)
    s = sub.add_parser("send")
    s.add_argument("--name", required=True)
    s.add_argument("--peer", required=True)
    s.add_argument("--text")
    s.add_argument("--file")
    s.add_argument("--kind", default="message", help="opener|followup1|followup2|followup3|reply|message")
    s.add_argument("--hypothesis")
    s.add_argument("--allow-no-opener", metavar="REASON")
    s.add_argument("--allow-text", metavar="REASON")
    d = sub.add_parser("draft")
    d.add_argument("--name", required=True)
    d.add_argument("--peer", required=True)
    d.add_argument("--text", required=True)
    d.add_argument("--allow-text", metavar="REASON")
    g = sub.add_parser("creategroup")
    g.add_argument("--name", required=True)
    g.add_argument("--title", required=True)
    g.add_argument("--photo")
    g.add_argument("--member", action="append", default=[], dest="members")
    sub.add_parser("list")
    return p.parse_args(argv)


def main(argv=None) -> None:
    a = parse_args(argv)
    if a.cmd == "init":
        cmd_init(a.api_id, a.api_hash, a.name)
    elif a.cmd == "login":
        if a.interactive:
            if any(value is not None for value in (a.phone, a.code, a.password)):
                raise SystemExit("--interactive takes no --phone, --code or --password arguments")
            asyncio.run(login_interactive(a.name))
        else:
            asyncio.run(cmd_login(a.name, a.phone, a.code, a.password))
    elif a.cmd == "qr-login-start":
        asyncio.run(cmd_qr_login_start(a.name))
    elif a.cmd == "qr-login-check":
        asyncio.run(cmd_qr_login_check(a.name))
    elif a.cmd == "send":
        if not a.text and not a.file:
            raise SystemExit("send requires --text and/or --file")
        asyncio.run(cmd_send(a.name, a.peer, a.text, a.kind, a.hypothesis, a.file, a.allow_no_opener, a.allow_text))
    elif a.cmd == "draft":
        asyncio.run(cmd_draft(a.name, a.peer, a.text, a.allow_text))
    elif a.cmd == "creategroup":
        asyncio.run(cmd_creategroup(a.name, a.title, a.photo, a.members))
    elif a.cmd == "poll":
        asyncio.run(cmd_poll(a.name))
    elif a.cmd == "whoami":
        asyncio.run(cmd_whoami(a.name))
    elif a.cmd == "list":
        cmd_list()


if __name__ == "__main__":
    main()
