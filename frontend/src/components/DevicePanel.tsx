import { DeviceInfo, TimeState } from '../types';
import { FiSun, FiCloud, FiDroplet, FiWind, FiClock, FiPlay, FiPause } from 'react-icons/fi';

interface DevicePanelProps {
  devices: Record<string, DeviceInfo>;
  onScene?: (scene: string) => void;
  timeState?: TimeState;
  localTime?: string;
  onSetTime?: (hour: number, minute: number) => void;
  onSetSpeed?: (speed: number) => void;
  onTogglePause?: () => void;
  onToggleSim?: () => void;
}

const SCENES = [
  { key: '起床', icon: '🌅', label: '起床', desc: '开灯·开窗帘' },
  { key: '离家', icon: '🚪', label: '离家', desc: '全关·关窗帘' },
  { key: '回家', icon: '🏠', label: '回家', desc: '开灯·开空调' },
  { key: '晚安', icon: '🌙', label: '晚安', desc: '关灯·关窗帘' },
];

const DEFAULT_DEVICES: Record<string, DeviceInfo> = {
  bedroom_light: { name: '卧室灯', type: 'light', room: 'bedroom', status: 'off', properties: { brightness: 100 } },
  living_light: { name: '客厅灯', type: 'light', room: 'living', status: 'off', properties: { brightness: 80 } },
  kitchen_light: { name: '厨房灯', type: 'light', room: 'kitchen', status: 'off', properties: { brightness: 100 } },
  bathroom_light: { name: '卫生间灯', type: 'light', room: 'bathroom', status: 'off', properties: { brightness: 100 } },
  study_light: { name: '书房灯', type: 'light', room: 'study', status: 'off', properties: { brightness: 80 } },
  living_curtain: { name: '客厅窗帘', type: 'curtain', room: 'living', status: 'closed', properties: { position: 0 } },
  water_heater: { name: '热水器', type: 'heater', room: 'bathroom', status: 'off', properties: { temperature: 40 } },
  ac: { name: '空调', type: 'ac', room: 'living', status: 'off', properties: { temperature: 26 } },
  fan: { name: '风扇', type: 'fan', room: 'living', status: 'off', properties: { speed: 1 } },
  tv: { name: '电视', type: 'tv', room: 'living', status: 'off', properties: { volume: 20 } },
};

const roomNames: Record<string, string> = { bedroom: '卧室', living: '客厅', kitchen: '厨房', bathroom: '卫浴', study: '书房' };
const typeNames: Record<string, string> = { light: '灯', curtain: '窗帘', heater: '热水器', ac: '空调' };

function icon(type: string, active: boolean) {
  const c = active ? 'var(--accent)' : 'var(--text-muted)';
  const T = type === 'light' ? FiSun : type === 'curtain' ? FiCloud : type === 'heater' ? FiDroplet : FiWind;
  return <T size={16} style={{ color: c }} />;
}

