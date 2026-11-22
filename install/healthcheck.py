#!/usr/bin/env python3
"""Offline deployment diagnostics. Does not connect, send or load Telegram sessions."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "runner"))
import kit_env


def check(root: Path, configured=False) -> dict:
    checks = {}
    pack_path = root / "config/pack"
    try:
        pack, base = kit_env.load_pack(pack_path)
        errors = (kit_env.check_files(pack, base) + kit_env.check_hypotheses(pack)
                  + kit_env.check_ops_sections(pack) + kit_env.check_mail_sections(pack)
                  + kit_env.check_jobs_sections(pack) + kit_env.check_operations(pack))
        checks["pack_structure"] = not errors
    except (kit_env.PackError, ValueError, OSError):
        return {"ok": False, "checks": {"pack_structure": False}, "connectivity": "not_tested"}
    checks["state_directory"] = (root / "state").is_dir()
    checks["unit_rendered"] = (root / "systemd/leadgen-ops.service").is_file()
    checks["report_service_rendered"] = (root / "systemd/leadgen-report.service").is_file()
    checks["report_timer_rendered"] = (root / "systemd/leadgen-report.timer").is_file()
    if configured:
        env_path = root / "config/client.env"
        checks["client_env"] = env_path.is_file()
        if env_path.is_file():
            env = {**kit_env.load_env_file(env_path), "KIT_HOME": str(root / "state")}
            _, _, search_errors = kit_env.build_env(pack, env)
            _, _, ops_errors = kit_env.build_ops_env(pack, env, base)
            outreach_errors = []
            if kit_env.get(pack, "mail.outreach") is not None:
                _, _, outreach_errors = kit_env.build_outreach_env(pack, env, base)
            checks["resolved_config"] = not (search_errors + ops_errors + outreach_errors)
            checks["llm_key_present"] = bool(env.get("OPENAI_API_KEY"))
            accounts = kit_env.ops_accounts(pack)
            checks["telegram_accounts"] = bool(accounts)
            checks["session_files_present"] = bool(accounts) and all(
                (root / "state/telegram_sender/sessions" / f"{name}.session").is_file() for name in accounts
            )
        for name in ("yaml", "telethon", "httpx"):
            checks["dependency_" + name] = importlib.util.find_spec(name) is not None
        python_path = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        checks["venv_python"] = (root / ".venv" / python_path).is_file()
        checks["telegram_setup_present"] = (root / "state/telegram_sender/config.json").is_file()
        if kit_env.get(pack, "mail.provider") == "himalaya":
            checks["himalaya_binary"] = shutil.which("himalaya") is not None
        if os.name == "posix" and env_path.exists():
            checks["env_permissions"] = env_path.stat().st_mode & 0o077 == 0
    return {"ok": all(checks.values()), "checks": checks, "connectivity": "not_tested",
            "live_enabled": (root / "config/ENABLE_LIVE").is_file(),
            "scope": "offline_configured" if configured else "offline_static"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--configured", action="store_true")
    args = parser.parse_args(argv)
    result = check(args.root, args.configured)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
