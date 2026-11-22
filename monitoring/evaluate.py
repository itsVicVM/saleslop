"""Run an explainable signal evaluation against a Telegram Desktop export."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

from .reports import build_history_report, signal_report
from .signals import SignalProfile, find_signals
from .store import StoredMessage
from .telegram_export import load_telegram_export


def evaluate_export(
    export_path: str | Path,
    profile: SignalProfile,
    *,
    limit: int = 50,
    semantic_scorer=None,
) -> dict[str, Any]:
    export = load_telegram_export(export_path)
    messages = [
        StoredMessage(
            chat_id=item.chat_id,
            chat_name=item.chat_name,
            message_id=item.message_id,
            kind=item.kind,
            date=item.date,
            edited=item.edited,
            sender=item.sender,
            sender_id=item.sender_id,
            text=item.text,
            reply_to_message_id=item.reply_to_message_id,
        )
        for item in export.messages
    ]
    history = build_history_report(messages)
    candidates = find_signals(messages, profile, limit=limit, semantic_scorer=semantic_scorer)
    result = {
        "source": {
            "chat_name": export.chat_name,
            "chat_type": export.chat_type,
            "messages": history.total_messages,
            "ordinary_messages": history.ordinary_messages,
            "service_messages": history.service_messages,
            "first_date": history.first_date,
            "last_date": history.last_date,
        },
        "profile": {
            "name": profile.name,
            "positive_terms": list(profile.positive_terms),
            "intent_terms": list(profile.intent_terms),
            "negative_terms": list(profile.negative_terms),
            "min_score": profile.min_score,
        },
        "summary": signal_report(candidates),
        "signals": [
            {
                "rank": rank,
                "message_id": candidate.message.message_id,
                "date": candidate.message.date,
                "text": candidate.message.text,
                "score": candidate.score,
                "semantic_score": candidate.semantic_score,
                "reasons": list(candidate.reasons),
                "review_label": None,
            }
            for rank, candidate in enumerate(candidates, start=1)
        ],
        "review_labels": ["good", "maybe", "irrelevant", "competitor", "spam"],
    }
    return result


def _split(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _read_profile(path: Path, name: str, min_score: int) -> SignalProfile:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object in the profile")
    terms = {}
    for field in ("positive_terms", "intent_terms", "negative_terms"):
        values = payload.get(field, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{field} must be an array of phrases")
        terms[field] = tuple(values)
    try:
        threshold = int(payload.get("min_score", min_score))
    except (ValueError, TypeError) as exc:
        raise ValueError("min_score must be an integer") from exc
    return SignalProfile(name=str(payload.get("name", name)), min_score=threshold, **terms)


def _write_report(output: Path, rendered: str, inputs: list[Path]) -> None:
    if output.exists() and any(output.samefile(source) for source in inputs):
        raise ValueError("--output must differ from the export and profile files")
    stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False)
    pending = Path(stream.name)
    try:
        with stream:
            stream.write(rendered + "\n")
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, help="Telegram Desktop result.json")
    parser.add_argument("--profile", help="JSON profile with name/positive_terms/intent_terms/negative_terms")
    parser.add_argument("--positive", default="", help="Comma-separated positive terms")
    parser.add_argument("--intent", default="", help="Comma-separated intent terms")
    parser.add_argument("--negative", default="", help="Comma-separated exclusion terms")
    parser.add_argument("--name", default="ad-hoc", help="Profile name")
    parser.add_argument("--min-score", type=int, default=2)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", help="Optional JSON report path")
    args = parser.parse_args()

    try:
        inputs = [Path(args.export)]
        if args.profile:
            inputs.append(Path(args.profile))
            profile = _read_profile(Path(args.profile), args.name, args.min_score)
        else:
            profile = SignalProfile(name=args.name, positive_terms=_split(args.positive),
                                    intent_terms=_split(args.intent), negative_terms=_split(args.negative),
                                    min_score=args.min_score)
        result = evaluate_export(args.export, profile, limit=args.limit)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            _write_report(Path(args.output), rendered, inputs)
        else:
            print(rendered)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
