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
  return (
    <div className="border-t border-white/5">
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-5 py-3 hover:bg-white/[0.02] transition-colors text-xs text-white/40">
        <FiHardDrive size={14} /> 记忆
        {entries.length > 0 && <span className="ml-auto bg-rose-500/10 text-rose-400 text-[10px] px-2 py-0.5 rounded-full">{entries.length}</span>}
      </button>
      {open && (
        <div className="max-h-52 overflow-y-auto">
          <div className="flex items-center gap-2 px-4 py-2 border-b border-white/[0.02]">
            <button onClick={onRefresh} className="text-[10px] text-white/25 hover:text-white/50 px-2 py-1 rounded flex items-center gap-1"><FiRefreshCw size={10} /> 刷新</button>
            {entries.length > 0 && (
              <button onClick={() => setShowClear(true)} className="text-[10px] text-red-400/50 hover:text-red-400 px-2 py-1 rounded ml-auto flex items-center gap-1"><FiAlertTriangle size={10} /> Clear</button>
            )}
          </div>
          {showClear && (
            <div className="px-4 py-2 bg-red-500/5 border-b border-red-500/10 text-[11px] text-red-400 flex items-center gap-2">
              确定清除全部？
              <button onClick={()=>{onClearAll();setShowClear(false)}} className="px-2 py-0.5 bg-red-500/20 rounded text-[10px]">确认</button>
              <button onClick={()=>setShowClear(false)} className="px-2 py-0.5 bg-white/5 rounded text-[10px]">取消</button>
            </div>
          )}
          {entries.length === 0 ? (
            <div className="py-6 text-center text-[11px] text-white/15">暂无记忆</div>
          ) : entries.map(e => (
            <div key={e.id} className="flex items-start gap-3 px-4 py-2.5 border-b border-white/[0.02] hover:bg-white/[0.02]">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-400 font-medium">{catLabels[e.category]||e.category}</span>
                  <span className="text-[11px] text-white/70">{e.value}</span>
                </div>
                <div className="text-[10px] text-white/15 mt-0.5 truncate">{e.source.slice(0,50)}</div>
              </div>
              <button onClick={()=>onDelete(e.id)} className="text-white/15 hover:text-red-400 p-1"><FiTrash2 size={12} /></button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
