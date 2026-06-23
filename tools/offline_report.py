"""Render a safe offline readiness report from local sample replay output."""

from __future__ import annotations

import argparse
import pathlib
import sys
import types
from typing import Any

_BASE = pathlib.Path(__file__).resolve().parent.parent
if "neko_warthunder" not in sys.modules:
    _pkg = types.ModuleType("neko_warthunder")
    _pkg.__path__ = [str(_BASE)]  # type: ignore[attr-defined]
    sys.modules["neko_warthunder"] = _pkg

from neko_warthunder.tools.sample_replay import replay_sample_root  # noqa: E402


def build_markdown_report(root: str | pathlib.Path, *, player_name: str = "tl0sr2") -> str:
    report = replay_sample_root(root, player_name=player_name)
    summary = report.get("session_summary") or {}
    checks = summary.get("validation_checks") or {}

    lines = [
        "# neko_warthunder offline readiness report",
        "",
        f"- sample root: `{report.get('root')}`",
        f"- files: `{report.get('files')}`",
        f"- frames: `{report.get('frames')}`",
        f"- status: `{summary.get('status') or 'unknown'}`",
        "",
        "## Observed outputs",
        "",
        _bullet_list(summary.get("observed_outputs") or []),
        "",
        "## Validation checks",
        "",
        "| check | status | detail |",
        "| --- | --- | --- |",
    ]
    for key in sorted(checks):
        value = checks[key] if isinstance(checks[key], dict) else {}
        detail = value.get("missing") or value.get("observed") or []
        lines.append(f"| {key} | {value.get('status') or 'unknown'} | {_inline_list(detail)} |")

    lines.extend(
        [
            "",
            "## Coverage gaps",
            "",
            _bullet_list(report.get("coverage_gaps") or []),
            "",
            "## Next validation steps",
            "",
            _bullet_list(summary.get("next_steps") or []),
            "",
            "## Remaining live-test scope",
            "",
            _bullet_list(_remaining_live_scope(summary)),
            "",
            "## Safety notes",
            "",
            "- Raw player names, HUD text, combat feed text, and awards text are not included.",
            "- free_text_safety=dry_run_only means the sample contains free-text sources, but real speech remains blocked.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _bullet_list(values: list[Any]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- `{value}`" for value in values)


def _remaining_live_scope(summary: dict[str, Any]) -> list[str]:
    checks = summary.get("validation_checks") if isinstance(summary.get("validation_checks"), dict) else {}
    scope: list[str] = []
    for key, value in checks.items():
        if not isinstance(value, dict):
            continue
        status = value.get("status")
        if status in {"needs_more_samples", "dry_run_only"}:
            scope.append(f"{key}:{status}")
    return scope


def _inline_list(values: list[Any]) -> str:
    if not values:
        return "-"
    return ", ".join(f"`{value}`" for value in values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a safe neko_warthunder offline readiness report.")
    parser.add_argument("root", nargs="?", default=str(_BASE / "local_samples" / "data_process_20260620"))
    parser.add_argument("player_name", nargs="?", default="tl0sr2")
    parser.add_argument("--output", help="Write Markdown report to this path instead of stdout.")
    args = parser.parse_args(argv)

    markdown = build_markdown_report(args.root, player_name=args.player_name)
    if args.output:
        pathlib.Path(args.output).write_text(markdown, encoding="utf-8")
    else:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
