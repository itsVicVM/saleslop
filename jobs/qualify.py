"""Turn a discovery run into an evidence review and record one operator verdict."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner.kit_env import PackError, discovery_config, load_pack


def review_packet(pack_path: Path, run: Path) -> dict:
    pack, base = load_pack(pack_path)
    config = json.loads((run / "config.json").read_text(encoding="utf-8"))
    hypothesis = discovery_config(pack, config["run_id"], 0)
    if hypothesis["hypothesis"] != config["hypothesis"]:
        raise ValueError("pack hypothesis changed since discovery")
    targeting = pack["targeting"]
    rules = {}
    for field in ("verdict_prompt_file", "fit_rubric_file"):
        if targeting.get(field):
            rules[field] = (base / targeting[field]).read_text(encoding="utf-8")
    companies = []
    with (run / "candidates.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("validation_status") in {"duplicate", "rejected"}:
                continue
            companies.append({key: item.get(key) for key in (
                "domain", "sample_url", "source_queries", "validation_status", "score",
                "reasons", "snippet", "fetched_title", "fetched_meta_description",
                "evidence_url", "page_excerpt", "fetch_source",
            )})
    return {"hypothesis": config["run_id"], "hypothesis_text": config["hypothesis"],
            "rules": rules, "companies": companies}


def record(packet: dict, args) -> dict:
    domain = args.domain.strip().lower()
    if not any(row["domain"] == domain for row in packet["companies"]):
        raise ValueError("domain is not in this qualification queue")
    if not args.evidence.strip() or not args.reason.strip():
        raise ValueError("supply evidence and a reason for the decision")
    source = urlsplit(args.source)
    if source.scheme not in ("http", "https") or not source.hostname:
        raise ValueError("supply the URL of the evidence")
    if args.accept and (args.verdict not in ("merchant", "partner") or
                        not args.company.strip() or not args.role.strip()):
        raise ValueError("acceptance requires a merchant or partner, a company name and a contact role")
    return {"domain": domain, "hypothesis": packet["hypothesis"], "verdict": args.verdict,
            "decision": "accepted" if args.accept else "reviewed", "source_url": args.source,
            "evidence": args.evidence.strip(), "reason": args.reason.strip(),
            "company": args.company.strip(), "role": args.role.strip()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    queue = sub.add_parser("queue")
    queue.add_argument("--pack", required=True, type=Path)
    queue.add_argument("--run-dir", required=True, type=Path)
    decide = sub.add_parser("record")
    decide.add_argument("--packet", required=True, type=Path)
    decide.add_argument("--out", required=True, type=Path)
    decide.add_argument("--verdict", choices=("merchant", "partner", "no", "unknown"), required=True)
    for key in ("domain", "source", "evidence", "reason"):
        decide.add_argument("--" + key, required=True)
    decide.add_argument("--company", default="")
    decide.add_argument("--role", default="")
    decide.add_argument("--accept", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "queue":
            packet = review_packet(args.pack, args.run_dir)
            target = args.run_dir / "qualification.json"
            with tempfile.NamedTemporaryFile(dir=args.run_dir, delete=False) as temp:
                path = Path(temp.name)
            try:
                path.write_text(json.dumps(packet, indent=2, ensure_ascii=False), encoding="utf-8")
                path.replace(target)
            finally:
                path.unlink(missing_ok=True)
            print(f"{len(packet['companies'])} candidates for review: {target}")
        else:
            packet = json.loads(args.packet.read_text(encoding="utf-8"))
            decision = record(packet, args)
            with args.out.open("x", encoding="utf-8") as stream:
                json.dump(decision, stream, indent=2, ensure_ascii=False)
            print(f"{decision['decision']}: {args.out}")
    except (OSError, KeyError, ValueError, PackError) as exc:
        print(f"Qualification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
