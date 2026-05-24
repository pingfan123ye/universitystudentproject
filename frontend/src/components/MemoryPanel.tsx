import { useState } from 'react';
import { FiHardDrive, FiTrash2, FiRefreshCw, FiAlertTriangle } from 'react-icons/fi';

interface MemoryEntry { id: number; category: string; value: string; source: string; created_at: number; }
interface Props { entries: MemoryEntry[]; onRefresh: () => void; onDelete: (id: number) => void; onClearAll: () => void; }

const catLabels: Record<string, string> = {
  name:'姓名',schedule:'日程',preference:'偏好',location:'位置',contact:'联系',pet:'宠物',
  job:'工作',age:'年龄',learning:'学习',family:'家庭',health:'健康',
};

export default function MemoryPanel({ entries, onRefresh, onDelete, onClearAll }: Props) {
  const [open, setOpen] = useState(false);
  const [showClear, setShowClear] = useState(false);
  const s = { borderColor: 'var(--border)', color: 'var(--text-muted)', background: 'var(--bg-elevated)' };
  return (
    <div className="border-t" style={{ borderColor: 'var(--border)' }}>
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-5 py-2.5 text-xs font-medium transition-colors" style={s}>
        <FiHardDrive size={14} /> 记忆
        {entries.length > 0 && <span className="ml-auto bg-pink-100 text-pink-700 text-[10px] px-2 py-0.5 rounded-full">{entries.length}</span>}
      </button>
      {open && (
        <div className="max-h-48 overflow-y-auto">
          <div className="flex px-4 py-1.5 border-b" style={{ borderColor: 'var(--border)' }}>
            <button onClick={onRefresh} className="text-[10px] px-2 py-1 rounded flex items-center gap-1" style={{ color: 'var(--text-muted)' }}><FiRefreshCw size={10} /> 刷新</button>
            {entries.length > 0 && (
              <button onClick={() => setShowClear(true)} className="text-[10px] px-2 py-1 rounded ml-auto flex items-center gap-1 text-red-400"><FiAlertTriangle size={10} /> 清除</button>
            )}
          </div>
          {showClear && (
            <div className="px-4 py-2 border-b text-[11px] flex items-center gap-2" style={{ background: 'var(--accent-glow)', borderColor: 'var(--accent)' }}>
              确定清除全部？
              <button onClick={()=>{onClearAll();setShowClear(false)}} className="px-2 py-0.5 rounded text-white text-[10px]" style={{ background: 'var(--accent)' }}>确认</button>
              <button onClick={()=>setShowClear(false)} className="px-2 py-0.5 rounded border text-[10px]" style={{ borderColor: 'var(--border)' }}>取消</button>
            </div>
          )}
          {entries.length === 0 ? (
            <div className="py-6 text-center text-[11px]" style={{ color: 'var(--text-muted)' }}>暂无</div>
          ) : entries.map(e => (
            <div key={e.id} className="flex items-start gap-3 px-4 py-2.5 border-b hover:opacity-80" style={{ borderColor: 'var(--border)' }}>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-pink-100 text-pink-700 font-medium">{catLabels[e.category]||e.category}</span>
                  <span className="text-[11px]" style={{ color: 'var(--text-primary)' }}>{e.value}</span>
                </div>
                <div className="text-[10px] truncate mt-0.5" style={{ color: 'var(--text-muted)' }}>{e.source.slice(0, 50)}</div>
              </div>
              <button onClick={()=>onDelete(e.id)} className="p-1 hover:text-red-500" style={{ color: 'var(--text-muted)' }}><FiTrash2 size={12} /></button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
