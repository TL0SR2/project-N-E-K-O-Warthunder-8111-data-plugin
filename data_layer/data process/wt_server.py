"""战雷遥测后台服务（分频轮询版）。

职责：
    1) 按“不同数据用不同频率”的策略，分组在独立线程里轮询游戏的 8111 接口；
    2) 线程安全地缓存各组最新数据 + 最新小地图；
    3) 自身另开一个 HTTP 端口（默认 8112），把处理好的数据以 JSON / 图片对外提供。

说明：8111 是游戏自己开的服务器，本服务是它的客户端；对外服务端口与 8111 不同。

分频策略（每组一个线程，互不阻塞）：
    fast   (state + indicators)          高频，飞行姿态/仪表变化最快   默认 0.1s (10Hz)
    map    (map_obj)                      中频，地图态势               默认 0.5s (2Hz)
    events (mission + hudmsg + gamechat)  低频 + 增量，击杀/聊天事件   默认 1.0s
    mapimg (map_info + map.img)           极低频 + 按版本变化才取底图  默认 5.0s

其中 fast 组兼任“在线/战局”状态探针：只有它判定为 IN_BATTLE 时，其余各组才会真正发起请求；
离开战局时自动清空与本局相关的缓存（地图、HUD、聊天等），避免前端读到过期数据。

对外接口（GET）：
    /                  健康检查 + 各组刷新状态
    /api/telemetry     最新完整快照（JSON）
    /api/state         载具仪表状态
    /api/indicators    座舱原始仪表
    /api/map_objects   地图物体数组
    /api/map_info      地图坐标换算参数
    /api/hud           累积的最近 HUD 事件
    /api/chat          累积的最近聊天
    /api/map.jpg       最新小地图底图（图片）

运行：
    python wt_server.py
    python wt_server.py --port 9000 --fast-interval 0.05 --save-map
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from wt_events import KillTracker
from wt_geo import analyze_situation
from wt_processor import TelemetryProcessor
from wt_proximity import ProximityTracker, resolve_proximity_thresholds
from wt_telemetry import DEFAULT_PORT as WT_PORT
from wt_telemetry import (
    ConnectionState,
    Indicators,
    MapInfo,
    Telemetry,
    VehicleState,
    WarThunderClient,
    detect_domain,
)

_CONTENT_TYPE_BY_EXT = {"jpg": "image/jpeg", "png": "image/png"}

_HUD_BUFFER = 200   # HUD 事件累积上限
_CHAT_BUFFER = 200  # 聊天累积上限
_PROXIMITY_BUFFER = 100  # 接近告警累积上限


# ---------------------------------------------------------------------------
# 后台采集服务：分频多线程轮询 + 缓存
# ---------------------------------------------------------------------------


class TelemetryService:
    """按数据组分频轮询 8111，并缓存最新数据。"""

    def __init__(
        self,
        client: WarThunderClient,
        fast_interval: float = 0.1,
        map_interval: float = 0.5,
        event_interval: float = 1.0,
        mapimg_interval: float = 5.0,
        save_map: bool = False,
        map_dir: str = "maps",
        profiles_path: str | None = None,
        player_name: str | None = None,
    ) -> None:
        self.client = client
        self.save_map = save_map
        self.map_dir = map_dir
        self.processor = TelemetryProcessor(profiles_path)
        self.tracker = KillTracker(player_name=player_name)
        self.proximity = ProximityTracker()

        # 各数据组的轮询间隔（秒）
        self.intervals = {
            "fast": max(0.02, fast_interval),
            "map": max(0.05, map_interval),
            "events": max(0.1, event_interval),
            "mapimg": max(0.5, mapimg_interval),
        }

        self._lock = threading.Lock()

        # -- 缓存（均为整体替换，读取时拷贝引用即可） --
        self._state = ConnectionState.OFFLINE
        self._fast_ts = 0.0
        self._indicators = Indicators(valid=False)
        self._vehicle = VehicleState(valid=False)
        self._map_objects: list[Any] = []
        self._map_info = MapInfo(valid=False)
        self._mission_status: str | None = None
        self._mission_objectives: Any = None
        self._hud_events: deque = deque(maxlen=_HUD_BUFFER)
        self._chat: deque = deque(maxlen=_CHAT_BUFFER)
        self._processed: dict[str, Any] | None = None  # 加工后的关键信息/告警
        self._situation: dict[str, Any] | None = None   # 态势(最近敌机/距离方位)
        self._combat: dict[str, Any] | None = None       # 战绩(击杀流/K-D)
        self._proximity_events: deque = deque(maxlen=_PROXIMITY_BUFFER)  # 敌军接近告警流
        self._proximity_threshold: dict[str, Any] | None = None  # 当前接近距离{vs_air,vs_ground}

        # 最新地图（内存）
        self._map_bytes: bytes | None = None
        self._map_ext: str | None = None
        self._map_gen: int | None = None

        # 每组运行统计
        self._meta = {
            name: {"count": 0, "last": 0.0} for name in self.intervals
        }

        self._running = False
        self._threads: list[threading.Thread] = []

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        workers: list[tuple[str, Callable[[], None], bool]] = [
            ("fast", self._poll_fast, False),     # 状态探针，始终运行
            ("map", self._poll_map, True),
            ("events", self._poll_events, True),
            ("mapimg", self._poll_mapimg, True),
        ]
        for name, fn, require_battle in workers:
            th = threading.Thread(
                target=self._worker,
                args=(name, fn, require_battle),
                name=f"wt-{name}",
                daemon=True,
            )
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._running = False
        for th in self._threads:
            th.join(timeout=2.0)

    # -- 通用轮询循环 ------------------------------------------------------

    def _worker(self, name: str, fn: Callable[[], None], require_battle: bool) -> None:
        interval = self.intervals[name]
        while self._running:
            t0 = time.time()
            try:
                if (not require_battle) or self._state is ConnectionState.IN_BATTLE:
                    fn()
                    with self._lock:
                        meta = self._meta[name]
                        meta["count"] += 1
                        meta["last"] = time.time()
            except Exception as exc:  # 单组异常不影响其它组与整体循环
                print(f"[{name}] 轮询出错（已忽略）：{exc!r}", file=sys.stderr)
            elapsed = time.time() - t0
            time.sleep(max(0.0, interval - elapsed))

    # -- 各组采集（网络 IO 在锁外，仅缓存更新在锁内） ----------------------

    def _poll_fast(self) -> None:
        # 探针同时取 indicators + map_info；战局判定以 map_info.valid 为准
        # （主界面/机库的 indicators/state/mission 都可能“像在战局”）
        state, ind, minfo = self.client.get_indicators()
        now = time.time()
        if state is ConnectionState.IN_BATTLE:
            vehicle = self.client.get_state()
            processed = self.processor.process(vehicle, ind, now).to_dict()
        else:
            vehicle = VehicleState(valid=False)
            processed = None
            self.processor.reset()
        with self._lock:
            prev = self._state
            self._state = state
            self._indicators = ind
            self._vehicle = vehicle
            self._processed = processed
            self._map_info = minfo  # grid 参数随 fast 实时刷新，供态势换算
            self._fast_ts = now
            # 离开战局 -> 清空本局缓存
            if state is not ConnectionState.IN_BATTLE and prev is ConnectionState.IN_BATTLE:
                self._reset_battle_cache_locked()

    def _poll_map(self) -> None:
        objs = self.client.get_map_objects()
        # 态势分析依赖 map_info（由 mapimg 组维护），grid 参数基本不变可直接用缓存
        situation = analyze_situation(objs, self._map_info)
        # 敌军接近告警：阈值随【我方兵种×敌方类型】变化
        ind = self._indicators
        domain = detect_domain(ind, True, objs)
        thr_air, thr_ground = resolve_proximity_thresholds(
            self.processor.profiles, domain, getattr(ind, "vehicle_type", None)
        )
        now = time.time()
        prox_events = self.proximity.update(
            situation.get("enemies", []), thr_air, thr_ground, now
        )
        with self._lock:
            self._map_objects = objs
            self._situation = situation
            self._proximity_threshold = {"vs_air": thr_air, "vs_ground": thr_ground}
            for ev in prox_events:
                self._proximity_events.append(ev)

    def _poll_events(self) -> None:
        status, objectives = self.client.get_mission()
        hud = self.client.get_hud()
        chat = self.client.get_chat()
        self.tracker.feed(hud)  # 解析击杀事件并累积战绩
        combat = self.tracker.get_summary()
        with self._lock:
            self._mission_status = status
            self._mission_objectives = objectives
            self._combat = combat
            for ev in hud:
                self._hud_events.append(ev)
            for msg in chat:
                self._chat.append(msg)

    def _poll_mapimg(self) -> None:
        # map_info 已由 fast 组实时缓存，这里只负责按 generation 拉取底图
        with self._lock:
            info = self._map_info
        new_map: tuple[bytes, str, int | None] | None = None
        if info.valid and (self._map_bytes is None or info.map_generation != self._map_gen):
            data, ext = self.client.fetch_map_image()
            if data and ext:
                new_map = (data, ext, info.map_generation)
                if self.save_map:
                    self._write_map(data, ext, info.map_generation)
        with self._lock:
            if new_map is not None:
                self._map_bytes, self._map_ext, self._map_gen = new_map

    def _reset_battle_cache_locked(self) -> None:
        """离开战局时清空本局相关缓存（调用方需已持锁）。"""
        self._map_objects = []
        self._map_info = MapInfo(valid=False)
        self._mission_status = None
        self._mission_objectives = None
        self._hud_events.clear()
        self._chat.clear()
        self._processed = None
        self._situation = None
        self._combat = None
        self._proximity_events.clear()
        self._proximity_threshold = None
        self.tracker.reset()
        self.proximity.reset()
        self._map_bytes = None
        self._map_ext = None
        self._map_gen = None

    def _write_map(self, data: bytes, ext: str, gen: int | None) -> None:
        try:
            os.makedirs(self.map_dir, exist_ok=True)
            name = f"map_{gen}.{ext}" if gen is not None else f"map.{ext}"
            with open(os.path.join(self.map_dir, name), "wb") as fh:
                fh.write(data)
        except OSError as exc:
            print(f"[mapimg] 保存地图失败：{exc!r}", file=sys.stderr)

    # -- 线程安全读取 ------------------------------------------------------

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = Telemetry(
                state=self._state,
                timestamp=self._fast_ts,
                in_battle=self._state is ConnectionState.IN_BATTLE,
                vehicle=self._vehicle,
                indicators=self._indicators,
                map_objects=list(self._map_objects),
                map_info=self._map_info,
                mission_status=self._mission_status,
                mission_objectives=self._mission_objectives,
                hud_events=list(self._hud_events),
                chat=list(self._chat),
            )
            data = snap.to_dict()
            data["processed"] = self._processed
            data["situation"] = self._situation
            data["combat"] = self._combat
            data["proximity"] = {
                "thresholds_m": self._proximity_threshold,
                "events": list(self._proximity_events),
            }
            data["meta"] = self._meta_locked()
        return data

    def get_part(self, key: str) -> Any:
        return self.get_snapshot().get(key)

    def get_map(self) -> tuple[bytes | None, str | None]:
        with self._lock:
            return self._map_bytes, self._map_ext

    def _meta_locked(self) -> dict[str, Any]:
        now = time.time()
        out: dict[str, Any] = {}
        for name, m in self._meta.items():
            out[name] = {
                "interval": self.intervals[name],
                "count": m["count"],
                "age_sec": round(now - m["last"], 3) if m["last"] else None,
            }
        return out

    def get_health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "service": "wt-telemetry",
                "state": self._state.value,
                "updated_at": self._fast_ts,
                "has_map": self._map_bytes is not None,
                "map_generation": self._map_gen,
                "groups": self._meta_locked(),
            }


# ---------------------------------------------------------------------------
# HTTP 请求处理
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "WTTelemetry/2.0"

    @property
    def service(self) -> TelemetryService:
        return self.server.service  # type: ignore[attr-defined]

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def _send_json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path in ("/", "/health", "/api/health"):
            self._send_json(self.service.get_health())
            return

        if path == "/api/telemetry":
            self._send_json(self.service.get_snapshot())
            return

        if path == "/api/processed":
            self._send_json(self.service.get_part("processed"))
            return

        if path == "/api/situation":
            self._send_json(self.service.get_part("situation"))
            return

        if path in ("/api/kills", "/api/combat"):
            self._send_json(self.service.get_part("combat"))
            return

        if path == "/api/proximity":
            self._send_json(self.service.get_part("proximity"))
            return

        if path == "/api/alerts":
            processed = self.service.get_part("processed")
            alerts = processed.get("alerts", []) if isinstance(processed, dict) else []
            level = processed.get("level") if isinstance(processed, dict) else None
            self._send_json({"level": level, "alerts": alerts})
            return

        if path in ("/api/map.jpg", "/api/map"):
            data, ext = self.service.get_map()
            if not data:
                self._send_json({"error": "no map available"}, 404)
                return
            ctype = _CONTENT_TYPE_BY_EXT.get(ext or "", "application/octet-stream")
            self._send_bytes(data, ctype)
            return

        subset_keys = {
            "/api/state": "vehicle",
            "/api/indicators": "indicators",
            "/api/map_objects": "map_objects",
            "/api/map_info": "map_info",
            "/api/hud": "hud_events",
            "/api/chat": "chat",
        }
        if path in subset_keys:
            self._send_json(self.service.get_part(subset_keys[path]))
            return

        self._send_json({"error": "not found", "path": path}, 404)

    def log_message(self, *args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="战雷遥测后台服务（分频轮询）")
    parser.add_argument("--host", default="0.0.0.0", help="对外服务监听地址")
    parser.add_argument("--port", type=int, default=8112, help="对外服务端口（默认 8112）")
    parser.add_argument("--wt-host", default="127.0.0.1", help="游戏 8111 地址")
    parser.add_argument("--wt-port", type=int, default=WT_PORT, help="游戏遥测端口（默认 8111）")
    parser.add_argument("--fast-interval", type=float, default=0.1, help="姿态/仪表轮询间隔（默认 0.1s）")
    parser.add_argument("--map-interval", type=float, default=0.5, help="地图物体轮询间隔（默认 0.5s）")
    parser.add_argument("--event-interval", type=float, default=1.0, help="任务/HUD/聊天轮询间隔（默认 1.0s）")
    parser.add_argument("--mapimg-interval", type=float, default=5.0, help="地图底图检查间隔（默认 5.0s）")
    parser.add_argument("--save-map", action="store_true", help="地图变化时落盘保存")
    parser.add_argument("--map-dir", default="maps", help="地图保存目录")
    parser.add_argument("--profiles", default=None, help="机型告警配置文件路径")
    parser.add_argument("--player-name", default=None, help="玩家名(不含战队标签),用于统计我的K/D")
    args = parser.parse_args()

    client = WarThunderClient(host=args.wt_host, port=args.wt_port)
    service = TelemetryService(
        client,
        fast_interval=args.fast_interval,
        map_interval=args.map_interval,
        event_interval=args.event_interval,
        mapimg_interval=args.mapimg_interval,
        save_map=args.save_map,
        map_dir=args.map_dir,
        profiles_path=args.profiles,
        player_name=args.player_name,
    )
    service.start()

    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    httpd.service = service  # type: ignore[attr-defined]

    print(f"战雷遥测服务已启动：http://{args.host}:{args.port}")
    print(f"  数据源：http://{args.wt_host}:{args.wt_port}")
    print("  分频轮询：")
    print(f"    fast(state+indicators) {args.fast_interval}s")
    print(f"    map(map_obj)           {args.map_interval}s")
    print(f"    events(mission+hud+chat) {args.event_interval}s")
    print(f"    mapimg(map_info+map.img) {args.mapimg_interval}s")
    print("  接口： /  /api/telemetry  /api/state  /api/map_objects  /api/map.jpg")
    print("        /api/processed  /api/alerts  （自定义告警）")
    print("        /api/situation （态势）  /api/kills （战绩）")
    print("        /api/proximity （敌军接近告警，边沿触发）")
    print("  Ctrl+C 退出\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.shutdown()
        service.stop()
        print("已停止。")


if __name__ == "__main__":
    main()
