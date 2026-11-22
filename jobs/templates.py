"""Cron job templates built from the pack, and their output for Hermes.

Each job is an agent prompt that runs ONE kit command and relays its stdout verbatim: the numbers and the decisions
are made by the kit's code, never recomputed by the model. `[SILENT]` from a command means "nothing to deliver".

Templates (jobs.list[].template):
    distribute_leads  python3 -m jobs.distribute apply          (--module jobs)
    tracker_report    python3 -m jobs.report                    (--module jobs)
    budget_report     python3 -m jobs.budget report             (--module jobs), params: warn_percent, always
    search_chain      python3 -m jobs.chain --hypothesis ...    (--module jobs), params: hypotheses (required)
    followup_check    python3 -m bots.ops.followup              (--module ops; it reports to the ops chat itself)

Output formats:
    json      {"jobs": [...]} with the job fields seen in Hermes's cron store, runtime fields reset. A spec to review
              and merge, not a file to drop over a live store (that carries Hermes's own runtime state).
    commands  `hermes cron create "<expr>" "<prompt>" --name <name> --deliver <origin|local>` lines.
"""
from __future__ import annotations

import hashlib
import shlex

TEMPLATES = ("distribute_leads", "tracker_report", "budget_report", "search_chain", "followup_check")
MODULE = {"distribute_leads": "jobs", "tracker_report": "jobs", "budget_report": "jobs", "search_chain": "jobs",
          "followup_check": "ops"}

RELAY = ("Run exactly this one shell command and nothing else: {cmd} -- "
         "Send its standard output to the operator verbatim: do not recompute, reformat, shorten or add numbers, "
         "and do not use earlier conversations or session search as a source. If the output is exactly [SILENT], "
         "reply exactly [SILENT]. If the command fails, report its error in one short message and do not retry it, "
         "work around it or run anything else. Never message leads and never edit the spreadsheet yourself.")
SELF_REPORT = ("Run exactly this one shell command and nothing else: {cmd} -- "
               "It reports to the operator itself. If it exits with code 0, reply exactly [SILENT]. If it fails, report "
               "its error in one short message and do not retry it or work around it. Never message leads yourself.")


def job_command(job: dict, paths: dict) -> str:
    t = job["template"]
    p = job.get("params") or {}
    if t == "distribute_leads":
        tail = "python3 -m jobs.distribute apply"
    elif t == "tracker_report":
        tail = "python3 -m jobs.report"
    elif t == "budget_report":
        tail = "python3 -m jobs.budget report"
        if "warn_percent" in p:
            tail += f" --warn-percent {p['warn_percent']:g}"
        if p.get("always"):
            tail += " --always"
    elif t == "search_chain":
        tail = f"python3 -m jobs.chain --pack {shlex.quote(paths['pack'])}" + "".join(
            f" --hypothesis {shlex.quote(h)}" for h in p["hypotheses"])
    elif t == "followup_check":
        tail = "python3 -m bots.ops.followup"
    else:
        raise ValueError(f"unknown template {t!r}")
    return (f"cd {shlex.quote(paths['kit'])} && python3 runner/kit_env.py --pack {shlex.quote(paths['pack'])} "
            f"--module {MODULE[t]} --env-file {shlex.quote(paths['env_file'])} run -- {tail}")


def job_prompt(job: dict, paths: dict) -> str:
    tpl = SELF_REPORT if job["template"] == "followup_check" else RELAY
    return tpl.format(cmd=job_command(job, paths))


def job_name(job: dict) -> str:
    return f"kit-{job['id']}"


def stable_id(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def hermes_job(job: dict, paths: dict, origin: dict | None) -> dict:
    name = job_name(job)
    deliver = "origin" if origin else "local"
    if job["template"] == "followup_check":
        deliver = "local"
    return {
        "id": stable_id(name), "name": name, "prompt": job_prompt(job, paths),
        "skills": [], "skill": None, "model": None, "provider": None, "base_url": None,
        "script": None, "no_agent": False, "context_from": None,
        "schedule": {"kind": "cron", "expr": job["schedule"], "display": job["schedule"]},
        "schedule_display": job["schedule"],
        "repeat": {"times": None, "completed": 0},
        "enabled": bool(job.get("enabled", True)), "state": "scheduled" if job.get("enabled", True) else "paused",
        "paused_at": None, "paused_reason": None, "created_at": None, "next_run_at": None, "last_run_at": None,
        "last_status": None, "last_error": None, "last_delivery_error": None,
        "deliver": deliver,
        "origin": dict(origin, thread_id=None, user_id=None) if (origin and deliver == "origin") else None,
        "enabled_toolsets": ["terminal"], "workdir": paths["kit"], "fire_claim": None,
    }


def to_commands(jobs: list[dict], paths: dict, origins: dict) -> list[str]:
    out = []
    for job in jobs:
        if not job.get("enabled", True):
            out.append(f"# {job_name(job)}: enabled: false in the pack, not created")
            continue
        deliver = "origin" if origins.get(job["id"]) and job["template"] != "followup_check" else "local"
        out.append(f"hermes cron create {shlex.quote(job['schedule'])} {shlex.quote(job_prompt(job, paths))} "
                   f"--name {shlex.quote(job_name(job))} --deliver {deliver}")
    return out
