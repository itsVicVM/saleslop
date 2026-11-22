"""Microsoft 365 mail cards over the public-cloud Microsoft Graph API."""
import base64
import os
import time
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx


GRAPH = "https://graph.microsoft.com"


class GraphMailbox:
    def __init__(self, mailbox, env=None, request=httpx.request):
        self.prefix = mailbox.env_prefix
        self.env = os.environ if env is None else env
        self.request = request
        self.path = GRAPH + "/v1.0/users/" + quote(mailbox.address, safe="")
        self.token = ""
        self.expires = 0

    def _headers(self):
        if time.monotonic() >= self.expires:
            tenant = UUID(self.env[f"{self.prefix}_TENANT_ID"])
            response = self.request("POST", f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                                    data={"grant_type": "client_credentials",
                                          "client_id": self.env[f"{self.prefix}_CLIENT_ID"],
                                          "client_secret": self.env[f"{self.prefix}_CLIENT_SECRET"],
                                          "scope": GRAPH + "/.default"}, timeout=20)
            if response.status_code != 200:
                raise ValueError(f"Microsoft sign-in failed (HTTP {response.status_code})")
            token = response.json()
            self.token = token["access_token"]
            self.expires = time.monotonic() + max(0, int(token["expires_in"]) - 60)
        return {"Authorization": "Bearer " + self.token}

    def page(self, cursor):
        url = cursor or self.path + "/mailFolders/inbox/messages/delta?changeType=created&$select=id"
        if urlsplit(url).netloc != "graph.microsoft.com" or urlsplit(url).scheme != "https":
            raise ValueError("Unexpected Graph continuation URL")
        response = self.request("GET", url, headers=self._headers(), timeout=30)
        if response.status_code != 200:
            raise ValueError(f"Graph inbox delta failed (HTTP {response.status_code})")
        page = response.json()
        next_page = page.get("@odata.nextLink")
        cursor = next_page or page["@odata.deltaLink"]
        return [m["id"] for m in page["value"] if "@removed" not in m], cursor, bool(next_page)

    def raw(self, mid):
        response = self.request("GET", self.path + "/messages/" + quote(mid, safe="") + "/$value",
                                headers=self._headers(), timeout=30)
        if response.status_code == 404:
            return None  # Message was removed before collection.
        if response.status_code != 200:
            raise ValueError(f"Graph message read failed (HTTP {response.status_code})")
        return response.content

    def send(self, mime):
        try:
            headers = self._headers()
        except (KeyError, ValueError, httpx.HTTPError) as exc:
            return "failed", "Microsoft authentication failed: " + type(exc).__name__
        try:
            response = self.request("POST", self.path + "/sendMail",
                                    headers={**headers, "Content-Type": "text/plain"},
                                    content=base64.b64encode(mime), timeout=30)
        except httpx.HTTPError:
            return "unknown", "Check Sent Items before retrying"
        if response.status_code == 202:
            return "sent", ""
        return ("unknown" if response.status_code >= 500 or response.status_code == 408 else "failed",
                f"Graph sendMail HTTP {response.status_code}")
