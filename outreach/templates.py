"""Email templates from the brand pack (mail.templates).

File format: first line `Subject: ...`, then a blank line, then the plain-text body.
Placeholders: {{first_name}}, {{company}}, {{sender_name}}. An unknown placeholder is a
config error; an empty value for a lead skips that lead (no "Hi ," emails).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
ALLOWED = {"first_name", "company", "sender_name"}


class TemplateError(Exception):
    pass


@dataclass(frozen=True)
class Template:
    id: str
    subject: str
    body: str

    @property
    def placeholders(self) -> set[str]:
        return set(PLACEHOLDER_RE.findall(self.subject + "\n" + self.body))

    def render(self, values: dict[str, str]) -> tuple[str, str]:
        missing = sorted(p for p in self.placeholders if not (values.get(p) or "").strip())
        if missing:
            raise TemplateError(f"empty {', '.join(missing)}")

        def sub(text: str) -> str:
            return PLACEHOLDER_RE.sub(lambda m: values[m.group(1)].strip(), text)
        return sub(self.subject), sub(self.body)


def parse_template(path: Path) -> Template:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    if not first.lower().startswith("subject:"):
        raise TemplateError(f"{path.name}: first line must be 'Subject: ...'")
    subject = first[len("subject:"):].strip()
    body = rest.lstrip("\n").rstrip() + "\n"
    if not subject or not body.strip():
        raise TemplateError(f"{path.name}: empty subject or body")
    tpl = Template(id=path.stem, subject=subject, body=body)
    unknown = tpl.placeholders - ALLOWED
    if unknown:
        raise TemplateError(f"{path.name}: unknown placeholders {sorted(unknown)} (allowed: {sorted(ALLOWED)})")
    return tpl


def load_templates(paths: list[Path]) -> dict[str, Template]:
    out: dict[str, Template] = {}
    for p in paths:
        t = parse_template(p)
        if t.id in out:
            raise TemplateError(f"duplicate template id {t.id!r}")
        out[t.id] = t
    if not out:
        raise TemplateError("no templates")
    return out


def pick_template(templates: dict[str, Template], email: str, requested: str = "") -> Template:
    """Lead's own `template` column wins; otherwise a stable hash of the address (same lead
    gets the same template across restarts)."""
    if requested:
        if requested not in templates:
            raise TemplateError(f"template {requested!r} not configured")
        return templates[requested]
    ids = sorted(templates)
    h = int(hashlib.sha256(email.encode("utf-8")).hexdigest(), 16)
    return templates[ids[h % len(ids)]]
