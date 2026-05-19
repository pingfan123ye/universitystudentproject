import { DeviceInfo } from '../types';
import { FiSun, FiCloud, FiDroplet, FiWind, FiPause } from 'react-icons/fi';

interface DevicePanelProps {
  devices: Record<string, DeviceInfo>;
  onScene?: (scene: string) => void;
}

const SCENES = [
  { key: '起床', icon: '🌅', label: '起床', desc: '开灯·开窗帘' },
  { key: '离家', icon: '🚪', label: '离家', desc: '全关·关窗帘' },
  { key: '回家', icon: '🏠', label: '回家', desc: '开灯·开空调' },
  { key: '晚安', icon: '🌙', label: '晚安', desc: '关灯·关窗帘' },
];

const DEFAULT_DEVICES: Record<string, DeviceInfo> = {
  bedroom_light:  { name: '卧室灯', type: 'light',   room: 'bedroom',  status: 'off', properties: { brightness: 100 } },
  living_light:   { name: '客厅灯', type: 'light',   room: 'living',   status: 'off', properties: { brightness: 80 } },
  kitchen_light:  { name: '厨房灯', type: 'light',   room: 'kitchen',  status: 'off', properties: { brightness: 100 } },
  bathroom_light: { name: '卫生间灯', type: 'light', room: 'bathroom', status: 'off', properties: { brightness: 100 } },
  study_light:    { name: '书房灯', type: 'light',   room: 'study',    status: 'off', properties: { brightness: 80 } },
  living_curtain: { name: '客厅窗帘', type: 'curtain', room: 'living', status: 'closed', properties: { position: 0 } },
  water_heater:   { name: '热水器', type: 'heater',  room: 'bathroom', status: 'off', properties: { temperature: 40 } },
  ac:             { name: '空调', type: 'ac',        room: 'living',   status: 'off', properties: { temperature: 26 } },
};

const roomNames: Record<string, string> = { bedroom: '卧室', living: '客厅', kitchen: '厨房', bathroom: '卫浴', study: '书房' };
const typeNames: Record<string, string> = { light: '灯', curtain: '窗帘', heater: '热水器', ac: '空调' };

function deviceIcon(type: string, active: boolean) {
  const cls = active ? 'text-accent-amber' : 'text-white/15';
  switch (type) {
    case 'light': return <FiSun size={16} className={cls} />;
    case 'curtain': return <FiCloud size={16} className={cls} />;
    case 'heater': return <FiDroplet size={16} className={cls} />;
    case 'ac': return <FiWind size={16} className={cls} />;
    default: return <FiPause size={16} className={cls} />;
  }
}

export default function DevicePanel({ devices, onScene }: DevicePanelProps) {
  const merged = Object.keys(devices).length > 0 ? devices : DEFAULT_DEVICES;
  const list = Object.entries(merged);
  const grouped = list.reduce<Record<string, [string, DeviceInfo][]>>((acc, [id, info]) => {
    (acc[info.room] = acc[info.room] || []).push([id, info]); return acc;
  }, {});
  const activeCount = list.filter(([,d]) => d.status === 'on' || d.status === 'open').length;

  return (
    <div className="flex flex-col h-full">
      <div className="px-5 py-3 border-b border-white/5">
        <span className="text-xs font-medium text-white/40 tracking-widest uppercase">家庭设备</span>
      </div>

      {onScene && (
        <div className="px-3 py-3 border-b border-white/5">
          <div className="grid grid-cols-2 gap-2">
            {SCENES.map(s => (
              <button key={s.key} onClick={() => onScene(s.key)}
                className="flex items-center gap-2 px-3 py-2.5 rounded-xl border border-white/5 hover:border-accent-amber/20 hover:bg-accent-amber/5 transition-all text-left">
                <span className="text-base">{s.icon}</span>
                <div><div className="text-[11px] font-medium text-white/70">{s.label}</div><div className="text-[10px] text-white/25">{s.desc}</div></div>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        {Object.entries(grouped).map(([room, items]) => (
          <div key={room}>
            <div className="text-[10px] text-white/20 font-medium mb-2 px-1 uppercase tracking-wider">{roomNames[room] || room}</div>
            <div className="space-y-1.5">
              {items.map(([id, d]) => {
                const active = d.type === 'curtain' ? d.status === 'open' : d.status === 'on';
                return (
                  <div key={id} className={`flex items-center gap-3 px-3 py-2.5 rounded-xl border transition-all duration-500 ${active ? 'bg-accent-amber/5 border-accent-amber/15' : 'bg-transparent border-transparent hover:border-white/5'}`}>
                    <div className={`flex items-center justify-center w-8 h-8 rounded-lg transition-colors duration-500 ${active ? 'bg-accent-amber/10' : 'bg-white/3'}`}>
                      {deviceIcon(d.type, active)}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-[11px] font-medium text-white/70 truncate">{d.name}</div>
                      <div className="text-[10px] text-white/25">{typeNames[d.type]}</div>
                    </div>
                    <div className="text-right">
                      <div className={`text-[10px] font-medium ${active ? 'text-accent-amber' : 'text-white/20'}`}>
                        {d.type === 'curtain' ? (active ? '已开' : '已关') : (active ? '开' : '关')}
                      </div>
                      {d.properties?.temperature != null && <div className="text-[10px] text-white/15">{d.properties.temperature}°</div>}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="px-4 py-2 border-t border-white/5 text-[10px] text-white/20">
        {list.length} 个设备 · {activeCount} 个运行中
      </div>
    </div>
  );
}
