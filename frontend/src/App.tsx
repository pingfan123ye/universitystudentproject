import { useCallback, useEffect, useRef, useState } from 'react';
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
import MusicPlayer from './components/MusicPlayer';

export default function App() {
  const {
    status, messages, devices, cacheEntries, lastPath, routeLog, musicAction,
    memoryEntries, pendingTask, ttsAudio,
    safetyConfirm, safetyReply,
    engineConfig, fetchEngineConfig, setEngineConfigItem, resetEngineConfig,
    timeState, setTime, setTimeSpeed, toggleTimePause, toggleTimeSim, toggleSuppressAlerts,
    sendMessage, clearMessages, fetchCacheList, deleteCache,
    fetchMemoryList, deleteMemory, clearMemories,
    sendCompleteAudio, sendVerifyWake, sendCancel, sendCet6Close, transcriptionText, ttsFallbackText,
    musicSearchStatus,
    cet6Paper, setCet6Paper, cet6Answers, setCet6Answers,
    cet6SearchResults, sendCet6Download,
    resetConversation,
  } = useWebSocket();

  const {
    playerState, setPlayerState, currentSong, currentPlaylist, queue, currentIndex, volume, progress, error, searchResults, setSearchResults,
    play, pause, duckForRecording, restoreVolumeAfterRecording, next, prev, seek, setVolume, setQueueAndPlay,
    handleMusicControl,
  } = useMusicPlayer();

  useEffect(() => { if (musicAction) handleMusicControl(musicAction); }, [musicAction, handleMusicControl]);

  // 音乐搜索状态 → 播放器状态同步
  useEffect(() => {
    if (musicSearchStatus.status === 'searching') {
      setPlayerState('searching');
    } else if (musicSearchStatus.status === 'copyright_blocked' || musicSearchStatus.status === 'not_found') {
      setPlayerState('idle');
    }
  }, [musicSearchStatus, setPlayerState]);

  // ═══════════════════════════════════════
  // TTS 队列：防止音乐/CET-6 播报打断 LLM 回复
  // ═══════════════════════════════════════
  const ttsQueueRef = useRef<{ text: string; audio: string; seq: number }[]>([]);
  const ttsPlayingRef = useRef(false);
  const [pendingTts, setPendingTts] = useState<{ text: string; audio: string } | null>(null);

  const playNextTts = useCallback(() => {
    if (ttsQueueRef.current.length > 0) {
      const next = ttsQueueRef.current.shift()!;
      ttsPlayingRef.current = true;
      setPendingTts(next);
    } else {
      ttsPlayingRef.current = false;
    }
  }, []);

  // 新 TTS 音频到达 → 按 seq 顺序入队，空闲则立即播放
  useEffect(() => {
    if (ttsAudio?.audio) {
      const item = { text: ttsAudio.text, audio: ttsAudio.audio, seq: ttsAudio.seq ?? 0 };
      // 按 seq 顺序插入（保持队列始终有序）
      const queue = ttsQueueRef.current;
      let insertAt = queue.length;
      for (let i = 0; i < queue.length; i++) {
        if (item.seq < queue[i].seq) {
          insertAt = i;
          break;
        }
      }
      queue.splice(insertAt, 0, item);
      if (!ttsPlayingRef.current) {
        playNextTts();
      }
    }
  }, [ttsAudio, playNextTts]);

  // 当前 TTS 播放完毕 → 出队播下一个
  const handleTtsFinished = useCallback(() => {
    setPendingTts(null);
    playNextTts();
  }, [playNextTts]);

  // 唤醒词打断 → 清空整个队列
  const handleTtsClear = useCallback(() => {
    ttsQueueRef.current = [];
    ttsPlayingRef.current = false;
    setPendingTts(null);
  }, []);

  // TTS 降级文本（后端 TTS 失败时触发浏览器 speechSynthesis）
  const [pendingTtsFallback, setPendingTtsFallback] = useState('');
  useEffect(() => {
    if (ttsFallbackText) setPendingTtsFallback(ttsFallbackText);
  }, [ttsFallbackText]);

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
            onTtsFinished={handleTtsFinished} onTtsClear={handleTtsClear}
            pendingTtsFallback={pendingTtsFallback} onTtsFallbackConsumed={() => setPendingTtsFallback('')}
            onSendCompleteAudio={(b64: string) => sendCompleteAudio(b64)} onVerifyWake={sendVerifyWake} onCancel={() => sendCancel()}
            streamText={transcriptionText}
            onDuckMusic={duckForRecording} onRestoreMusic={restoreVolumeAfterRecording}
            isMusicPlaying={playerState === 'playing'}
            isMobile={isMobile} onToggleSidebar={() => setSidebarOpen(true)}
            onResetConversation={resetConversation}
            cet6Paper={cet6Paper} cet6Answers={cet6Answers}
            onCet6Close={() => { setCet6Paper(null); setCet6Answers(null); sendCet6Close(); }}
            cet6SearchResults={cet6SearchResults}
            onCet6Download={sendCet6Download} />
          {/* 音乐导航栏：仅占左栏宽度，不遮挡侧栏 */}
          <MusicPlayer
            playerState={playerState}
            currentSong={currentSong}
            currentPlaylist={currentPlaylist}
            queue={queue}
            currentIndex={currentIndex}
            volume={volume}
            progress={progress}
            error={error}
            searchResults={searchResults}
            onPlay={play}
            onPause={pause}
            onNext={next}
            onPrev={prev}
            onSeek={seek}
            onSetVolume={setVolume}
            onSetQueueAndPlay={setQueueAndPlay}
            onSearchResults={setSearchResults}
          />
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

    </div>
  );
}
