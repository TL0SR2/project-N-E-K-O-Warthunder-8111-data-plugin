"""Unified offline preflight helper tests."""

from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path


def test_preflight_plan_contains_documented_checks():
    from neko_warthunder.tools import preflight

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        plugin_root = root / "plugin"
        host_root = root / "N.E.K.O"
        sample_root = plugin_root / "local_samples" / "data_process_20260620"
        sample_root.mkdir(parents=True)
        host_root.mkdir()

        checks = preflight.build_checks(plugin_root=plugin_root, host_root=host_root)
        names = [check.name for check in checks]

        assert names == [
            "logic self-check",
            "pytest",
            "plugin check",
            "synthetic replay",
            "local sample replay",
        ]
        assert checks[0].cwd == plugin_root.resolve()
        assert checks[0].cmd == ["uv", "run", "python", "tests/run_logic_tests.py"]
        assert checks[2].cwd == host_root.resolve()
        assert checks[2].cmd[-1] == str(plugin_root.resolve())
        assert checks[-1].cmd == [
            "uv",
            "run",
            "python",
            "tools/sample_replay.py",
            "local_samples/data_process_20260620",
            "tl0sr2",
        ]


def test_preflight_plan_skips_optional_sample_when_missing():
    from neko_warthunder.tools import preflight

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        checks = preflight.build_checks(plugin_root=root, host_root=root / "missing-host")
        names = [check.name for check in checks]

        assert "plugin check" not in names
        assert "local sample replay" not in names
        assert names == ["logic self-check", "pytest", "synthetic replay"]


def test_preflight_dry_run_prints_commands_without_running():
    from neko_warthunder.tools import preflight

    with tempfile.TemporaryDirectory() as td:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = preflight.main(["--plugin-root", td])

        text = output.getvalue()
        assert rc == 0
        assert "# neko_warthunder offline preflight" in text
        assert "uv run python tests/run_logic_tests.py" in text
        assert "uv run pytest -c tests/pytest.ini tests -q" in text
        assert "uv run python tools/replay.py" in text
        assert "use --run to execute" in text
