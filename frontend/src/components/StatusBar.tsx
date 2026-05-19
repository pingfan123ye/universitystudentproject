import { ConnectionStatus } from '../hooks/useWebSocket';
import { RoutePath } from '../types';

interface StatusBarProps {
  status: ConnectionStatus;
  modelName?: string;
  lastPath?: RoutePath;
}

const statusMap: Record<ConnectionStatus, { label: string; dot: string }> = {
  connected:    { label: '在线', dot: 'bg-accent-green dot-amber' },
  connecting:   { label: '连接中', dot: 'bg-accent-amber animate-breathe' },
  disconnected: { label: '离线', dot: 'bg-accent-rose' },
};

const pathLabels: Record<string, { text: string; bg: string; textColor: string }> = {
  xiaoai:  { text: '小爱', bg: 'bg-blue-500/10', textColor: 'text-blue-400' },
  llm:     { text: 'AI', bg: 'bg-purple-500/10', textColor: 'text-purple-400' },
  reasonix:{ text: 'Reasonix', bg: 'bg-teal-500/10', textColor: 'text-teal-400' },
  cache:   { text: '缓存', bg: 'bg-green-500/10', textColor: 'text-green-400' },
  unknown: { text: '--', bg: 'bg-white/5', textColor: 'text-gray-500' },
};

export default function StatusBar({ status, modelName = 'Qwen2.5:7B', lastPath = 'unknown' }: StatusBarProps) {
  const s = statusMap[status];
  const p = pathLabels[lastPath] || pathLabels.unknown;

  return (
    <header className="flex items-center gap-3 px-5 py-2.5 border-b border-white/5 bg-surface-1/80 backdrop-blur-xl text-xs">
      <span className={`inline-block w-2 h-2 rounded-full ${s.dot}`} />
      <span className="font-medium text-white/80 tracking-wide">{s.label}</span>
      <span className="text-white/10">·</span>
      <span className="text-white/40 font-mono text-[11px]">{modelName}</span>
      <span className="text-white/10">·</span>
      <span className={`px-2 py-0.5 rounded-full text-[11px] font-medium ${p.bg} ${p.textColor}`}>
        {p.text}
      </span>
      <div className="flex-1" />
      <span className="text-white/20 text-[11px] tracking-widest uppercase">Voice Hub</span>
    </header>
  );
}
