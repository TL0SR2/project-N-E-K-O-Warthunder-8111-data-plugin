"""Replay local data-layer sample dumps through the plugin logic.

This tool is intended for ignored local samples under ``local_samples/``. It
does not persist output and its summaries intentionally avoid raw free text.
"""

from __future__ import annotations

import gzip
import json
import pathlib
import sys
import types
from collections import Counter
from typing import Any, Iterable

_BASE = pathlib.Path(__file__).resolve().parent.parent
if "neko_warthunder" not in sys.modules:
    _pkg = types.ModuleType("neko_warthunder")
    _pkg.__path__ = [str(_BASE)]  # type: ignore[attr-defined]
    sys.modules["neko_warthunder"] = _pkg

from neko_warthunder.adapters.neko_dispatcher import NekoDispatcher  # noqa: E402
from neko_warthunder.adapters.telemetry_client import parse_telemetry  # noqa: E402
from neko_warthunder.core.arbiter import Arbiter  # noqa: E402
from neko_warthunder.core.contracts import BattleState, WtConfig  # noqa: E402
from neko_warthunder.core.safety_guard import SafetyGuard  # noqa: E402
from neko_warthunder.core.scenario import ScenarioResolver  # noqa: E402
from neko_warthunder.detectors._base import DetectorEngine  # noqa: E402
from neko_warthunder.detectors.condition.flight_safety import build_condition_detectors  # noqa: E402
from neko_warthunder.detectors.discrete.lifecycle import build_discrete_detectors  # noqa: E402


def discover_sample_files(root: str | pathlib.Path) -> list[pathlib.Path]:
    base = pathlib.Path(root)
    files = list(base.glob("captures/*/processed_8112.jsonl"))
    files.extend(base.glob("records/*/frames*.jsonl.gz"))
    return sorted(files, key=lambda p: p.as_posix())


def _iter_jsonl(path: pathlib.Path) -> Iterable[dict[str, Any]]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            yield from _loads_lines(f)
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        yield from _loads_lines(f)


def _loads_lines(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        data = json.loads(stripped)
        if isinstance(data, dict):
            yield data


def _unwrap_payload(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("data")
    return data if isinstance(data, dict) else row


def replay_sample_root(root: str | pathlib.Path, *, player_name: str = "") -> dict[str, Any]:
    files = discover_sample_files(root)
    cfg = WtConfig(
        player_name=player_name,
        dry_run=True,
        global_rate_limit_seconds=0,
        critical_preempt_cooldown_seconds=0,
        spawn_grace_seconds=0,
    )
    resolver = ScenarioResolver()
    engine = DetectorEngine(list(build_condition_detectors()) + list(build_discrete_detectors(cfg.player_name)))
    arbiter = Arbiter(SafetyGuard(cfg))
    dispatcher = NekoDispatcher(None)

    report: dict[str, Any] = {
        "root": str(pathlib.Path(root)),
        "files": len(files),
        "frames": 0,
        "states": Counter(),
        "domains": Counter(),
        "flags": Counter(),
        "events": Counter(),
        "chosen": Counter(),
        "dry_run_outputs": Counter(),
        "sample_files": [str(p.relative_to(root)) if pathlib.Path(root) in p.parents else str(p) for p in files],
    }

    prev = BattleState()
    now = 1000.0
    for path in files:
        for row in _iter_jsonl(path):
            payload = _unwrap_payload(row)
            cur = parse_telemetry(payload)
            report["frames"] += 1
            report["states"][cur.conn_state] += 1
            report["domains"][cur.domain] += 1
            for key, value in cur.flags.items():
                if value:
                    report["flags"][key] += 1

            cur.scenario = resolver.resolve(cur, now, cfg.spawn_grace_seconds)
            candidates = engine.feed(prev, cur)
            for event in candidates:
                report["events"][f"{event.event_id}/{event.level}"] += 1

            chosen, _chain = arbiter.decide(candidates, cur.scenario, now)
            if chosen is not None:
                event_key = f"{chosen.event_id}/{chosen.level}"
                report["chosen"][event_key] += 1
                result = dispatcher.push_event(chosen, dry_run=True)
                report["dry_run_outputs"][result.split(",", 1)[0].replace("dry_run(event=", "")] += 1
            prev = cur
            now += 1.0

    return _plain_report(report)


def _plain_report(report: dict[str, Any]) -> dict[str, Any]:
    plain = dict(report)
    for key in ("states", "domains", "flags", "events", "chosen", "dry_run_outputs"):
        plain[key] = dict(report[key])
    return plain


def render_report(report: dict[str, Any]) -> str:
    lines = [
        f"root: {report['root']}",
        f"files: {report['files']}",
        f"frames: {report['frames']}",
        f"states: {_fmt_counts(report['states'])}",
        f"domains: {_fmt_counts(report['domains'])}",
        f"flags: {_fmt_counts(report['flags'])}",
        f"candidate_events: {_fmt_counts(report['events'])}",
        f"chosen_events: {_fmt_counts(report['chosen'])}",
        f"dry_run_outputs: {_fmt_counts(report['dry_run_outputs'])}",
    ]
    return "\n".join(lines)


def _fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1]) if len(argv) > 1 else _BASE / "local_samples" / "data_process_20260620"
    player_name = argv[2] if len(argv) > 2 else "tl0sr2"
    report = replay_sample_root(root, player_name=player_name)
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
