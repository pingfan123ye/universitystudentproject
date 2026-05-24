import { useState } from 'react';
import { FiActivity, FiChevronDown, FiChevronRight } from 'react-icons/fi';

interface RouteEntry { time: number; text: string; path: string; reason: string; }
interface Props { entries: RouteEntry[]; }

const badge: Record<string, string> = {
  xiaoai: 'bg-blue-100 text-blue-700', llm: 'bg-purple-100 text-purple-700',
  reasonix: 'bg-teal-100 text-teal-700', cache: 'bg-green-100 text-green-700',
};

export default function RouteLog({ entries }: Props) {
  const [open, setOpen] = useState(false);
  const s = { borderColor: 'var(--border)', color: 'var(--text-muted)', background: 'var(--bg-elevated)' };
  return (
    <div className="border-t" style={{ borderColor: 'var(--border)' }}>
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-5 py-2.5 text-xs font-medium transition-colors"
        style={s}>
        <FiActivity size={14} /> 路由日志
        {entries.length > 0 && <span className="ml-auto text-[10px]" style={{ color: 'var(--text-muted)' }}>{entries.length}</span>}
        {open ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
      </button>
      {open && (
        <div className="max-h-40 overflow-y-auto">
          {entries.length === 0 ? (
            <div className="py-6 text-center text-[11px]" style={{ color: 'var(--text-muted)' }}>暂无</div>
          ) : entries.map((e, i) => (
            <div key={`${e.time}-${i}`} className="flex items-center gap-2 px-4 py-1.5 border-b text-[10px]" style={{ borderColor: 'var(--border)' }}>
              <span className="w-14 font-mono flex-shrink-0" style={{ color: 'var(--text-muted)' }}>{new Date(e.time).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'})}</span>
              <span className={`px-1.5 py-0.5 rounded font-bold ${badge[e.path] || 'bg-gray-100 text-gray-500'}`}>{e.path}</span>
              <span className="truncate" style={{ color: 'var(--text-secondary)' }}>{e.reason}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
