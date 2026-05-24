"""
设备控制服务 —— 虚拟设备执行 + 音乐控制 + 场景
"""
from models.virtual_home import VirtualHome


def execute(
    virtual_home: VirtualHome,
    device_actions: list[dict],
    text: str = "",
    matched_key: str = "",
    music_action: dict | None = None,
) -> dict:
    """
    Returns: {"reply": str, "handled": bool, "results": list, "path": "xiaoai", "music_action": dict|None}
    """
    results = []
    handled = False
    reply = ""

    # ---- 1. 设备控制 ----
    if device_actions:
        replies = []
        for action in device_actions:
            result = virtual_home.execute(
                action["device"], action["action"], action.get("params", {})
            )
            results.append(result)
            if result.get("success"):
                handled = True
                device_name = result.get("device_name", action["device"])
                if result.get("device_type") == "curtain":
                    verb = "拉开" if action["action"] in ("on", "open") else "关上"
                    replies.append(f"已{verb}{device_name}")
                else:
                    verb = "打开" if action["action"] in ("on",) else "关闭" if action["action"] in ("off",) else "调节"
                    replies.append(f"已{verb}{device_name}")
            else:
                replies.append(f"未找到{action['device']}")
        reply = "，".join(replies)

    # ---- 2. 音乐控制 ----
    if music_action:
        handled = True
        action = music_action["action"]
        music_replies = {
            "play": "好的，正在为你播放音乐",
            "pause": "好的，已暂停播放",
            "next": "好的，已切换到下一首",
            "prev": "好的，已切换到上一首",
        }
        music_part = music_replies.get(action, "好的")
        reply = f"{reply}；{music_part}" if reply else music_part

    # ---- 3. 场景 ----
    if not handled and any(kw in (matched_key + text) for kw in ["晚安", "离家", "回家", "起床"]):
        scene_map = {
            "晚安": [("bedroom_light", "off"), ("living_light", "off"), ("living_curtain", "close")],
            "离家": [("bedroom_light", "off"), ("living_light", "off"), ("kitchen_light", "off"),
                     ("ac", "off"), ("water_heater", "off"), ("living_curtain", "close")],
            "回家": [("living_light", "on"), ("living_curtain", "open"), ("ac", "on")],
            "起床": [("bedroom_light", "on"), ("living_curtain", "open")],
        }
        for sname, actions in scene_map.items():
            if sname in text or sname in matched_key:
                for device_id, operation in actions:
                    results.append(virtual_home.execute(device_id, operation))
                reply = f"好的，已启用{sname}模式"
                handled = True
                break

    # ---- 4. 兜底 ----
    if not handled:
        return {"reply": "", "handled": False, "results": [], "path": "xiaoai", "music_action": None}

    return {
        "reply": reply,
        "handled": True,
        "results": results,
        "path": "xiaoai",
        "music_action": music_action,
    }