export default function DevicePanel({ devices, onScene, timeState, localTime, onSetTime, onSetSpeed, onTogglePause, onToggleSim }: DevicePanelProps) {
  const merged = Object.keys(devices).length > 0 ? devices : DEFAULT_DEVICES;
  const list = Object.entries(merged);
  const grouped = list.reduce<Record<string, [string, DeviceInfo][]>>((a, [id, info]) => { (a[info.room] = a[info.room] || []).push([id, info]); return a; }, {});
  const activeCount = list.filter(([,d]) => d.status === 'on' || d.status === 'open').length;

  return (
    <div className="flex flex-col h-full">
      <div className="px-5 py-3 border-b" style={{ borderColor: 'var(--border)' }}>
        <span className="text-xs font-bold tracking-widest uppercase" style={{ color: 'var(--text-muted)' }}>设备</span>
      </div>
      {onScene && (
        <div className="px-3 py-3 border-b" style={{ borderColor: 'var(--border)' }}>
          <div className="grid grid-cols-2 gap-2">
            {SCENES.map(s => (
              <button key={s.key} onClick={() => onScene(s.key)} className="flex items-center gap-2 px-3 py-2.5 rounded border transition-all hover:shadow-sm"
                style={{ borderColor: 'var(--border)', background: 'var(--bg-elevated)' }}>
                <span className="text-base">{s.icon}</span>
                <div><div className="text-[11px] font-medium" style={{ color: 'var(--text-primary)' }}>{s.label}</div><div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>{s.desc}</div></div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ═══ 时间模拟控制 ═══ */}
      {timeState && onSetTime && (
        <div className="px-3 py-3 border-b" style={{ borderColor: 'var(--border)' }}>
          <div className="flex items-center gap-2 mb-2">
            <FiClock size={12} style={{ color: 'var(--accent)' }} />
            <span className="text-[10px] font-bold tracking-wider uppercase" style={{ color: 'var(--text-muted)' }}>时间</span>
            <span className="ml-auto text-[20px] font-mono font-bold" style={{ color: timeState.simulated ? 'var(--accent)' : 'var(--text-secondary)' }}>
              {timeState.simulated ? (timeState.current_time || '--:--') : (localTime || timeState.current_time || '--:--')}
            </span>
            <button onClick={onToggleSim}
              className="text-[10px] px-2 py-0.5 rounded border"
              style={{ borderColor: 'var(--border)', color: timeState.simulated ? 'var(--accent)' : 'var(--text-muted)' }}>
              {timeState.simulated ? '模拟' : '实时'}
            </button>
          </div>
          {timeState.simulated && (
            <>
              {/* 小时滑块 */}
              <div className="flex items-center gap-2 mb-1">
                <input type="range" min="0" max="23" value={timeState.current_time?.split(':')[0] || '8'}
                  onChange={(e) => onSetTime?.(parseInt(e.target.value), parseInt(timeState.current_time?.split(':')[1] || '0'))}
                  className="flex-1 h-1 rounded accent-purple-400"
                  style={{ background: 'var(--bg-input)' }} />
                <span className="text-[10px] w-6 text-right font-mono" style={{ color: 'var(--text-muted)' }}>
                  {timeState.current_time?.split(':')[0] || '08'}时
                </span>
              </div>
              {/* 加速比 */}
              <div className="flex items-center gap-2">
                <span className="text-[10px]" style={{ color: 'var(--text-muted)' }}>倍速</span>
                <input type="range" min="0" max="60" value={Math.round(Math.log2(timeState.speed || 1) + 5)}
                  onChange={(e) => onSetSpeed?.(Math.pow(2, parseInt(e.target.value) - 5))}
                  className="flex-1 h-1 rounded accent-purple-400"
                  style={{ background: 'var(--bg-input)' }} />
                <span className="text-[10px] w-12 text-right font-mono" style={{ color: 'var(--accent)' }}>
                  {timeState.speed.toFixed(1)}x
                </span>
                <button onClick={() => onTogglePause?.()}
                  className="p-1 rounded border flex items-center justify-center"
                  style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
                  {timeState.paused ? <FiPlay size={12} /> : <FiPause size={12} />}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        {Object.entries(grouped).map(([room, items]) => (
          <div key={room}>
            <div className="text-[10px] font-bold mb-2 px-1 uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>{roomNames[room] || room}</div>
            <div className="space-y-1">
              {items.map(([id, d]) => {
                const active = d.type === 'curtain' ? d.status === 'open' : d.status === 'on';
                return (
                  <div key={id} className="flex items-center gap-3 px-3 py-2.5 rounded border transition-all duration-500"
                    style={{
                      background: active ? 'var(--accent-glow)' : 'var(--bg-elevated)',
                      borderColor: active ? 'var(--accent)' : 'var(--border)',
                    }}>
                    <div className="flex items-center justify-center w-8 h-8 rounded" style={{ background: active ? 'var(--accent-strong)' : 'var(--bg-input)' }}>
                      {icon(d.type, active)}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-[11px] font-medium truncate" style={{ color: 'var(--text-primary)' }}>{d.name}</div>
                      <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>{typeNames[d.type]}</div>
                    </div>
                    <div className="text-right">
                      <div className="text-[10px] font-bold" style={{ color: active ? 'var(--accent)' : 'var(--text-muted)' }}>
                        {d.type === 'curtain' ? (active ? '已开' : '已关') : (active ? '开' : '关')}
                      </div>
                      {d.properties?.temperature != null && <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>{String(d.properties.temperature)}°</div>}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      <div className="px-4 py-2 border-t text-[10px]" style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
        {list.length} 设备 · {activeCount} 运行
      </div>
    </div>
  );
}
