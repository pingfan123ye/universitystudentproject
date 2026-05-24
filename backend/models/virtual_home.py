"""
虚拟家居设备层 —— 用 Python 字典模拟米家设备状态，
提供与真实米家 API 参数完全一致的接口。
包含虚拟时间系统，支持加速/暂停/设定。
"""
import time as real_time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeviceState:
    device_id: str
    name: str          # 中文名
    device_type: str   # light / curtain / heater / sensor
    room: str          # bedroom / living / kitchen / bathroom / study
    status: str        # on / off / open / closed
    properties: dict = field(default_factory=dict)  # 额外属性


class VirtualHome:
    """虚拟家庭设备管理器"""

    def __init__(self):
        self.devices: dict[str, DeviceState] = {
            "bedroom_light": DeviceState(
                device_id="bedroom_light", name="卧室灯", device_type="light",
                room="bedroom", status="off", properties={"brightness": 100, "color": "warm"}
            ),
            "living_light": DeviceState(
                device_id="living_light", name="客厅灯", device_type="light",
                room="living", status="off", properties={"brightness": 80, "color": "white"}
            ),
            "kitchen_light": DeviceState(
                device_id="kitchen_light", name="厨房灯", device_type="light",
                room="kitchen", status="off", properties={"brightness": 100, "color": "white"}
            ),
            "bathroom_light": DeviceState(
                device_id="bathroom_light", name="卫生间灯", device_type="light",
                room="bathroom", status="off", properties={"brightness": 100, "color": "white"}
            ),
            "study_light": DeviceState(
                device_id="study_light", name="书房灯", device_type="light",
                room="study", status="off", properties={"brightness": 80, "color": "white"}
            ),
            "living_curtain": DeviceState(
                device_id="living_curtain", name="客厅窗帘", device_type="curtain",
                room="living", status="closed", properties={"position": 0}
            ),
            "water_heater": DeviceState(
                device_id="water_heater", name="热水器", device_type="heater",
                room="bathroom", status="off", properties={"temperature": 40, "mode": "eco"}
            ),
            "ac": DeviceState(
                device_id="ac", name="空调", device_type="ac",
                room="living", status="off", properties={"temperature": 26, "mode": "cool"}
            ),
        }

        # 传感器
        self.sensors: dict[str, dict[str, Any]] = {
            "bedroom_temp": {"value": 25.0, "unit": "celsius"},
            "living_temp": {"value": 26.5, "unit": "celsius"},
            "bedroom_motion": {"value": "no_person"},
            "bedroom_door": {"value": "closed"},
            "phone_charging": {"value": True},
        }

    def execute(self, device_id: str, action: str, params: dict | None = None) -> dict:
        """
        执行设备操作，参数与真实米家 API 一致。

        Returns:
            {"success": bool, "device_id": str, "action": str, "previous_state": str, "new_state": str}
        """
        if device_id not in self.devices:
            return {"success": False, "error": f"未知设备: {device_id}"}

        device = self.devices[device_id]
        previous_state = device.status

        # 执行操作
        if device.device_type in ("light", "heater", "ac", "fan", "tv"):
            if action == "on":
                device.status = "on"
            elif action == "off":
                device.status = "off"
            elif action == "toggle":
                device.status = "on" if device.status == "off" else "off"
        elif device.device_type == "curtain":
            if action == "on" or action == "open":
                device.status = "open"
                device.properties["position"] = 100
            elif action == "off" or action == "close":
                device.status = "closed"
                device.properties["position"] = 0
            elif action == "stop":
                device.status = "stopped"

        # 处理额外参数
        if params:
            device.properties.update(params)

        return {
            "success": True,
            "device_id": device_id,
            "device_name": device.name,
            "action": action,
            "previous_state": previous_state,
            "new_state": device.status,
            "room": device.room,
            "device_type": device.device_type,
        }

    def get_all_states(self) -> dict[str, dict]:
        """获取所有设备状态（用于前端同步）"""
        return {
            did: {
                "name": d.name, "type": d.device_type, "room": d.room,
                "status": d.status, "properties": d.properties,
            }
            for did, d in self.devices.items()
        }

    def get_device(self, device_id: str) -> dict | None:
        """获取单个设备状态"""
        if device_id in self.devices:
            d = self.devices[device_id]
            return {
                "name": d.name, "type": d.device_type, "room": d.room,
                "status": d.status, "properties": d.properties,
            }
        return None


