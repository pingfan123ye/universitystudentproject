"""
情境引擎 —— 定时检查时间 + 用户记忆 + 设备/传感器状态，
匹配触发条件后向用户发送主动提醒。
"""
import asyncio
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


async def _parse_schedule(memories: list[dict]) -> dict:
    """从记忆中提取日程信息"""
    schedule = {}
    for m in memories:
        val = m.get("value", "")
        # 匹配 "7:50" / "07:50" / "7点50" 格式
        import re
        m_time = re.search(r'(\d{1,2})[:：点](\d{0,2})', val)
        if m_time:
            hour = int(m_time.group(1))
            minute = int(m_time.group(2)) if m_time.group(2) else 0
            category = m.get("category", "")
            text = f"{category}:{val}"
            if "出门" in val or "上班" in val:
                schedule["leave_hour"] = hour
                schedule["leave_minute"] = minute
            elif "起床" in val or "醒" in val:
                schedule["wake_hour"] = hour
                schedule["wake_minute"] = minute
            elif "睡觉" in val or "睡" in val or "晚安" in val:
                schedule["sleep_hour"] = hour
                schedule["sleep_minute"] = minute

        # 通勤时间
        m_commute = re.search(r'(\d+)\s*分钟', val)
        if m_commute and ("通勤" in val or "路程" in val or "路上" in val):
            schedule["commute_minutes"] = int(m_commute.group(1))

    return schedule


def _check_leave_reminder(hour: int, minute: int, schedule: dict,
                          sensors: dict) -> dict | None:
    """检查是否需要提醒出门"""
    if "leave_hour" not in schedule:
        return None
    lh = schedule["leave_hour"]
    lm = schedule.get("leave_minute", 0)
    commute = schedule.get("commute_minutes", 30)
    buffer_min = 10
    # 提醒时间 = 出门时间 - 通勤时间 - 缓冲
    remind_h = lh
    remind_m = lm - commute - buffer_min
    while remind_m < 0:
        remind_h -= 1
        remind_m += 60
    if remind_h < 0:
        return None

    # 精确匹配 ±2 分钟内
    current_total = hour * 60 + minute
    remind_total = remind_h * 60 + remind_m
    if abs(current_total - remind_total) <= 2:
        # 检查用户是否还在家
        sensor_hint = ""
        if sensors.get("bedroom_motion", {}).get("value") == "person_detected":
            sensor_hint = " 看你还在家"
        return {
            "id": "leave_reminder",
            "message": f"主人，现在 {hour:02d}:{minute:02d} 了，"
                       f"按您 {commute} 分钟通勤时间，差不多该出发了哦{sensor_hint}，需要我拉开窗帘吗？",
            "reason": f"日程提醒：{lh:02d}:{lm:02d}出门，通勤{commute}分钟",
            "actions": [{"device": "living_curtain", "action": "open"}],
        }
    return None


def _check_wake_reminder(hour: int, minute: int, schedule: dict,
                         sensors: dict) -> dict | None:
    """检查是否需要起床提醒"""
    if "wake_hour" not in schedule:
        return None

    wh = schedule["wake_hour"]
    wm = schedule.get("wake_minute", 0)
    current_total = hour * 60 + minute
    wake_total = wh * 60 + wm

    # 在起床时间 ±2 分钟内，且窗帘还没拉开
    if abs(current_total - wake_total) <= 2:
        curtain_closed = sensors.get("living_curtain", {}).get("value", "").lower() == "closed"
        if curtain_closed:
            return {
                "id": "wake_reminder",
                "message": f"主人，早上好！已经 {hour:02d}:{minute:02d} 了，"
                           f"需要我拉开窗帘、打开卧室灯吗？",
                "reason": "起床提醒",
                "actions": [
                    {"device": "living_curtain", "action": "open"},
                    {"device": "bedroom_light", "action": "on"},
                ],
            }
    return None


