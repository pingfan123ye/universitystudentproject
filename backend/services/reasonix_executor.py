"""
Reasonix 执行器 —— 封装 reasonix CLI 调用，支持待审批任务队列 + 流式输出
当用户通过"工作助手"/"克劳德"/"贾维斯"触发编程任务时，先创建待审批任务，
用户说"允许"后才执行 reasonix run。
"""
import asyncio
import json
import os
import shutil
import time
import sys
from typing import AsyncIterator
from dataclasses import dataclass, field


# ===== 待审批任务管理 =====

@dataclass
class PendingTask:
    """一个待审批的 Reasonix 编程任务"""
    id: str
    prompt: str
    created_at: float = 0
    approved: bool = False


class PendingTaskManager:
    """管理待审批任务队列（先进先出）"""

    def __init__(self):
        self._tasks: list[PendingTask] = []

    def add(self, prompt: str, task_id: str | None = None) -> PendingTask:
        task = PendingTask(
            id=task_id or f"task_{int(time.time())}",
            prompt=prompt,
            created_at=time.time(),
        )
        self._tasks.append(task)
        return task

    def pop_next(self) -> PendingTask | None:
        """取出并移除最早的一个待审批任务"""
        if not self._tasks:
            return None
        task = self._tasks.pop(0)
        task.approved = True
        return task

    def peek(self) -> PendingTask | None:
        """查看最早的一个待审批任务，不移除"""
        return self._tasks[0] if self._tasks else None

    def count(self) -> int:
        return len(self._tasks)

    def clear(self):
        self._tasks.clear()

    def list_all(self) -> list[dict]:
        return [
            {"id": t.id, "prompt": t.prompt[:80], "created_at": t.created_at}
            for t in self._tasks
        ]


# ===== Reasonix CLI 封装 =====

def find_reasonix() -> str | None:
    """查找可用的 reasonix 命令，返回完整路径"""
    cmd = shutil.which("reasonix")
    if cmd:
        return cmd
    # npm 全局安装的常见位置
    for p in [
        os.path.expanduser("~/AppData/Roaming/npm/reasonix.cmd"),
        os.path.expanduser("~/AppData/Roaming/npm/reasonix"),
        os.path.expanduser("~/AppData/Local/npm/reasonix.cmd"),
        r"C:\Program Files\nodejs\reasonix.cmd",
    ]:
        if os.path.exists(p):
            return p
    return None


def is_reasonix_available() -> bool:
    """检查 reasonix CLI 是否已安装"""
    return find_reasonix() is not None


def _resolve_api_key() -> str | None:
    """从项目配置或环境变量中读取 DeepSeek API Key"""
    # 1. 优先环境变量
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return env_key
    # 2. 从项目 .reasonix/config.json 读取
    try:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/.. -> 项目根
        cfg_path = os.path.join(project_root, ".reasonix", "config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
                return cfg.get("apiKey") or cfg.get("api_key")
    except Exception:
        pass
    return None


async def execute(prompt: str, cwd: str | None = None) -> AsyncIterator[str]:
    """
    执行 reasonix run <prompt>，流式返回输出。
    自动读取 DeepSeek API Key 注入子进程环境变量。
    """
    reasonix_path = find_reasonix()
    if not reasonix_path:
        yield "Reasonix CLI 未安装。\n"
        yield "安装: npm install -g reasonix\n"
        return

    work_dir = cwd or os.getcwd()

    # 准备环境变量（注入 API Key）
    env = os.environ.copy()
    api_key = _resolve_api_key()
    if api_key and "DEEPSEEK_API_KEY" not in env:
        env["DEEPSEEK_API_KEY"] = api_key

    try:
        # reasonix run <task> — 流式输出到 stdout
        # 使用 -m/--model 指定快速模型
        proc = await asyncio.create_subprocess_exec(
            reasonix_path, "run", prompt,
            "--model", "deepseek-v4-flash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=work_dir,
            env=env,
        )

        if proc.stdout:
            async for line in proc.stdout:
                yield line.decode("utf-8", errors="replace")

        await proc.wait()

        if proc.returncode != 0:
            yield f"\n(Reasonix 退出码: {proc.returncode})"

    except FileNotFoundError:
        yield f"找不到 Reasonix CLI: {reasonix_path}\n"
    except Exception as e:
        yield f"Reasonix 执行异常: {str(e)}\n"


# ===== 全局单例 =====
_pending_manager: PendingTaskManager | None = None


def get_pending_manager() -> PendingTaskManager:
    global _pending_manager
    if _pending_manager is None:
        _pending_manager = PendingTaskManager()
    return _pending_manager
