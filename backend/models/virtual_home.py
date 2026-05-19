"""
虚拟家居设备层 —— 用 Python 字典模拟米家设备状态，
提供与真实米家 API 参数完全一致的接口。
"""
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