def _check_night_mode(hour: int, minute: int, schedule: dict,
                      sensors: dict, devices: dict | None = None) -> dict | None:
    """检查是否到睡眠时间但灯还开着"""
    if "sleep_hour" not in schedule or not devices:
        return None

    sh = schedule["sleep_hour"]
    sm = schedule.get("sleep_minute", 0)
    current_total = hour * 60 + minute
    sleep_total = sh * 60 + sm

    if current_total >= sleep_total and current_total <= sleep_total + 30:
        # 检查是否有灯还开着
        lights_on = [d for d in devices.values()
                     if d.get("type") == "light" and d.get("status") == "on"]
        if lights_on:
            names = "、".join(d.get("name", "") for d in lights_on[:3])
            return {
                "id": "night_reminder",
                "message": f"主人，已经 {hour:02d}:{minute:02d} 了，"
                           f"{names}还开着，需要我帮你关掉吗？",
                "reason": "睡眠提醒：检测到灯未关",
                "actions": [{"device": d.get("device_id", ""), "action": "off"}
                            for d in lights_on[:3]],
            }
    return None


class ContextEngine:
    """
    情境引擎：定期检查环境状态，匹配规则后推送主动提醒。

    检查间隔：每 30 真实秒一次（受虚拟时间影响）。
    """

    def __init__(self, get_virtual_time, get_memory_engine, get_virtual_home):
        self._running = False
        self._task: asyncio.Task | None = None
        self._get_time = get_virtual_time
        self._get_memory = get_memory_engine
        self._get_home = get_virtual_home
        self._alert_callback: Callable | None = None
        self._suppressed = False
        self._last_alert_times: dict[str, float] = {}

    def set_alert_callback(self, callback: Callable):
        """设置提醒推送回调，接收 alert dict"""
        self._alert_callback = callback

    async def _check_cycle(self):
        """单次检查所有规则"""
        if self._suppressed:
            return

        vt = self._get_time()
        now = vt.now_struct()
        hour = now["hour"]
        minute = now["minute"]
        memories = self._get_memory().get_all()
        home = self._get_home()

        # 提取 sensors 和 devices
        sensors = home.sensors if hasattr(home, 'sensors') else {}
        devices = home.get_all_states() if hasattr(home, 'get_all_states') else {}

        schedule = await _parse_schedule(memories)

        rules = [
            ("leave_reminder", _check_leave_reminder),
            ("wake_reminder", _check_wake_reminder),
        ]

        for rule_id, check_fn in rules:
            result = check_fn(hour, minute, schedule, sensors)
            if result and self._alert_callback:
                last = self._last_alert_times.get(rule_id, 0)
                if time.time() - last < 900:
                    continue
                self._last_alert_times[rule_id] = time.time()
                result["timestamp"] = time.time()
                logger.info(f"情境引擎触发: {rule_id} — {result['reason']}")
                await self._alert_callback(result)

        # _check_night_mode 需要额外 devices 参数，单独处理
        night_result = _check_night_mode(hour, minute, schedule, sensors, devices)
        if night_result and self._alert_callback:
            last = self._last_alert_times.get("night_reminder", 0)
            if time.time() - last >= 900:
                self._last_alert_times["night_reminder"] = time.time()
                night_result["timestamp"] = time.time()
                logger.info(f"情境引擎触发: night_reminder — {night_result['reason']}")
                await self._alert_callback(night_result)

    async def start(self, interval_seconds: int = 30):
        """启动循环检查"""
        if self._running:
            return
        self._running = True
        logger.info(f"情境引擎已启动，检查间隔={interval_seconds}s")

        async def _loop():
            while self._running:
                try:
                    await self._check_cycle()
                except Exception as e:
                    logger.error(f"情境引擎检查异常: {e}")
                await asyncio.sleep(interval_seconds)

        self._task = asyncio.create_task(_loop())

    async def stop(self):
        """停止检查"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("情境引擎已停止")

    @property
    def suppressed(self) -> bool:
        return self._suppressed

    def set_suppressed(self, val: bool):
        self._suppressed = val
        logger.info(f"主动提醒{'已关闭' if val else '已开启'}")


# 全局单例
_engine: ContextEngine | None = None


def get_context_engine(get_virtual_time=None, get_memory_engine=None, get_virtual_home=None) -> ContextEngine:
    global _engine
    if _engine is None:
        _engine = ContextEngine(get_virtual_time, get_memory_engine, get_virtual_home)
    return _engine
