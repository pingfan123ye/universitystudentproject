import { useState } from 'react';
import { CacheEntry } from '../types';
import { FiDatabase, FiTrash2, FiRefreshCw } from 'react-icons/fi';

interface Props { entries: CacheEntry[]; onRefresh: () => void; onDelete: (id: string) => void; }

export default function CachePanel({ entries, onRefresh, onDelete }: Props) {
  const [open, setOpen] = useState(false);
  const s = { borderColor: 'var(--border)', color: 'var(--text-muted)', background: 'var(--bg-elevated)' };
  return (
    <div className="border-t" style={{ borderColor: 'var(--border)' }}>
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-5 py-2.5 text-xs font-medium transition-colors" style={s}>
        <FiDatabase size={14} /> 缓存
        {entries.length > 0 && <span className="ml-auto bg-purple-100 text-purple-700 text-[10px] px-2 py-0.5 rounded-full">{entries.length}</span>}
      </button>
      {open && (
        <div className="max-h-48 overflow-y-auto">
          <div className="flex px-4 py-1.5 border-b" style={{ borderColor: 'var(--border)' }}>
            <button onClick={onRefresh} className="text-[10px] px-2 py-1 rounded flex items-center gap-1" style={{ color: 'var(--text-muted)' }}><FiRefreshCw size={10} /> 刷新</button>
          </div>
          {entries.length === 0 ? (
            <div className="py-6 text-center text-[11px]" style={{ color: 'var(--text-muted)' }}>暂无</div>
          ) : entries.map(e => (
            <div key={e.id} className="flex items-start gap-3 px-4 py-2.5 border-b hover:opacity-80" style={{ borderColor: 'var(--border)' }}>
              <div className="flex-1 min-w-0">
                <div className="text-[11px] truncate" style={{ color: 'var(--text-primary)' }}>{e.original_text}</div>
                <div className="text-[10px] truncate mt-0.5" style={{ color: 'var(--text-muted)' }}>{e.reply.slice(0, 40)}</div>
                <div className="text-[10px] mt-0.5" style={{ color: 'var(--text-muted)' }}>命中 {e.hit_count} 次</div>
              </div>
              <button onClick={() => onDelete(e.id)} className="p-1 hover:text-red-500" style={{ color: 'var(--text-muted)' }}><FiTrash2 size={12} /></button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
