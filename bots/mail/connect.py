#!/usr/bin/env python3
"""Connect, inspect and tune the mailboxes of the mail bot (same state as /settings -> Mail in the ops bot).
Output is JSON for the operator or the agent; passwords and tokens are never printed.

Run from kit/ through the runner (KIT_OPS_CONFIG and KIT_HOME come from the pack and .env):
  python3 runner/kit_env.py --pack <pack> --module ops --env-file .env run -- python3 -m bots.mail.connect status
  ... connect check                                   # what is missing (Himalaya accounts, OAuth client)
  ... connect start --addr name@company.example [--provider google|imap] [--preset ID]
  ... connect finish-google --addr ... --redirect "<pasted http://localhost/?code=... address>"
  printf %s "$PASSWORD" | ... connect finish-imap --addr ... [--preset ID]     (password ONLY via stdin, on the VPS)
  ... connect set --mailbox ID [--notify on|off] [--reply draft|off] [--outreach on|off]
  ... connect disconnect --mailbox ID                 (only mailboxes added with this CLI or the bot)

Hosted IMAP/SMTP settings come from mail.bot.connect.presets in the pack (no provider is assumed). Google mailboxes
(Gmail or Google Workspace, found by MX) use OAuth with the client's own OAuth client (mail.bot.gmail_client_secret).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import Mailbox

OAUTH_PENDING = "oauth_pending.json"
OAUTH_TTL_S = 1800


def mx_hosts(domain: str) -> list[str]:
    """MX hostnames of a domain, lowercase, without the trailing dot. [] when nothing could be resolved."""
    for argv in (["dig", "+short", "MX", domain], ["nslookup", "-type=MX", domain]):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout.lower()
        except (OSError, subprocess.SubprocessError):
            continue
        hosts = re.findall(r"(?:^\d+\s+|mail exchanger = (?:\d+\s+)?)([a-z0-9.-]+)\.?\s*$", out, re.M)
        if hosts:
            return [h.rstrip(".") for h in hosts]
    return []


def pick_preset(presets: list, domain: str, preset_id: str | None = None):
    if preset_id:
        return next((p for p in presets if p.get("id") == preset_id), None)
    hosts = mx_hosts(domain)
    for p in presets:
        if any(any(m.lower() in h for h in hosts) for m in p.get("mx_contains") or []):
            return p
    return None


def _slug(addr: str, taken: set) -> str:
    base = "c-" + re.sub(r"[^a-z0-9]+", "-", addr.lower().split("@")[0]).strip("-")[:18]
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}-{n}", n + 1
    return sid


def _secret_dir(him) -> Path:
    return him.config_path.parent / "secrets"


def remove_secret(him, account: str) -> None:
    """Deletes the password file of an account THIS tool created; never a file another account references."""
    p = _secret_dir(him) / account
    try:
        cfg = him.config_path.read_text(encoding="utf-8")
    except OSError:
        cfg = ""
    if str(p) in cfg:  # still referenced by some account section: keep it
        return
    try:
        p.unlink()
    except OSError:
        pass


def finish_google(engine, addr: str, redirect_text: str, verifier: str) -> tuple[bool, str]:
    """Pasted redirect (or code) -> tokens -> the account that actually signed in -> registered mailbox."""
    from . import gmail as g
    t = engine.t
    code, err = g.code_from_redirect(redirect_text)
    if err:
        return False, t["connect_denied"].format(err=err)
    try:
        client = g.load_client(engine.cfg.gmail_client_secret)
        gm = engine.gmail or g.Gmail(client, g.TokenStore(engine.cfg.state_dir / "tokens"))
        gm.client = client
        tok = gm.exchange(code, verifier)
    except (OSError, ValueError, g.AuthError) as e:
        return False, t["connect_code_bad"].format(err=e)
    real = gm.profile_address(tok["access_token"])
    if not real:
        return False, t["connect_no_profile"]
    # the typed address can differ from the account that signed in: connect the one that signed in
    existing = next((m for m in engine.mailboxes() if m.address == real), None)
    mid = existing.id if existing else _slug(real, {m.id for m in engine.mailboxes()})
    gm.tokens.save(mid, tok)
    if not existing:
        engine.register_connected(Mailbox(id=mid, address=real, provider="gmail_api", source="connect",
                                          notify=True, reply="off", outreach=False))
    engine.gmail = gm
    return True, mid


def connect_imap(engine, addr: str, password: str, preset: dict) -> tuple[bool, str]:
    him = engine.himalaya
    pw = re.sub(r"\s+", "", password or "")
    if len(pw) < 8:
        return False, "password too short (expected the mailbox password, at least 8 characters)"
    taken = {m.id for m in engine.mailboxes()}
    if any(m.address == addr for m in engine.mailboxes()):
        return False, "this address is already connected"
    mid = _slug(addr, taken)
    account = "kit-" + mid
    if him.has_account(account):
        return False, f"Himalaya already has an account {account!r}"
    sd = _secret_dir(him)
    sd.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(sd, 0o700)
    except OSError:
        pass
    secret = sd / account
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(pw)
    before = him.config_path.read_text(encoding="utf-8") if him.config_path.exists() else ""
    him.add_account(account, addr, preset["imap"], preset["smtp"], bool(preset.get("starttls", True)), secret)
    ids = him.list_ids(account, 5)
    if ids is None:  # login failed: restore the config and drop the secret
        him.config_path.write_text(before, encoding="utf-8")
        try:
            secret.unlink()
        except OSError:
            pass
        return False, "could not log in over IMAP (wrong password or IMAP disabled); nothing was kept"
    engine.register_connected(Mailbox(id=mid, address=addr, provider="himalaya", source="connect", notify=True,
                                      reply="off", outreach=False, himalaya_account=account))
    engine.state.update_box(mid, last_uid=ids[-1] if ids else 0)
    engine._refresh_send_status(engine.mailbox(mid))
    return True, mid


# ---------------------------------------------------------------------------------------------- CLI

class _SilentOps:
    """The CLI talks JSON to stdout, not to the ops chat."""

    def send(self, *a, **k):
        return None

    def edit(self, *a, **k):
        return None

    def delete(self, *a, **k):
        return False

    def answer(self, *a, **k):
        return None


def build_engine(env=None, ops=None):
    from ..ops.config import OpsConfig
    from ..ops.llm import ChatLLM
    from ..ops.store import Store
    from . import gmail as g
    from .config import MailConfig
    from .engine import MailEngine
    from .himalaya import Himalaya
    env = os.environ if env is None else env
    ops_cfg = OpsConfig.from_env(env)
    cfg = MailConfig.from_dict(ops_cfg.mail, home=ops_cfg.home, env=env)
    client = None
    if cfg.gmail_client_secret:
        try:
            client = g.load_client(cfg.gmail_client_secret)
        except (OSError, ValueError) as e:
            print(f"[mail] OAuth client unreadable: {e}", file=sys.stderr)
    return MailEngine(cfg, ops_cfg, llm=ChatLLM(env.get("OPENAI_API_KEY"), ops_cfg.llm_model, ops_cfg.llm_base_url),
                      ops=ops or _SilentOps(), himalaya=Himalaya(cfg.himalaya_bin, config_path=cfg.himalaya_config or None),
                      gmail=g.Gmail(client, g.TokenStore(cfg.state_dir / "tokens")),
                      outreach=Store(ops_cfg.state_dir, ops_cfg.send_log_path))


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def main(argv=None, engine=None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description="mail bot mailboxes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("check")
    s = sub.add_parser("start")
    s.add_argument("--addr", required=True)
    s.add_argument("--provider", choices=["google", "imap"])
    s.add_argument("--preset")
    g_ = sub.add_parser("finish-google")
    g_.add_argument("--addr", required=True)
    g_.add_argument("--redirect", required=True)
    i_ = sub.add_parser("finish-imap")
    i_.add_argument("--addr", required=True)
    i_.add_argument("--preset")
    t = sub.add_parser("set")
    t.add_argument("--mailbox", required=True)
    t.add_argument("--notify", choices=["on", "off"])
    t.add_argument("--reply", choices=["draft", "off"])
    t.add_argument("--outreach", choices=["on", "off"])
    d = sub.add_parser("disconnect")
    d.add_argument("--mailbox", required=True)
    a = ap.parse_args(argv)
    try:
        eng = engine or build_engine()
    except Exception as e:  # config errors: tell what is missing, exit non-zero
        _out({"ok": False, "error": str(e)})
        return 2

    if a.cmd == "status":
        _out({"mailboxes": [{"id": m.id, "address": m.address, "provider": m.provider, "source": m.source,
                             "notify": eng.notify_on(m), "reply": eng.reply_mode(m), "outreach": eng.outreach_on(m),
                             "outreach_in_pack": m.outreach, "can_send": eng.state.box(m.id).get("send_ok", None)
                             if m.provider == "himalaya" else True} for m in eng.mailboxes()]})
        return 0
    if a.cmd == "check":
        problems = eng.cfg.problems()
        _out({"ok": not problems, "problems": problems})
        return 0 if not problems else 1
    if a.cmd == "set":
        mb = eng.mailbox(a.mailbox)
        if not mb:
            _out({"ok": False, "error": "no such mailbox", "known": [m.id for m in eng.mailboxes()]})
            return 1
        if a.notify:
            eng.state.update_box(mb.id, notify=a.notify == "on")
        if a.reply:
            eng.state.update_box(mb.id, reply=a.reply)
        if a.outreach:
            if not mb.outreach and a.outreach == "on":
                _out({"ok": False, "error": "outreach is off for this mailbox in the pack (mail.accounts[].outreach)"})
                return 1
            eng.outreach.set_outreach_off("mail", mb.id, a.outreach == "off")
        _out({"ok": True, "mailbox": {"id": mb.id, "notify": eng.notify_on(mb), "reply": eng.reply_mode(mb),
                                      "outreach": eng.outreach_on(mb)}})
        return 0
    if a.cmd == "disconnect":
        ok = eng.disconnect(a.mailbox)
        _out({"ok": ok, "note": None if ok else "only mailboxes added with this CLI or the bot can be disconnected here"})
        return 0 if ok else 1

    addr = a.addr.strip().lower()
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", addr):
        _out({"ok": False, "error": "not an email address"})
        return 1
    domain = addr.split("@")[1]
    pending = eng.state.load(OAUTH_PENDING, {})
    if a.cmd == "start":
        prov = a.provider or ("imap" if a.preset else ("google" if eng.detect_google(domain) else "imap"))
        if prov == "google":
            from . import gmail as g
            try:
                client = g.load_client(eng.cfg.gmail_client_secret)
            except (OSError, ValueError) as e:
                _out({"ok": False, "error": f"OAuth client not configured (mail.bot.gmail_client_secret): {e}"})
                return 1
            url, verifier = g.auth_url(client, addr)
            pending[addr] = {"verifier": verifier, "at": time.time()}
            eng.state.save(OAUTH_PENDING, pending)
            _out({"provider": "google", "auth_url": url,
                  "tell_user": ["Open the link and sign in with this mailbox's Google account.",
                                "Allow reading and sending mail.",
                                "The browser then shows 'cannot connect to localhost': expected. Copy the whole address bar "
                                "(http://localhost/?code=...) and send it back."],
                  "next": "finish-google --addr ... --redirect '<address>'"})
            return 0
        preset = pick_preset(eng.cfg.presets, domain, a.preset)
        if not preset:
            _out({"provider": "unknown", "presets": [p.get("id") for p in eng.cfg.presets],
                  "ask_user": f"Where is the mail of {domain} hosted? Repeat start with --provider google or --preset <id> "
                              "(presets: mail.bot.connect.presets in the pack)."})
            return 1
        _out({"provider": "imap", "preset": preset.get("id"),
              "next": "on the server: printf %s '<password>' | python3 -m bots.mail.connect finish-imap --addr ... "
                      f"--preset {preset.get('id')}  (never paste the password into a chat)"})
        return 0
    if a.cmd == "finish-google":
        p = pending.get(addr)
        if not p or time.time() - p["at"] > OAUTH_TTL_S:
            _out({"ok": False, "error": "no active sign-in for this address (or older than 30 minutes): run start again"})
            return 1
        ok, res = finish_google(eng, addr, a.redirect, p["verifier"])
        if ok:
            pending.pop(addr, None)
            eng.state.save(OAUTH_PENDING, pending)
        _out({"ok": ok, ("mailbox" if ok else "error"): res})
        return 0 if ok else 1
    if a.cmd == "finish-imap":
        preset = pick_preset(eng.cfg.presets, domain, a.preset)
        if not preset:
            _out({"ok": False, "error": "no preset for this domain: pass --preset <id>"})
            return 1
        ok, res = connect_imap(eng, addr, (stdin or sys.stdin).read(), preset)
        _out({"ok": ok, ("mailbox" if ok else "error"): res,
              "can_send": eng.state.box(res).get("send_ok") if ok else None})
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
