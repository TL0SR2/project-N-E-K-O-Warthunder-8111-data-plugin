"""Detector：边沿 FSM（confirm/迟滞/re-arm）+ 离散去重（D-B3）。"""

from __future__ import annotations

from neko_warthunder.core import contracts as C
from neko_warthunder.detectors._base import ConditionDetector
from neko_warthunder.detectors.discrete.lifecycle import DeathDetector, KillDetector, SpawnDetector


def _st(flags=None):
    return C.BattleState(flags=flags or {})


def test_condition_enter_after_confirm():
    d = ConditionDetector("stall_risk", [("stall_warning", "stall_critical")], confirm_enter=2, confirm_exit=2)
    prev = C.BattleState()
    assert d.feed(prev, _st({"stall_warning": True})) is None        # confirming 1/2
    ev = d.feed(prev, _st({"stall_warning": True}))                  # confirming 2/2 -> ACTIVE
    assert ev is not None and ev.event_id == "stall_risk" and ev.edge == "enter" and ev.level == "warning"
    assert d.feed(prev, _st({"stall_warning": True})) is None        # 持续期不重发


def test_condition_debounce_spike():
    d = ConditionDetector("stall_risk", [("stall_warning", "stall_critical")], confirm_enter=2)
    prev = C.BattleState()
    assert d.feed(prev, _st({"stall_warning": True})) is None        # 1
    assert d.feed(prev, _st({})) is None                             # 单帧尖刺被滤，回 ARMED
    assert d.feed(prev, _st({"stall_warning": True})) is None        # 重新计数 1/2


def test_condition_rearm():
    d = ConditionDetector("low_fuel", [("fuel_low", "fuel_critical")], confirm_enter=1, confirm_exit=1)
    prev = C.BattleState()
    assert d.feed(prev, _st({"fuel_low": True})).event_id == "low_fuel"   # confirm_enter=1 当拍触发
    assert d.feed(prev, _st({})) is None                                  # 退出 -> re-arm
    assert d.feed(prev, _st({"fuel_low": True})).event_id == "low_fuel"   # 再次触发


def test_condition_critical_level():
    d = ConditionDetector("stall_risk", [("stall_warning", "stall_critical")], confirm_enter=1)
    ev = d.feed(C.BattleState(), _st({"stall_critical": True}))
    assert ev is not None and ev.level == "critical"


def test_condition_escalation_reemits_critical():
    """warning 持续中升级到 critical：应重发一条 critical enter（可抢占）。"""
    d = ConditionDetector("stall_risk", [("stall_warning", "stall_critical")], confirm_enter=1, confirm_exit=2)
    prev = C.BattleState()
    ev1 = d.feed(prev, _st({"stall_warning": True}))
    assert ev1 is not None and ev1.level == "warning"
    ev2 = d.feed(prev, _st({"stall_critical": True}))   # 升级
    assert ev2 is not None and ev2.level == "critical" and ev2.edge == "enter"
    assert d.feed(prev, _st({"stall_critical": True})) is None  # 升级后不重复


def test_spawn_detector():
    det = SpawnDetector()
    prev = C.BattleState(connected=True, in_battle=False, vehicle_valid=False)
    cur = C.BattleState(connected=True, in_battle=True, vehicle_valid=True, vehicle_type="bf-109f-4")
    ev = det.feed(prev, cur)
    assert ev is not None and ev.event_id == "spawn" and ev.payload.get("vehicle_type") == "bf-109f-4"
    assert det.feed(cur, cur) is None  # 已存活不再触发


def test_spawn_not_fired_after_telemetry_blip():
    det = SpawnDetector()
    blip = C.BattleState(connected=False)  # 遥测瞬断
    cur = C.BattleState(connected=True, in_battle=True, vehicle_valid=True)
    assert det.feed(blip, cur) is None  # prev 断连 → 不误判重生


def test_death_detector():
    det = DeathDetector()
    prev = C.BattleState(in_battle=True, vehicle_valid=True)
    cur = C.BattleState(in_battle=True, vehicle_valid=False)
    ev = det.feed(prev, cur)
    assert ev is not None and ev.event_id == "you_died" and ev.level == "critical"


def test_kill_dedup_monotonic():
    det = KillDetector("Me")
    feed1 = {"player_name": "Me", "feed": [{"id": 5, "is_kill": True, "killer": "Me", "victim": "A"}]}
    cur1 = C.BattleState(in_battle=True, vehicle_valid=True, combat=feed1)
    ev = det.feed(C.BattleState(), cur1)
    assert ev is not None and ev.event_id == "you_killed"
    assert det.feed(cur1, cur1) is None  # 同一 feed 不重发
    feed2 = {"player_name": "Me", "feed": [{"id": 8, "is_kill": True, "killer": "Me", "victim": "B"}, {"id": 5, "is_kill": True, "killer": "Me", "victim": "A"}]}
    cur2 = C.BattleState(in_battle=True, vehicle_valid=True, combat=feed2)
    ev2 = det.feed(cur1, cur2)
    assert ev2 is not None and ev2.payload.get("victim") == "B"  # 只发新 id


def test_kill_requires_player_name():
    det = KillDetector("")
    feed = {"feed": [{"id": 1, "is_kill": True, "killer": "Someone", "victim": "X"}]}
    cur = C.BattleState(in_battle=True, vehicle_valid=True, combat=feed)
    assert det.feed(C.BattleState(), cur) is None  # 无 player_name → 桩，不产出