# ═══════════════════════════════════════
# 虚拟时间系统
# ═══════════════════════════════════════

class VirtualTime:
    """
    模拟时间系统，支持加速/暂停/手动设定。

    工作原理：
    - simulated = base_simulated + (real_now - base_real) * speed
    - 暂停时模拟时间停止流逝
    """

    def __init__(self):
        self._simulated = False        # 是否启用模拟时间
        self._base_real = 0.0          # 上次变更时的真实时间戳
        self._base_simulated = 0.0     # 上次变更时的模拟时间戳
        self._speed = 1.0              # 加速比 (1=实时, 60=1秒=1分钟)
        self._paused = False           # 是否暂停

        # 默认初始化为当前真实时间
        now = real_time.time()
        self._base_real = now
        self._base_simulated = now

    @property
    def simulated(self) -> bool:
        return self._simulated

    def now(self) -> float:
        """获取当前（模拟）时间戳"""
        if not self._simulated or self._paused:
            return self._base_simulated
        elapsed = (real_time.time() - self._base_real) * self._speed
        return self._base_simulated + elapsed

    def now_struct(self) -> dict:
        """返回格式化的当前时间"""
        t = self.now()
        import datetime
        dt = datetime.datetime.fromtimestamp(t)
        return {
            "simulated": self._simulated,
            "current_time": dt.strftime("%H:%M"),
            "current_datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "speed": self._speed,
            "paused": self._paused,
            "hour": dt.hour,
            "minute": dt.minute,
        }

    def enable(self, enabled: bool = True):
        """启用/禁用模拟时间"""
        if enabled == self._simulated:
            return
        now = real_time.time()
        if enabled:
            # 从当前真实时间开始模拟
            self._base_real = now
            self._base_simulated = now
            self._simulated = True
        else:
            # 禁用：将模拟时间的当前值冻结为基准
            self._base_simulated = self.now()
            self._base_real = now
            self._simulated = False

    def set_time(self, target_hour: int, target_minute: int = 0):
        """手动设定模拟时间"""
        import datetime
        now = self.now()
        base_dt = datetime.datetime.fromtimestamp(now)
        # 保持日期不变，只改小时和分钟
        new_dt = base_dt.replace(hour=target_hour % 24, minute=target_minute % 60, second=0, microsecond=0)
        real_now = real_time.time()
        self._base_simulated = new_dt.timestamp()
        self._base_real = real_now
        self._simulated = True

    def set_speed(self, speed: float):
        """设置加速比 (0.1 ~ 300)"""
        # 调整前先更新基准
        now_real = real_time.time()
        self._base_simulated = self.now()
        self._base_real = now_real
        self._speed = max(0.1, min(300.0, speed))

    def toggle_pause(self) -> bool:
        """暂停/继续，返回新状态"""
        if self._paused:
            # 恢复：更新基准
            now_real = real_time.time()
            self._base_real = now_real
            self._paused = False
        else:
            # 暂停：冻结当前模拟时间
            self._base_simulated = self.now()
            self._paused = True
        return self._paused

    def to_dict(self) -> dict:
        """前端可用的时间状态"""
        t = self.now()
        import datetime
        dt = datetime.datetime.fromtimestamp(t)
        return {
            "simulated": self._simulated,
            "current_time": dt.strftime("%H:%M"),
            "speed": self._speed,
            "paused": self._paused,
            "hour": dt.hour,
            "minute": dt.minute,
        }


# 全局单例
_virtual_time: VirtualTime | None = None


def get_virtual_time() -> VirtualTime:
    global _virtual_time
    if _virtual_time is None:
        _virtual_time = VirtualTime()
    return _virtual_time
