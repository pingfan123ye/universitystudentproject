import { useState } from 'react';
import { FiActivity, FiChevronDown, FiChevronRight } from 'react-icons/fi';

interface RouteEntry { time: number; text: string; path: string; reason: string; }
interface Props { entries: RouteEntry[]; }

const badge: Record<string, string> = {
  xiaoai: 'bg-blue-500/10 text-blue-400', llm: 'bg-purple-500/10 text-purple-400',
  reasonix: 'bg-teal-500/10 text-teal-400', cache: 'bg-green-500/10 text-green-400',
};

export default function RouteLog({ entries }: Props) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-white/5">
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-5 py-3 hover:bg-white/[0.02] transition-colors text-xs text-white/40">
        <FiActivity size={14} /> 路由日志
        {entries.length > 0 && <span className="ml-auto text-white/15">{entries.length}</span>}
        <span className="text-white/10">{open ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}</span>
      </button>
      {open && (
        <div className="max-h-44 overflow-y-auto">
          {entries.length === 0 ? (
            <div className="py-6 text-center text-[11px] text-white/15">暂无路由记录</div>
          ) : entries.map((e, i) => (
            <div key={`${e.time}-${i}`} className="flex items-center gap-2 px-4 py-1.5 border-b border-white/[0.02] text-[10px]">
              <span className="text-white/15 w-14 flex-shrink-0 font-mono">{new Date(e.time).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'})}</span>
              <span className={`px-1.5 py-0.5 rounded font-medium ${badge[e.path] || 'bg-white/5 text-white/30'}`}>{e.path}</span>
              <span className="text-white/25 truncate">{e.reason}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
