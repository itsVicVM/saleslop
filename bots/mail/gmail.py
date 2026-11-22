"""Gmail API backend over HTTPS (for hosts where outbound SMTP is blocked, and for Google Workspace mailboxes).

OAuth: authorization-code flow with PKCE for a Desktop-app client of the CLIENT's own Google Cloud project.
The redirect goes to http://localhost: the browser shows "cannot connect", and the operator copies the address
bar (…?code=…) back to the bot or the CLI. Tokens live in KIT_HOME/mail_state/tokens/<mailbox>.json (chmod 600).

The HTTP layer is injected: `http(method, url, *, headers=None, params=None, data=None, json_body=None)`
-> (status, parsed JSON or text). Tests use a fake; nothing here was run against Google in this kit.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCOPES = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
REDIRECT = "http://localhost"


class AuthError(Exception):
    """Refresh token revoked or expired (an app in Testing mode expires tokens after 7 days): reconnect."""


class SendUnknown(Exception):
    """The request may have reached Google (timeout, connection reset): do not retry blindly."""


def urllib_http(method, url, *, headers=None, params=None, data=None, json_body=None, timeout=40):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body, hdrs = None, dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw.decode("utf-8"))
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def load_client(path) -> dict:
    """client_id/client_secret from the JSON Google Cloud gives for an OAuth client ("installed" or "web")."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    cl = d.get("installed") or d.get("web") or {}
    if not cl.get("client_id"):
        raise ValueError(f"{path}: no client_id (expected the OAuth client JSON from Google Cloud)")
    return cl


def auth_url(client: dict, login_hint: str = "") -> tuple[str, str]:
    """(url to open, PKCE verifier to keep until the code comes back)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"client_id": client["client_id"], "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES,
         "access_type": "offline", "prompt": "consent", "state": secrets.token_urlsafe(8),
         "code_challenge": challenge, "code_challenge_method": "S256"}
    if login_hint:
        q["login_hint"] = login_hint
    return AUTH_URL + "?" + urllib.parse.urlencode(q), verifier


def code_from_redirect(text: str) -> tuple[str | None, str | None]:
    """(code, error) from the pasted address bar or a bare code."""
    text = (text or "").strip()
    parsed = urllib.parse.urlparse(text)
    q = urllib.parse.parse_qs(parsed.query) if parsed.query else {}
    if q.get("error"):
        return None, q["error"][0]
    if q.get("code"):
        return q["code"][0], None
    if text and " " not in text and "://" not in text:
        return text, None
    return None, "no code in the pasted text"


class TokenStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def path(self, mailbox_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in mailbox_id)
        return self.dir / f"{safe}.json"

    def load(self, mailbox_id: str) -> dict | None:
        try:
            return json.loads(self.path(mailbox_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, mailbox_id: str, tok: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        p = self.path(mailbox_id)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tok, fh)

    def delete(self, mailbox_id: str) -> None:
        try:
            self.path(mailbox_id).unlink()
        except OSError:
            pass


class Gmail:
    def __init__(self, client: dict | None, tokens: TokenStore, http=None, clock=time.time):
        self.client = client or {}
        self.tokens = tokens
        self.http = http or urllib_http
        self.clock = clock

    # ------------------------------------------------------------------ OAuth
    def exchange(self, code: str, verifier: str) -> dict:
        st, j = self.http("POST", TOKEN_URL, data={
            "code": code, "client_id": self.client.get("client_id", ""), "client_secret": self.client.get("client_secret", ""),
            "redirect_uri": REDIRECT, "grant_type": "authorization_code", "code_verifier": verifier})
        if st != 200 or not isinstance(j, dict) or "access_token" not in j:
            err = j.get("error_description") or j.get("error") if isinstance(j, dict) else None
            raise AuthError(err or f"HTTP {st}")
        return {"access_token": j["access_token"], "refresh_token": j.get("refresh_token", ""),
                "expires_at": self.clock() + int(j.get("expires_in", 3600)) - 60, "scope": j.get("scope", "")}

    def profile_address(self, access_token: str) -> str:
        st, j = self.http("GET", API + "/profile", headers={"Authorization": "Bearer " + access_token})
        return (j.get("emailAddress") or "").lower() if st == 200 and isinstance(j, dict) else ""

    def access(self, mailbox_id: str) -> str:
        tok = self.tokens.load(mailbox_id)
        if not tok:
            raise AuthError("no token for this mailbox")
        if tok.get("expires_at", 0) > self.clock():
            return tok["access_token"]
        st, j = self.http("POST", TOKEN_URL, data={
            "client_id": self.client.get("client_id", ""), "client_secret": self.client.get("client_secret", ""),
            "refresh_token": tok.get("refresh_token", ""), "grant_type": "refresh_token"})
        if st != 200 or not isinstance(j, dict) or "access_token" not in j:
            raise AuthError((j.get("error_description") or j.get("error")) if isinstance(j, dict) else f"HTTP {st}")
        tok.update(access_token=j["access_token"], expires_at=self.clock() + int(j.get("expires_in", 3600)) - 60)
        self.tokens.save(mailbox_id, tok)
        return tok["access_token"]

    def revoke(self, mailbox_id: str) -> None:
        tok = self.tokens.load(mailbox_id) or {}
        t = tok.get("refresh_token") or tok.get("access_token")
        if t:
            try:
                self.http("POST", REVOKE_URL, params={"token": t})
            except OSError:
                pass
        self.tokens.delete(mailbox_id)

    def _call(self, mailbox_id, method, path, **kw):
        st, j = self.http(method, API + path, headers={"Authorization": "Bearer " + self.access(mailbox_id)}, **kw)
        if st == 401:
            raise AuthError("unauthorized")
        return st, j

    # ------------------------------------------------------------------ mail
    def inbox_ids(self, mailbox_id: str, limit: int = 25) -> list[str] | None:
        """Newest first, as the API returns them. None on a non-auth failure."""
        st, j = self._call(mailbox_id, "GET", "/messages", params={"q": "in:inbox", "maxResults": limit})
        if st != 200 or not isinstance(j, dict):
            print(f"[mail] gmail list {mailbox_id} failed: {st}", file=sys.stderr)
            return None
        return [m["id"] for m in j.get("messages", []) if m.get("id")]

    def read_raw(self, mailbox_id: str, msg_id: str) -> tuple[bytes, str] | None:
        st, j = self._call(mailbox_id, "GET", f"/messages/{msg_id}", params={"format": "raw"})
        if st != 200 or not isinstance(j, dict) or "raw" not in j:
            return None
        raw = j["raw"]
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), j.get("threadId", "")

    def send_raw(self, mailbox_id: str, raw: bytes, thread_id: str = "") -> tuple[str, str]:
        """Keep ambiguous server and network failures for operator reconciliation."""
        body = {"raw": base64.urlsafe_b64encode(raw).decode()}
        if thread_id:
            body["threadId"] = thread_id
        try:
            st, j = self._call(mailbox_id, "POST", "/messages/send", json_body=body)
        except AuthError as e:
            return "failed", f"Gmail auth: {e}"
        except (OSError, ValueError) as e:
            return "unknown", f"Gmail: {e}"[:300]
        if st == 200:
            return "sent", ""
        return ("unknown" if st >= 500 or st == 408 else "failed", f"Gmail API {st}: {str(j)[:160]}")
