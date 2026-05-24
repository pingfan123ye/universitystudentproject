import { FiAlertTriangle, FiCheck, FiX } from 'react-icons/fi';

interface SafetyDialogProps {
  command: string;
  risk: string;
  reasons?: string[];
  message?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function SafetyDialog({ command, reasons, message, onConfirm, onCancel }: SafetyDialogProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0,0,0,0.6)' }}>
      <div className="w-full max-w-md rounded-xl border shadow-2xl animate-slide-up overflow-hidden"
        style={{ background: 'var(--bg-elevated)', borderColor: 'var(--border)' }}>
        {/* 头部 */}
        <div className="flex items-center gap-3 px-5 py-4 border-b" style={{ borderColor: 'var(--border)', background: 'rgba(239,68,68,0.1)' }}>
          <div className="w-10 h-10 rounded-full flex items-center justify-center" style={{ background: 'rgba(239,68,68,0.2)' }}>
            <FiAlertTriangle size={22} style={{ color: '#ef4444' }} />
          </div>
          <div>
            <div className="text-sm font-bold" style={{ color: '#ef4444' }}>高风险命令</div>
            <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>请确认是否执行此操作</div>
          </div>
        </div>

        {/* 命令内容 */}
        <div className="px-5 py-4">
          <div className="mb-3">
            <div className="text-[10px] font-bold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-muted)' }}>命令</div>
            <div className="font-mono text-[12px] p-3 rounded-lg whitespace-pre-wrap break-all"
              style={{ background: 'var(--bg-input)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}>
              {command}
            </div>
          </div>

          {/* 风险原因 */}
          {reasons && reasons.length > 0 && (
            <div className="mb-3">
              <div className="text-[10px] font-bold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-muted)' }}>风险原因</div>
              <ul className="space-y-1">
                {reasons.map((r, i) => (
                  <li key={i} className="text-[11px] flex items-start gap-1.5" style={{ color: 'var(--text-secondary)' }}>
                    <span style={{ color: '#ef4444' }}>⚠</span> {r}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* 额外信息 */}
          {message && (
            <div className="text-[11px] p-3 rounded-lg" style={{ background: 'var(--bg-input)', color: 'var(--text-muted)' }}>
              {message}
            </div>
          )}
        </div>

        {/* 操作按钮 */}
        <div className="flex gap-2 px-5 py-4 border-t" style={{ borderColor: 'var(--border)' }}>
          <button onClick={onCancel}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg border text-sm font-medium transition-colors"
            style={{ borderColor: 'var(--border)', color: 'var(--text-secondary)' }}>
            <FiX size={16} /> 取消
          </button>
          <button onClick={onConfirm}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium text-white transition-colors"
            style={{ background: '#ef4444' }}>
            <FiCheck size={16} /> 确认执行
          </button>
        </div>
      </div>
    </div>
  );
}
