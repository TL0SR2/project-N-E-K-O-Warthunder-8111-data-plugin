"""离散/生命周期检测器：spawn / you_died / battle_end / you_killed。

按"跳变 / 新 id"去重（D-B3 已边沿型）：
- spawn / you_died：用 in_battle + vehicle_valid 跳变（不依赖 player_name，v1 启发式）。
- battle_end：mission_status 进入结束态的跳变。
- you_killed：需 combat.player_name；为空时不产出（桩，待数据层补 hudmsg/击杀归属）。
"""

from __future__ import annotations

from typing import Any

from ...core.contracts import BattleEvent, BattleState
from .._base import DiscreteDetector

_END_STATUSES = frozenset({"win", "won", "victory", "fail", "failed", "lost", "defeat", "left", "ended", "finished"})


def _alive(s: BattleState) -> bool:
    return bool(s.in_battle and s.vehicle_valid)


class SpawnDetector(DiscreteDetector):
    id = "spawn"

    def detect(self, prev: BattleState, cur: BattleState) -> BattleEvent | None:
        # 要求 prev.connected：遥测瞬断（parse(None)→not alive）恢复后不误判为重生
        if _alive(cur) and not _alive(prev) and prev.connected:
            return BattleEvent("spawn", payload={"vehicle_type": cur.vehicle_type}, ts=cur.timestamp or 0.0, level="warning")
        return None


class DeathDetector(DiscreteDetector):
    """阵亡：存活→在战但无载具遥测。cause 暂未知（待 hudmsg 富化）。"""

    id = "you_died"

    def detect(self, prev: BattleState, cur: BattleState) -> BattleEvent | None:
        if _alive(prev) and cur.in_battle and not cur.vehicle_valid:
            return BattleEvent("you_died", payload={"cause": "unknown"}, ts=cur.timestamp or 0.0, level="critical")
        return None


class BattleEndDetector(DiscreteDetector):
    id = "battle_end"

    def _ended(self, s: BattleState) -> bool:
        return (s.mission_status or "").lower() in _END_STATUSES

    def detect(self, prev: BattleState, cur: BattleState) -> BattleEvent | None:
        if self._ended(cur) and not self._ended(prev):
            payload: dict[str, Any] = {"result": cur.mission_status}
            my = cur.combat.get("my") if isinstance(cur.combat, dict) else None
            if isinstance(my, dict):
                payload["result"] = f"{cur.mission_status}, K{my.get('kills', 0)}/D{my.get('deaths', 0)}"
            return BattleEvent("battle_end", payload=payload, ts=cur.timestamp or 0.0, level="warning")
        return None


class KillDetector(DiscreteDetector):
    """击杀：combat.feed 里 killer==player_name 的新 is_kill。player_name 空=不产出（桩）。"""

    id = "you_killed"

    def __init__(self, player_name: str) -> None:
        self.player_name = (player_name or "").strip()
        self._last_id: int = -1  # 已处理的最大 feed id（单调、确定、有界；feed id 递增）

    def detect(self, prev: BattleState, cur: BattleState) -> BattleEvent | None:
        if not self.player_name:
            return None
        feed = cur.combat.get("feed") if isinstance(cur.combat, dict) else None
        if not isinstance(feed, list) or not feed:
            return None
        ids: list[int] = []
        for item in feed:
            if not isinstance(item, dict):
                continue
            try:
                ids.append(int(item.get("id")))
            except (TypeError, ValueError):
                continue
        if not ids:
            return None
        max_id = max(ids)
        if max_id < self._last_id:  # 新对局 feed id 回退 → 重置
            self._last_id = -1
        newest: dict[str, Any] | None = None
        for item in feed:
            if not isinstance(item, dict):
                continue
            try:
                eid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if eid <= self._last_id:
                continue
            if item.get("is_kill") and str(item.get("killer") or "") == self.player_name:
                if newest is None or eid > int(newest.get("id")):
                    newest = item
        self._last_id = max(self._last_id, max_id)
        if newest is None:
            return None
        return BattleEvent(
            "you_killed",
            payload={"victim": newest.get("victim"), "victim_vehicle": newest.get("victim_vehicle")},
            ts=cur.timestamp or 0.0,
            level="warning",
        )


def build_discrete_detectors(player_name: str) -> list[DiscreteDetector]:
    return [SpawnDetector(), DeathDetector(), BattleEndDetector(), KillDetector(player_name)]
