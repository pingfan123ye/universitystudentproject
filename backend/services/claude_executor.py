"""
Claude Code 执行器 —— subprocess 调用 claude CLI，流式输出结果
"""
import asyncio
import os
import shutil
import sys
from typing import AsyncIterator


def _find_claude() -> str | None:
    """查找可用的 claude 命令，返回完整路径"""
    # 直接搜索
    cmd = shutil.which("claude")
    if cmd:
        return cmd
    cmd = shutil.which("claude-code")
    if cmd:
        return cmd
    # 通过 npx
    npx = shutil.which("npx")
    if npx:
        return f"{npx} @anthropic-ai/claude-code"
    # 常见安装位置
    for p in [
        os.path.expanduser("~/AppData/Roaming/npm/claude.cmd"),
        os.path.expanduser("~/AppData/Local/npm/claude.cmd"),
        r"C:\Program Files\nodejs\claude.cmd",
    ]:
        if os.path.exists(p):
            return p
    return None


def is_claude_available() -> bool:
    return _find_claude() is not None


async def execute(prompt: str, workspace: str | None = None) -> AsyncIterator[str]:
    """执行 Claude Code CLI，流式返回输出"""
    claude_path = _find_claude()

    if not claude_path:
        yield "Claude Code CLI 未安装。\n"
        yield "安装: npm install -g @anthropic-ai/claude-code\n"
        return

    cwd = workspace or os.getcwd()

    # 构建命令行
    if "npx" in claude_path:
        parts = claude_path.split() + ["-p", prompt]
    else:
        parts = [claude_path, "-p", prompt]

    try:
        if sys.platform == "win32" and not claude_path.startswith("npx"):
            # Windows: .cmd 文件需要通过 shell 或显式调用
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )

        if proc.stdout:
            async for line in proc.stdout:
                yield line.decode("utf-8", errors="replace")

        await proc.wait()

        if proc.returncode != 0:
            yield f"\n(退出码: {proc.returncode})"

    except FileNotFoundError:
        yield f"找不到 Claude CLI: {claude_path}\n"
        yield "请确认已安装: npm install -g @anthropic-ai/claude-code\n"
    except Exception as e:
        yield f"执行异常: {str(e)}\n"
