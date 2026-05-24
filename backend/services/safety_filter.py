"""
安全过滤器 —— 对 Shell 命令进行风险分级和二次确认拦截
"""
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RiskAssessment:
    level: str          # low | medium | high
    score: int          # 0-100
    reasons: list[str] = field(default_factory=list)
    command: str = ""
    filtered_command: str = ""  # 去敏感参数后的展示用命令


# ═══════════════════════════════════════
# 高风险模式（必须二次确认）
# ═══════════════════════════════════════

HIGH_RISK_PATTERNS = [
    # 删除/破坏
    (re.compile(r'\brm\s+-rf\b'), "递归强制删除 (rm -rf)"),
    (re.compile(r'\brm\b.*?\s+/\s'), "删除根目录"),
    (re.compile(r'\b(?:del|erase|rmdir)\b', re.IGNORECASE), "删除操作"),
    (re.compile(r'\bformat\b', re.IGNORECASE), "格式化磁盘"),
    (re.compile(r'\b(?:mkfs|fdisk|parted)\b'), "磁盘分区/格式化"),
    # 危险覆盖
    (re.compile(r'(?:>|>>)\s*/dev/'), "写入设备文件"),
    (re.compile(r'(?:>|>>)\s*/(?:etc|boot|sys|proc)/'), "写入系统目录"),
    (re.compile(r'\bdd\b'), "dd 命令（直接磁盘操作）"),
    # 权限/系统
    (re.compile(r'\bsudo\s+rm\b'), "sudo 删除"),
    (re.compile(r'\bchmod\s+777\b'), "授予所有权限 777"),
    (re.compile(r'\bpasswd\b'), "修改密码"),
    (re.compile(r'\b(?:useradd|userdel|usermod)\b'), "用户管理"),
    (re.compile(r'\b(?:shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b'), "系统关机/重启"),
    # 网络攻击
    (re.compile(r'\b(?:nmap|sqlmap|hydra|medusa|aircrack)\b', re.IGNORECASE), "安全扫描/渗透工具"),
    # fork 炸弹
    (re.compile(r':\(\)\s*\{'), "Fork 炸弹检测"),
]

# ═══════════════════════════════════════
# 中风险模式（记录日志）
# ═══════════════════════════════════════

MEDIUM_RISK_PATTERNS = [
    (re.compile(r'\bchmod\b'), "修改文件权限"),
    (re.compile(r'\bchown\b'), "修改文件所有者"),
    (re.compile(r'\bmv\b.*(?:/etc|/usr|/bin|/boot|/lib)'), "移动系统文件"),
    (re.compile(r'\bkill\b'), "终止进程"),
    (re.compile(r'\bsystemctl\s+(?:restart|stop|disable)\b'), "管理系统服务"),
    (re.compile(r'\bservice\s+\w+\s+(?:restart|stop)\b'), "管理系统服务"),
    (re.compile(r'\bdocker\s+(?:rm|stop|kill)\b'), "管理容器"),
    (re.compile(r'\bsource\b'), "加载脚本"),
    (re.compile(r'\bcrontab\b'), "修改定时任务"),
    (re.compile(r'\bwget\b|curl\b.*-o\b'), "下载外部文件"),
]

# ═══════════════════════════════════════
# 安全命令（直接执行）
# ═══════════════════════════════════════

LOW_RISK_PATTERNS = [
    r'\bls\b',
    r'\bcat\b',
    r'\b(?:head|tail)\b',
    r'\b(?:echo|printf)\b',
    r'\b(?:touch|mkdir)\b',
    r'\b(?:cd|pwd)\b',
    r'\b(?:cp|mv)\b(?!.*(?:/etc|/usr|/bin|/boot|/lib))',
    r'\bgrep\b',
    r'\bfind\b',
    r'\b(?:sort|uniq|wc)\b',
    r'\b(?:git|npm|pip|yarn|pnpm|npx)\b',
    r'\bpython\b',
    r'\bnode\b',
]


def assess_risk(command: str) -> RiskAssessment:
    """
    评估 shell 命令的风险等级。

    Args:
        command: 要执行的 shell 命令

    Returns:
        RiskAssessment 包含风险等级、评分和详情
    """
    reasons = []
    max_level = "low"
    score = 0

    if not command.strip():
        return RiskAssessment(level="low", score=0, command=command)

    # 检查高风险
    for pattern, desc in HIGH_RISK_PATTERNS:
        if pattern.search(command):
            reasons.append(f"[高] {desc}")
            max_level = "high"
            score += 40

    # 检查中风险
    for pattern, desc in MEDIUM_RISK_PATTERNS:
        if pattern.search(command):
            reasons.append(f"[中] {desc}")
            if max_level != "high":
                max_level = "medium"
            score += 15

    # 检查低风险（加分降低整体风险）
    low_match = False
    for pattern in LOW_RISK_PATTERNS:
        if re.search(pattern, command):
            low_match = True
            break
    if low_match:
        score = max(0, score - 10)

    score = min(100, max(0, score))

    # 展示用命令（隐藏敏感参数）
    display_cmd = command
    if len(command) > 80:
        display_cmd = command[:80] + "..."

    return RiskAssessment(
        level=max_level,
        score=score,
        reasons=reasons,
        command=command,
        filtered_command=display_cmd,
    )


def requires_confirmation(command: str) -> bool:
    """判断命令是否需要用户二次确认"""
    assessment = assess_risk(command)
    return assessment.level == "high"


def format_confirm_message(assessment: RiskAssessment) -> str:
    """生成供前端展示的确认信息"""
    lines = [
        f"⚠️  检测到高风险命令",
        f"",
        f"命令: `{assessment.filtered_command}`",
    ]
    if assessment.reasons:
        lines.append("")
        lines.append("风险原因:")
        for r in assessment.reasons:
            lines.append(f"  • {r}")
    lines.append("")
    lines.append("回复「确认」执行，回复「取消」拒绝")
    return "\n".join(lines)
