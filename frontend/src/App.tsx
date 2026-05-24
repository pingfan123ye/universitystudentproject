import { useCallback, useEffect, useState } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import { useMusicPlayer } from './hooks/useMusicPlayer';
import StatusBar from './components/StatusBar';
import ChatPanel from './components/ChatPanel';
import DevicePanel from './components/DevicePanel';
import CachePanel from './components/CachePanel';
import RouteLog from './components/RouteLog';
import MemoryPanel from './components/MemoryPanel';
import SafetyDialog from './components/SafetyDialog';
import SettingsPanel from './components/SettingsPanel';

export default function App() {
  const {
    status, messages, devices, cacheEntries, lastPath, routeLog, musicAction,
    memoryEntries, pendingTask, ttsAudio,
    safetyConfirm, safetyReply,
    engineConfig, fetchEngineConfig, setEngineConfigItem, resetEngineConfig,
    timeState, setTime, setTimeSpeed, toggleTimePause, toggleTimeSim, toggleSuppressAlerts,
    sendMessage, clearMessages, fetchCacheList, deleteCache,
    fetchMemoryList, deleteMemory, clearMemories,
  } = useWebSocket();

  const { playerState, trackName, doAction } = useMusicPlayer();

  useEffect(() => { if (musicAction) doAction(musicAction); }, [musicAction, doAction]);

  // Edge TTS 音频播放（ttsAudio 变化时触发）
  const [pendingTts, setPendingTts] = useState<{ text: string; audio: string } | null>(null);
  useEffect(() => {
    if (ttsAudio?.audio) {
      setPendingTts({ text: ttsAudio.text, audio: ttsAudio.audio });
    }
  }, [ttsAudio]);

  // 本地实时时钟（非模拟时间时使用）
  const [localTime, setLocalTime] = useState('');
  useEffect(() => {
    const update = () => {
      const now = new Date();
      setLocalTime(`${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`);
    };
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, []);

  // 响应式：检测窄屏
  const [isMobile, setIsMobile] = useState(window.innerWidth < 768);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  useEffect(() => {
    const onResize = () => {
      const mobile = window.innerWidth < 768;
      setIsMobile(mobile);
      if (!mobile) setSidebarOpen(false);
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const handleScene = useCallback((scene: string) => {
    const map: Record<string, string> = { '起床': '起床模式', '离家': '离家模式', '回家': '回家模式', '晚安': '晚安' };
    sendMessage(map[scene] || scene);
  }, [sendMessage]);

  return (
    <div className="flex flex-col h-screen" style={{ background: 'var(--bg-root)', color: 'var(--text-primary)' }}>
      <StatusBar status={status} lastPath={lastPath}
  alertsEnabled={true} onToggleAlerts={toggleSuppressAlerts} />

      {/* 桌面：主区域 + 侧栏并排；手机：侧栏作为覆盖层 */}
      <div className="flex flex-1 overflow-hidden relative">
        {/* 主聊区域 */}
        <div className={`flex-1 min-w-0 transition-all duration-300 ${isMobile && sidebarOpen ? 'hidden' : 'flex flex-col'}`}
          style={!isMobile ? { borderRight: '1px solid var(--border)' } : {}}>
          <ChatPanel messages={messages} pendingTask={pendingTask ? { task: pendingTask } : null} onSend={sendMessage} onClear={clearMessages}
            pendingTts={pendingTts} onTtsPlayed={() => setPendingTts(null)}
            isMobile={isMobile} onToggleSidebar={() => setSidebarOpen(true)} />
        </div>

        {/* 侧栏：桌面固定显示，手机为抽屉覆盖层 */}
        {(!isMobile || sidebarOpen) && (
          <>
            {/* 手机遮罩 */}
            {isMobile && (
              <div className="fixed inset-0 z-30" style={{ background: 'rgba(0,0,0,0.5)' }}
                onClick={() => setSidebarOpen(false)} />
            )}
            <div className={`${isMobile ? 'fixed right-0 top-0 bottom-0 z-40 w-[85vw] max-w-sm shadow-2xl animate-slide-left' : 'w-[40%] max-w-md'} flex flex-col`}
              style={{ background: 'var(--bg-surface)' }}>
              <div className="flex-1 overflow-y-auto">
                {/* 手机端关闭按钮 */}
                {isMobile && (
                  <div className="flex items-center justify-between px-4 py-2 border-b" style={{ borderColor: 'var(--border)' }}>
                    <span className="text-[11px] font-bold uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>设备</span>
                    <button onClick={() => setSidebarOpen(false)} className="text-sm px-2 py-1 rounded" style={{ color: 'var(--text-muted)' }}>✕</button>
                  </div>
                )}
                <DevicePanel devices={devices} onScene={handleScene}
                  timeState={timeState} localTime={localTime}
                  onSetTime={setTime} onSetSpeed={setTimeSpeed}
                  onTogglePause={toggleTimePause} onToggleSim={toggleTimeSim} />
              </div>
              <RouteLog entries={routeLog} />
              <MemoryPanel entries={memoryEntries} onRefresh={fetchMemoryList} onDelete={deleteMemory} onClearAll={clearMemories} />
              <SettingsPanel config={engineConfig} onFetchConfig={fetchEngineConfig} onSetConfig={setEngineConfigItem} onReset={resetEngineConfig} />
              <CachePanel entries={cacheEntries} onRefresh={fetchCacheList} onDelete={deleteCache} />
            </div>
          </>
        )}
      </div>

      {/* 安全确认弹窗 */}
      {safetyConfirm && (
        <SafetyDialog
          command={safetyConfirm.command}
          risk={safetyConfirm.risk}
          reasons={safetyConfirm.reasons}
          message={safetyConfirm.message}
          onConfirm={() => safetyReply(true)}
          onCancel={() => safetyReply(false)}
        />
      )}

      {playerState !== 'idle' && (
        <div className="fixed bottom-5 left-5 px-4 py-3 rounded-2xl border flex items-center gap-3 z-50 animate-slide-up shadow-lg"
          style={{ background: 'var(--bg-elevated)', borderColor: 'var(--border)' }}>
          <div className="w-9 h-9 rounded flex items-center justify-center text-sm" style={{ background: playerState === 'playing' ? 'var(--accent-glow)' : 'var(--bg-input)', color: 'var(--accent)' }}>
            {playerState === 'playing' ? '▶' : '⏸'}
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>正在播放</div>
            <div className="text-xs font-medium">{trackName}</div>
          </div>
          <button onClick={() => doAction(playerState === 'playing' ? 'pause' : 'play')} className="ml-2 text-[11px] px-3 py-1.5 rounded border transition-colors"
            style={{ borderColor: 'var(--border)', color: 'var(--text-secondary)' }}>
            {playerState === 'playing' ? '暂停' : '播放'}
          </button>
        </div>
      )}
    </div>
  );
}
