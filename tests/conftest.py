"""把 neko_warthunder 的纯逻辑子包注册成轻量顶层包，绕开会拉 SDK/宿主的包 __init__。

这样单测无需 NEKO 宿主环境即可跑（`python3 -m pytest plugin/plugins/neko_warthunder/tests`），
只测纯逻辑（contracts / scenario / detectors / arbiter / telemetry 解析）。
"""

from __future__ import annotations

import pathlib
import sys
import types

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent

if "neko_warthunder" not in sys.modules:
    _pkg = types.ModuleType("neko_warthunder")
    _pkg.__path__ = [str(_PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules["neko_warthunder"] = _pkg
