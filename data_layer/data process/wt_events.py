"""战雷 HUD 事件（击杀/受损）解析与战绩统计。

把 /hudmsg 的 damage 文本解析成结构化击杀事件，并累积本局战绩(K/D)。

数据特点（来自实测，中文客户端）：
    "-RINKO- tl0sr2 (歼-15T) 击毁了 AI米格-15bis"
    - 文本语言随游戏客户端（这里是中文），故动作词需中英文都支持。
    - 字符间插有零宽字符(\\u200b 等，战雷反爬)，解析前必须清洗。
    - 格式：<[战队] 玩家名> (载具) <动作> <被击杀者[ (载具)]>
    - AI 单位名以 "AI" 开头且通常无载具括号。

注意：localhost API 不直接告诉你“谁是自己”。可在创建 KillTracker 时传入 player_name
（玩家名，不含战队标签）来统计“我的”战绩；不传则只产出全局击杀榜，由前端自行高亮。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

# 需清洗的零宽/不可见字符
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00a0"), None
)


def clean_text(s: str | None) -> str:
    """去除零宽字符并压缩空白。"""
    if not s:
        return ""
    text = s.translate(_INVISIBLE)
    return re.sub(r"\s+", " ", text).strip()


# 双方事件：<击杀者> 动作 <被击杀者>。短语按出现概率排列，先匹配到先用。
_DUAL_ACTIONS: list[tuple[tuple[str, ...], str]] = [
    (("击落了", "shot down", "gunned down"), "shot_down"),
    (("击毁了", "摧毁了", "destroyed"), "destroyed"),
    (("炸毁了", "炸沉了"), "destroyed"),
    (("点燃了", "set afire", "set on fire"), "set_afire"),
    (("严重损坏了", "严重损毁了", "severely damaged"), "severely_damaged"),
    (("击伤了", "damaged"), "damaged"),
]

# 单方事件：<对象> 动作（无施动者，如坠毁/坠机）
_SOLO_ACTIONS: list[tuple[tuple[str, ...], str]] = [
    (("坠毁", "has crashed", "crashed"), "crashed"),
    (("失控", "has been wrecked", "wrecked"), "wrecked"),
]

_ACTION_LABEL = {
    "shot_down": "击落",
    "destroyed": "击毁",
    "set_afire": "点燃",
    "severely_damaged": "重创",
    "damaged": "击伤",
    "crashed": "坠毁",
    "wrecked": "损毁",
    "other": "事件",
}


@dataclass
class KillEvent:
    """一条结构化击杀/受损事件。"""

    id: int = 0
    time: int | None = None
    action: str = "other"          # 归一化动作代码
    action_label: str = "事件"      # 中文动作标签
    killer: str = ""               # 施动者玩家名（不含战队/载具）
    killer_squad: str = ""
    killer_vehicle: str = ""
    killer_is_ai: bool = False
    victim: str = ""
    victim_squad: str = ""
    victim_vehicle: str = ""
    victim_is_ai: bool = False
    is_kill: bool = False          # 是否致命(击落/击毁/坠毁/损毁)
    parsed: bool = True            # 是否成功解析
    raw: str = ""                  # 清洗后的原文

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FATAL_ACTIONS = {"shot_down", "destroyed", "crashed", "wrecked"}


def _parse_actor(part: str) -> tuple[str, str, str, bool]:
    """解析一个参与者，返回 (玩家名, 战队, 载具, 是否AI)。"""
    part = part.strip()
    vehicle = ""
    # 末尾的 (载具)
    m = re.search(r"\(([^)]*)\)\s*$", part)
    if m:
        vehicle = m.group(1).strip()
        part = part[: m.start()].strip()

    is_ai = part.startswith("AI")
    if is_ai:
        part = part[2:].strip()

    squad = ""
    # 战队标签：-TAG- name 或 [TAG] name
    m2 = re.match(r"^[\-\[]([^\-\]]{1,12})[\-\]]\s+(.+)$", part)
    if m2:
        squad = m2.group(1).strip()
        name = m2.group(2).strip()
    else:
        name = part

    # AI 通常名字即载具
    if is_ai and not vehicle:
        vehicle = name
    return name, squad, vehicle, is_ai


def parse_event(text: str, event_id: int = 0, time: int | None = None) -> KillEvent:
    """把一条 hudmsg 文本解析成 KillEvent；解析失败时 parsed=False 但保留原文。"""
    raw = clean_text(text)

    for phrases, norm in _DUAL_ACTIONS:
        for ph in phrases:
            idx = raw.find(ph)
            if idx == -1:
                continue
            killer_part = raw[:idx].strip()
            victim_part = raw[idx + len(ph):].strip()
            if not killer_part or not victim_part:
                continue
            k_name, k_squad, k_veh, k_ai = _parse_actor(killer_part)
            v_name, v_squad, v_veh, v_ai = _parse_actor(victim_part)
            return KillEvent(
                id=event_id, time=time, action=norm,
                action_label=_ACTION_LABEL.get(norm, "事件"),
                killer=k_name, killer_squad=k_squad,
                killer_vehicle=k_veh, killer_is_ai=k_ai,
                victim=v_name, victim_squad=v_squad,
                victim_vehicle=v_veh, victim_is_ai=v_ai,
                is_kill=norm in _FATAL_ACTIONS, parsed=True, raw=raw,
            )

    for phrases, norm in _SOLO_ACTIONS:
        for ph in phrases:
            idx = raw.find(ph)
            if idx == -1:
                continue
            victim_part = raw[:idx].strip()
            v_name, v_squad, v_veh, v_ai = _parse_actor(victim_part)
            return KillEvent(
                id=event_id, time=time, action=norm,
                action_label=_ACTION_LABEL.get(norm, "事件"),
                victim=v_name, victim_squad=v_squad,
                victim_vehicle=v_veh, victim_is_ai=v_ai,
                is_kill=norm in _FATAL_ACTIONS, parsed=True, raw=raw,
            )

    return KillEvent(id=event_id, time=time, parsed=False, raw=raw)


class KillTracker:
    """累积本局击杀事件与战绩统计。"""

    def __init__(self, player_name: str | None = None, feed_size: int = 100) -> None:
        self.player_name = (player_name or "").strip()
        self._feed: deque[KillEvent] = deque(maxlen=feed_size)
        self._seen_ids: set[int] = set()
        self.reset()

    def reset(self) -> None:
        """清空（换局时调用）。"""
        self._feed.clear()
        self._seen_ids.clear()
        self._players: dict[str, dict[str, int]] = {}
        self._by_action: dict[str, int] = {}
        self._my = {"kills": 0, "deaths": 0}

    def set_player_name(self, name: str | None) -> None:
        self.player_name = (name or "").strip()

    def feed(self, hud_messages: list[Any]) -> list[KillEvent]:
        """喂入新的 HudMessage（kind=='damage' 的会被解析），返回本次新增的事件。"""
        added: list[KillEvent] = []
        for hm in hud_messages:
            kind = getattr(hm, "kind", None)
            if kind != "damage":
                continue
            eid = int(getattr(hm, "id", 0) or 0)
            if eid in self._seen_ids:
                continue
            self._seen_ids.add(eid)
            ev = parse_event(getattr(hm, "msg", ""), eid, getattr(hm, "time", None))
            self._feed.append(ev)
            self._accumulate(ev)
            added.append(ev)
        return added

    def _accumulate(self, ev: KillEvent) -> None:
        self._by_action[ev.action] = self._by_action.get(ev.action, 0) + 1
        if ev.killer and not ev.killer_is_ai and ev.is_kill:
            self._players.setdefault(ev.killer, {"kills": 0, "deaths": 0})["kills"] += 1
        if ev.victim and not ev.victim_is_ai and ev.is_kill:
            self._players.setdefault(ev.victim, {"kills": 0, "deaths": 0})["deaths"] += 1
        if self.player_name:
            if ev.killer == self.player_name and ev.is_kill:
                self._my["kills"] += 1
            if ev.victim == self.player_name and ev.is_kill:
                self._my["deaths"] += 1

    def get_summary(self) -> dict[str, Any]:
        """返回战绩快照：击杀流 + 各动作计数 + 玩家榜 + 我的K/D。"""
        leaderboard = sorted(
            ({"name": n, **s} for n, s in self._players.items()),
            key=lambda x: (x["kills"], -x["deaths"]),
            reverse=True,
        )
        return {
            "player_name": self.player_name or None,
            "total_events": len(self._seen_ids),
            "by_action": dict(self._by_action),
            "leaderboard": leaderboard[:20],
            "my": dict(self._my) if self.player_name else None,
            "feed": [e.to_dict() for e in reversed(self._feed)],
        }
