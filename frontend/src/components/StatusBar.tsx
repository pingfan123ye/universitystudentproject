import { ConnectionStatus } from '../hooks/useWebSocket';
import { RoutePath } from '../types';
import { useTheme } from '../hooks/useTheme';
import { FiSun, FiMoon, FiBell, FiBellOff } from 'react-icons/fi';

interface StatusBarProps {
  status: ConnectionStatus;
  modelName?: string;
  lastPath?: RoutePath;
  alertsEnabled?: boolean;
  onToggleAlerts?: (enabled: boolean) => void;
}

const statusMap: Record<ConnectionStatus, { label: string; cls: string }> = {
  connected:    { label: '在线', cls: 'bg-green-400' },
  connecting:   { label: '连接中', cls: 'bg-yellow-400 animate-pulse' },
  disconnected: { label: '离线', cls: 'bg-red-400' },
};

const pathBadge: Record<string, { text: string; cls: string }> = {
  xiaoai:  { text: '小爱', cls: 'bg-blue-100 text-blue-700' },
  llm:     { text: 'AI', cls: 'bg-purple-100 text-purple-700' },
  reasonix:{ text: 'Reasonix', cls: 'bg-teal-100 text-teal-700' },
  cache:   { text: '缓存', cls: 'bg-green-100 text-green-700' },
  unknown: { text: '--', cls: 'bg-gray-100 text-gray-500' },
};

export default function StatusBar({ status, modelName = 'Qwen2.5:7B', lastPath = 'unknown', alertsEnabled = true, onToggleAlerts }: StatusBarProps) {
  const s = statusMap[status];
  const p = pathBadge[lastPath] || pathBadge.unknown;
  const { theme, toggle } = useTheme();

  return (
    <header className="flex items-center gap-3 px-5 py-2 text-xs font-medium border-b" style={{ background: 'var(--bg-surface)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}>
      <span className={`inline-block w-2 h-2 rounded-full ${s.cls}`} />
      <span style={{ color: 'var(--text-secondary)' }}>{s.label}</span>
      <span style={{ color: 'var(--border-strong)' }}>|</span>
      <span className="font-mono text-[11px]" style={{ color: 'var(--text-muted)' }}>{modelName}</span>
      <span style={{ color: 'var(--border-strong)' }}>|</span>
      <span className={`px-2 py-0.5 rounded text-[11px] font-medium ${p.cls}`}>{p.text}</span>
      <div className="flex-1" />
      {onToggleAlerts && (
        <button onClick={() => onToggleAlerts(!alertsEnabled)} className="flex items-center gap-1 px-2 py-1 rounded hover:opacity-80 transition-opacity" style={{ color: alertsEnabled ? 'var(--accent)' : 'var(--text-muted)' }} title={alertsEnabled ? '关闭提醒' : '开启提醒'}>
          {alertsEnabled ? <FiBell size={14} /> : <FiBellOff size={14} />}
        </button>
      )}
      <button onClick={toggle} className="flex items-center gap-1 px-2 py-1 rounded hover:opacity-80 transition-opacity" style={{ color: 'var(--accent)' }} title={theme === 'dark' ? '切换浅色' : '切换深色'}>
        {theme === 'dark' ? <FiSun size={14} /> : <FiMoon size={14} />}
      </button>
      <span className="tracking-widest text-[10px]" style={{ color: 'var(--text-muted)' }}>VOICE HUB</span>
    </header>
  );
}
