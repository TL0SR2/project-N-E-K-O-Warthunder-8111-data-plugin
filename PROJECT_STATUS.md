# Project Status

## Current State

- M1 scaffold and M2 understanding/decision logic are implemented.
- Logic self-check currently passes: `29/29`.
- Default runtime mode is `dry_run = true`; the plugin runs the decision chain but does not push real catgirl speech until dry run is disabled.
- The plugin boundary is HTTP `:8112` (`/api/telemetry`) only. It consumes the vendored data layer and must not import or modify `data_layer/` code.

## Ready to Hand Off

- Core contracts, scenario machine, detectors, arbiter, safety guard, dispatcher, tests, and replay tool are present.
- Design docs are complete for the current v1 scope: D-B1 through D-B5, implementation plan, data-layer TODOs, and real-machine validation checklist.
- Vendored data layer is included under `data_layer/data process/`.

## Not Done Yet

- NEKO host validation is not done.
- Real-game/data-layer validation is not done.
- `ui/panel.tsx` is not implemented.
- Data-layer subprocess orchestration is not implemented.
- M3 event unstubbing is waiting on data-layer support for overspeed flags, HUD/combat parsing, and stable `player_name`.
- `contract/telemetry_sample.json` is still waiting for a real `/api/telemetry` capture.
- i18n currently has only a `zh-CN` placeholder; full 8-locale coverage is expected when the panel lands.

## Verification

Run from the N.E.K.O repository root:

```powershell
uv run python plugin/plugins/neko_warthunder/tests/run_logic_tests.py
uv run pytest plugin/plugins/neko_warthunder/tests -q
```

The real-machine checklist is in `docs/真机验证-checklist.md`.

## Next Recommended Work

1. Run the three host/real-machine seam checks from `docs/真机验证-checklist.md`.
2. Implement the minimal hosted UI panel.
3. Add data-layer subprocess orchestration or document the manual startup path.
4. Capture `contract/telemetry_sample.json` from a real `/api/telemetry` response.
5. Unstub overspeed and kill/death handling after the data layer provides the missing fields.
