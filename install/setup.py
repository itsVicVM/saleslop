"""Prepare the configuration and state next to a release checkout named kit."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys


KIT = Path(__file__).resolve().parents[1]


def setup(root: Path, user: str, apply: bool = False) -> dict:
    root = root.resolve()
    if root / "kit" != KIT:
        raise ValueError("--root must contain this checkout in a directory named kit")
    if os.name == "posix" and not re.fullmatch(r"/[A-Za-z0-9_/-]+", root.as_posix()):
        raise ValueError("use a Linux deployment path without spaces or systemd specifiers")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or user == "root":
        raise ValueError("--user must name a non-root service account")

    folders = ("config", "config/pack", "state", "systemd")
    files = {
        "config/pack/pack.json": (KIT / "config/pack.template.json").read_bytes(),
        "config/pack/first-touch.txt": (KIT / "config/first-touch.txt").read_bytes(),
        "config/client.env.example": (
            f"KIT_HOME={root.as_posix()}/state\nKIT_OUTREACH_STATE_DIR={root.as_posix()}/state/outreach_state\n"
            "OPS_BOT_TOKEN=\nOPS_OWNER_CHAT_ID=\nOPENAI_API_KEY=\n"
        ).encode("utf-8"),
    }
    for unit in ("ops.service", "report.service", "report.timer"):
        template = (KIT / "install" / f"{unit}.in").read_text(encoding="utf-8")
        files[f"systemd/leadgen-{unit}"] = template.replace("@ROOT@", root.as_posix()).replace("@USER@", user).encode()
    create, keep = [], []
    for name in (*folders, *files):
        path = root / name
        if path.resolve() != path or path.is_symlink():
            raise ValueError(f"linked deployment path: {name}")
        if not path.exists():
            create.append(name)
            continue
        expected_type = path.is_dir() if name in folders else path.is_file()
        if not expected_type:
            raise ValueError(f"unexpected path type: {name}")
        keep.append(name)
    if apply:
        for name in folders:
            (root / name).mkdir(mode=0o700, exist_ok=True)
        for name, data in files.items():
            if name in create:
                with (root / name).open("xb") as output:
                    output.write(data)
    return {"mode": "apply" if apply else "plan", "root": str(root), "create": create, "keep": keep}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--user", default="leadgen")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(setup(args.root, args.user, args.apply), indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
