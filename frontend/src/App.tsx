import { useCallback, useEffect } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import { useMusicPlayer } from './hooks/useMusicPlayer';
import StatusBar from './components/StatusBar';
import ChatPanel from './components/ChatPanel';
import DevicePanel from './components/DevicePanel';
import CachePanel from './components/CachePanel';
import RouteLog from './components/RouteLog';
import MemoryPanel from './components/MemoryPanel';

export default function App() {
  const {
    status, messages, devices, cacheEntries, lastPath, routeLog, musicAction, memoryEntries, pendingTask,
    sendMessage, clearMessages, fetchCacheList, deleteCache,
    fetchMemoryList, deleteMemory, clearMemories,
  } = useWebSocket();

  const { playerState, trackName, doAction } = useMusicPlayer();

  useEffect(() => {
    if (musicAction) doAction(musicAction);
  }, [musicAction, doAction]);

  const handleScene = useCallback((scene: string) => {
    const map: Record<string, string> = { '起床': '起床模式', '离家': '离家模式', '回家': '回家模式', '晚安': '晚安' };
    sendMessage(map[scene] || scene);
  }, [sendMessage]);

  return (
    <div className="flex flex-col h-screen bg-surface-0 text-white">
      <StatusBar status={status} lastPath={lastPath} />

      <div className="flex flex-1 overflow-hidden">
        {/* Left: Chat */}
        <div className="flex-1 min-w-0 border-r border-white/5">
          <ChatPanel messages={messages} onSend={sendMessage} onClear={clearMessages} pendingTask={pendingTask} />
        </div>

        {/* Right: Panels */}
        <div className="w-[40%] max-w-md flex flex-col bg-surface-1/50">
          <div className="flex-1 overflow-y-auto">
            <DevicePanel devices={devices} onScene={handleScene} />
          </div>
          <RouteLog entries={routeLog} />
          <MemoryPanel entries={memoryEntries} onRefresh={fetchMemoryList} onDelete={deleteMemory} onClearAll={clearMemories} />
          <CachePanel entries={cacheEntries} onRefresh={fetchCacheList} onDelete={deleteCache} />
        </div>
      </div>

      {/* Music Player Overlay */}
      {playerState !== 'idle' && (
        <div className="fixed bottom-5 left-5 glass-elevated rounded-2xl px-4 py-3 flex items-center gap-3 z-50 animate-slide-up">
          <div className={`w-9 h-9 rounded-xl flex items-center justify-center text-sm ${playerState === 'playing' ? 'bg-accent-amber/15 text-accent-amber' : 'bg-white/5 text-white/30'}`}>
            {playerState === 'playing' ? '▶' : '⏸'}
          </div>
          <div>
            <div className="text-[10px] text-white/40 uppercase tracking-wider">正在播放</div>
            <div className="text-xs font-medium text-white/80">{trackName}</div>
          </div>
          <button onClick={() => doAction(playerState === 'playing' ? 'pause' : 'play')}
            className="ml-2 text-[11px] px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-white/60 transition-colors">
            {playerState === 'playing' ? '暂停' : '播放'}
          </button>
        </div>
      )}

      {/* 背景紫光 */}
      <div className="fixed top-0 right-0 w-[500px] h-[500px] bg-purple-600/8 rounded-full blur-[150px] pointer-events-none" />
      <div className="fixed bottom-0 left-0 w-[400px] h-[400px] bg-indigo-600/6 rounded-full blur-[120px] pointer-events-none" />
    </div>
  );
}
