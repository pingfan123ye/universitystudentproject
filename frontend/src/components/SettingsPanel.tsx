import { useState, useEffect } from 'react';
import { FiSettings, FiRotateCcw } from 'react-icons/fi';

interface SettingsPanelProps {
  config: Record<string, unknown>;
  onFetchConfig: () => void;
  onSetConfig: (key: string, value: unknown) => void;
  onReset: () => void;
}

const MODES = [
  { key: 'local_first', label: '本地优先', desc: '默认本地，实时任务切云端' },
  { key: 'cloud_first', label: '云端优先', desc: '默认云端，隐私任务切本地' },
  { key: 'local_only', label: '仅本地', desc: '强制只用本地模型' },
  { key: 'cloud_only', label: '仅云端', desc: '强制只用云端模型' },
];

export default function SettingsPanel({ config, onFetchConfig, onSetConfig, onReset }: SettingsPanelProps) {
  const [open, setOpen] = useState(false);
  const s = { borderColor: 'var(--border)', color: 'var(--text-muted)', background: 'var(--bg-elevated)' };

  useEffect(() => {
    if (open && (!config || Object.keys(config).length === 0)) {
      onFetchConfig();
    }
  }, [open, config, onFetchConfig]);

  return (
    <div className="border-t" style={{ borderColor: 'var(--border)' }}>
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-5 py-2.5 text-xs font-medium transition-colors" style={s}>
        <FiSettings size={14} /> 引擎设置
      </button>
      {open && (
        <div className="max-h-64 overflow-y-auto p-4 space-y-3">
          {/* 调度模式 */}
          <div>
            <div className="text-[10px] font-bold mb-2 uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>调度模式</div>
            <div className="space-y-1.5">
              {MODES.map(m => (
                <button key={m.key} onClick={() => onSetConfig('default_mode', m.key)}
                  className="w-full text-left px-3 py-2 rounded border text-[11px] transition-colors"
                  style={{
                    borderColor: config.default_mode === m.key ? 'var(--accent)' : 'var(--border)',
                    background: config.default_mode === m.key ? 'var(--accent-glow)' : 'var(--bg-elevated)',
                    color: 'var(--text-primary)',
                  }}>
                  <div className="font-medium">{m.label}</div>
                  <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>{m.desc}</div>
                </button>
              ))}
            </div>
          </div>

          {/* 搜索开关 */}
          <div className="flex items-center justify-between">
            <div>
              <div className="text-[11px] font-medium" style={{ color: 'var(--text-primary)' }}>联网搜索</div>
              <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>允许模型搜索实时信息</div>
            </div>
            <label className="relative inline-flex items-center cursor-pointer">
              <input type="checkbox" checked={!!config.enable_search} onChange={e => onSetConfig('enable_search', e.target.checked)}
                className="sr-only peer" />
              <div className="w-9 h-5 rounded-full peer-checked:bg-purple-500 bg-gray-400 transition-colors" />
            </label>
          </div>

          {/* 本地超时 */}
          <div className="flex items-center justify-between">
            <span className="text-[11px]" style={{ color: 'var(--text-primary)' }}>本地超时 (秒)</span>
            <select value={String(config.timeout_seconds || 8)} onChange={e => onSetConfig('timeout_seconds', parseInt(e.target.value))}
              className="text-[11px] px-2 py-1 rounded border" style={{ borderColor: 'var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)' }}>
              {[3, 5, 8, 10, 15, 20, 30].map(v => (
                <option key={v} value={v}>{v}s</option>
              ))}
            </select>
          </div>

          {/* 云端模型 */}
          <div className="flex items-center justify-between">
            <span className="text-[11px]" style={{ color: 'var(--text-primary)' }}>云端模型</span>
            <select value={String(config.cloud_model || 'deepseek-v4-flash')} onChange={e => onSetConfig('cloud_model', e.target.value)}
              className="text-[11px] px-2 py-1 rounded border" style={{ borderColor: 'var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)' }}>
              <option value="deepseek-v4-flash">deepseek-v4-flash</option>
              <option value="deepseek-v4-pro">deepseek-v4-pro</option>
            </select>
          </div>

          {/* 重置按钮 */}
          <button onClick={onReset}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded border text-[11px] transition-colors"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
            <FiRotateCcw size={12} /> 恢复默认
          </button>
        </div>
      )}
    </div>
  );
}
