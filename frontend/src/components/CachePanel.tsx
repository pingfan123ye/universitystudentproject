import { useState } from 'react';
import { CacheEntry } from '../types';
import { FiDatabase, FiTrash2, FiRefreshCw } from 'react-icons/fi';

interface Props { entries: CacheEntry[]; onRefresh: () => void; onDelete: (id: string) => void; }

export default function CachePanel({ entries, onRefresh, onDelete }: Props) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-white/5">
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-5 py-3 hover:bg-white/[0.02] transition-colors text-xs text-white/40">
        <FiDatabase size={14} /> 缓存
        {entries.length > 0 && <span className="ml-auto bg-purple-500/10 text-purple-400 text-[10px] px-2 py-0.5 rounded-full">{entries.length}</span>}
      </button>
      {open && (
        <div className="max-h-52 overflow-y-auto">
          <div className="flex items-center gap-2 px-4 py-2 border-b border-white/[0.02]">
            <button onClick={onRefresh} className="text-[10px] text-white/25 hover:text-white/50 px-2 py-1 rounded flex items-center gap-1"><FiRefreshCw size={10} /> 刷新</button>
          </div>
          {entries.length === 0 ? (
            <div className="py-6 text-center text-[11px] text-white/15">暂无缓存</div>
          ) : entries.map(e => (
            <div key={e.id} className="flex items-start gap-3 px-4 py-2.5 border-b border-white/[0.02] hover:bg-white/[0.02]">
              <div className="flex-1 min-w-0">
                <div className="text-[11px] text-white/60 truncate">{e.original_text}</div>
                <div className="text-[10px] text-white/20 mt-0.5 truncate">{e.reply.slice(0,40)}</div>
                <div className="text-[10px] text-white/15 mt-0.5">hits: {e.hit_count}</div>
              </div>
              <button onClick={() => onDelete(e.id)} className="text-white/15 hover:text-red-400 p-1"><FiTrash2 size={12} /></button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
