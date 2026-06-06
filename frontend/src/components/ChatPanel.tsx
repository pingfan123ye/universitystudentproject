import { useState, useRef, useEffect, useCallback } from 'react';
import { Message, Cet6Paper, Cet6SearchResult } from '../types';
import MessageBubble from './MessageBubble';
import WakeWordEnrollment from './WakeWordEnrollment';
import CET6Panel from './CET6Panel';
import { useTTS } from '../hooks/useTTS';
import { useVoiceInteraction } from '../hooks/useVoiceInteraction';
import { FiSend, FiTrash2, FiRefreshCw, FiMic, FiMicOff, FiVolume2, FiVolumeX } from 'react-icons/fi';

interface ChatPanelProps {
  messages: Message[];
  pendingTask?: { task: string; message?: string } | null;
  pendingTts?: { text: string; audio: string } | null;
  onTtsPlayed?: () => void;
  onTtsFinished?: () => void;   // TTS 自然播完 → 播队列下一个
  onTtsClear?: () => void;      // 唤醒词打断 → 清空整个 TTS 队列
  pendingTtsFallback?: string;
  onTtsFallbackConsumed?: () => void;
  onSend: (text: string) => void;
  onClear: () => void;
  onSendCompleteAudio?: (base64: string) => void;
  onCancel?: () => void;  // 唤醒词打断：取消正在进行的 LLM 生成
  streamText?: string;
  onDuckMusic?: () => void;
  onRestoreMusic?: () => void;
  isMobile?: boolean;
  onToggleSidebar?: () => void;
  onResetConversation?: () => void;
  // CET-6
  cet6Paper?: Cet6Paper | null;
  cet6Answers?: Record<string, string> | null;
  onCet6Close?: () => void;
  cet6SearchResults?: Cet6SearchResult[];
  onCet6Download?: (paperId: string) => void;
}

const WAKE_WORD = '小爱同学';

export default function ChatPanel({
  messages, pendingTask, pendingTts, onTtsPlayed, onTtsFinished, onTtsClear,
  pendingTtsFallback, onTtsFallbackConsumed,
  onSend, onClear, onSendCompleteAudio, onCancel, streamText: propStreamText,
  onDuckMusic, onRestoreMusic, isMobile, onToggleSidebar, onResetConversation,
  cet6Paper, cet6Answers, onCet6Close,
  cet6SearchResults, onCet6Download,
}: ChatPanelProps) {
  const [input, setInput] = useState('');
  const [micActive, setMicActive] = useState(false);
  const [showEnrollment, setShowEnrollment] = useState(false);
  const [showCet6Card, setShowCet6Card] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const prevLastMsgRef = useRef('');

  // ═══════════════════════════════════════
  // TTS（必须在语音交互之前初始化，供打断回调使用）
  // ═══════════════════════════════════════
  const { isSupported: ttsSupported, speaking, autoSpeak, toggleAutoSpeak, speak, stop: stopTts } = useTTS({ lang: 'zh-CN', rate: 1.1 });

  // ═══════════════════════════════════════
  // 语音交互状态机（唤醒词 + 录音）
  // ═══════════════════════════════════════
  const [voiceState, voiceActions] = useVoiceInteraction({
    wakeWord: WAKE_WORD,
    onWakeDetected: () => {
      console.log('[语音助手] 唤醒词检测成功');
      // 立即闪避音乐音量，减少扬声器残响干扰后续录音
      onDuckMusic?.();
      // 打断 TTS：停止当前播报 + 清空整个待播队列
      stopTts();
      onTtsClear?.();
      onTtsFallbackConsumed?.();
      // 取消正在进行的 LLM 生成（后端流式输出 + 前端流式消息）
      onCancel?.();
    },
    // B-3: 录音结束后发送完整音频
    onAudioComplete: (b64: string) => {
      onSendCompleteAudio?.(b64);
    },
    onAudioFinal: () => {
      // 唤醒词监听恢复已移至 useVoiceInteraction 内部自动处理
      // 录音结束 → onstop → 自动重启 Mellon，不等 TTS 播完
    },
    onError: (err: string) => {
      console.error('[语音助手] 错误:', err);
    },
    onDuckMusic,
    onRestoreMusic,
  });

  // ── 唤醒词注册完成 → 自动启用语音助手 ──
  const handleEnrollmentComplete = useCallback(() => {
    setShowEnrollment(false);
    voiceActions.enable();
  }, [voiceActions]);

  const handleEnrollmentCancel = useCallback(() => {
    setShowEnrollment(false);
  }, []);

  // ── 语音助手按钮 ──
  const handleVoiceAssistantClick = useCallback(async () => {
    const phase = voiceState.phase;
    if (phase === 'idle') {
      // 未注册 → 显示注册弹窗；已注册 → 直接启用
      if (!voiceState.isEnrolled) {
        setShowEnrollment(true);
        return;
      }
      await voiceActions.enable();
    } else if (phase === 'waiting_for_wake' || phase === 'wake_detected' || phase === 'recording') {
      // 关闭语音助手
      voiceActions.disable();
    } else if (phase === 'initializing') {
      // 加载中，取消
      voiceActions.disable();
    }
  }, [voiceState.phase, voiceState.isEnrolled, voiceActions]);

  // ── 手动麦克风按钮（备用：不依赖唤醒词） ──
  const handleMicClick = useCallback(async () => {
    if (micActive) {
      // 停止手动录音 → 发送完整音频
      setMicActive(false);
      const b64 = await voiceActions.stopManualRecord();
      if (b64) {
        onSendCompleteAudio?.(b64);
      }
    } else {
      // 开始手动录音
      const ok = await voiceActions.startManualRecord();
      if (ok) setMicActive(true);
    }
  }, [micActive, voiceActions, onSendCompleteAudio]);

  // ── 语音状态同步 micActive ──
  useEffect(() => {
    if (voiceState.phase === 'recording' || voiceState.phase === 'wake_detected') {
      setMicActive(true);
    } else if (voiceState.phase === 'processing' || voiceState.phase === 'waiting_for_wake' || voiceState.phase === 'idle') {
      setMicActive(false);
    }
  }, [voiceState.phase]);

  // 后端 STT 转写结果 → 写入输入框
  useEffect(() => {
    if (propStreamText !== undefined && propStreamText !== input) {
      setInput(propStreamText);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [propStreamText]);

  // Edge TTS 音频到达 → 播放，播完后恢复唤醒词监听 + 推进队列
  useEffect(() => {
    if (pendingTts && pendingTts.audio) {
      speak(pendingTts.text, pendingTts.audio, () => {
        // TTS 播完 → 恢复唤醒词监听 + 播队列下一个
        voiceActions.resumeWakeListening();
        onTtsFinished?.();
      });
      onTtsPlayed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingTts, speak, onTtsPlayed, onTtsFinished]);

  // 后端 TTS 失败 → 降级浏览器 speechSynthesis，播完后恢复 + 推进队列
  useEffect(() => {
    if (pendingTtsFallback) {
      speak(pendingTtsFallback, undefined, () => {
        voiceActions.resumeWakeListening();
        onTtsFinished?.();
      });
      onTtsFallbackConsumed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingTtsFallback, onTtsFinished]);

  useEffect(() => {
    const lastMsg = messages[messages.length - 1];
    if (lastMsg?.role === 'ai' && !lastMsg.isStreaming && lastMsg.id !== prevLastMsgRef.current) {
      prevLastMsgRef.current = lastMsg.id;
    }
  }, [messages]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  // 新试卷到达 → 收起卡片，显示圆点
  useEffect(() => {
    if (cet6Paper) {
      setShowCet6Card(false);
    }
  }, [cet6Paper]);

  const handleSubmit = useCallback(() => {
    if (!input.trim()) return;
    onSend(input.trim()); setInput('');
  }, [input, onSend]);

  // ── 语音助手按钮的标签和样式 ──
  const voiceBtnLabel = (() => {
    switch (voiceState.phase) {
      case 'initializing': return '⏳ 加载中';
      case 'waiting_for_wake': return '🔊 待命中';
      case 'wake_detected': return '🟢 我在听';
      case 'recording': return '🔴 录音中';
      case 'processing': return '⏳ 思考中';
      default: return '🤖 语音助手';
    }
  })();

  const voiceBtnStyle: React.CSSProperties = {
    padding: '4px 10px',
    borderRadius: '6px',
    border: 'none',
    cursor: 'pointer',
    fontSize: '11px',
    fontWeight: 700,
    whiteSpace: 'nowrap',
    transition: 'all 0.2s',
    color: voiceState.phase === 'idle' ? 'var(--text-muted)' : '#fff',
    background:
      voiceState.phase === 'initializing' ? '#f59e0b' :
      voiceState.phase === 'waiting_for_wake' ? '#22c55e' :
      voiceState.phase === 'wake_detected' ? '#22c55e' :
      voiceState.phase === 'recording' ? '#dc2626' :
      voiceState.phase === 'processing' ? '#f59e0b' :
      'var(--bg-input)',
    animation: voiceState.phase === 'waiting_for_wake' ? 'pulse 2s ease-in-out infinite' :
                voiceState.phase === 'recording' ? 'pulse 0.8s ease-in-out infinite' :
                voiceState.phase === 'initializing' ? 'pulse 1s ease-in-out infinite' : 'none',
  };

  return (
    <div className="flex flex-col flex-1 min-h-0" style={{ background: 'var(--bg-root)' }}>
      {/* ── 唤醒词注册弹窗 ── */}
      {showEnrollment && (
        <WakeWordEnrollment
          wakeWord={WAKE_WORD}
          onComplete={handleEnrollmentComplete}
          onCancel={handleEnrollmentCancel}
        />
      )}

      {/* ── 移动端顶栏 ── */}
      {isMobile && (
        <div className="flex items-center justify-between px-4 py-2 border-b" style={{ borderColor: 'var(--border)', background: 'var(--bg-surface)' }}>
          <span className="text-xs font-bold tracking-wider" style={{ color: 'var(--text-secondary)' }}>AI 语音助手</span>
          <button onClick={onToggleSidebar} className="flex items-center gap-1 text-[11px] px-3 py-1.5 rounded border"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
            ☰ 设备
          </button>
        </div>
      )}

      {/* ── 消息列表 ── */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full" style={{ color: 'var(--text-muted)' }}>
            <div className="mb-5 w-16 h-16 rounded-full flex items-center justify-center" style={{ background: 'var(--accent-glow)' }}>
              <span className="text-2xl" style={{ color: 'var(--accent)' }}>◆</span>
            </div>
            <p className="text-lg font-bold" style={{ color: 'var(--text-secondary)' }}>AI 语音助手</p>
            <p className="text-sm mt-2">点击「语音助手」或麦克风开始对话</p>
            {!voiceState.isEnrolled && voiceState.phase === 'idle' && (
              <button
                onClick={() => setShowEnrollment(true)}
                style={{
                  marginTop: '16px', padding: '10px 24px', borderRadius: '12px',
                  border: 'none', background: 'var(--accent)', color: '#fff',
                  fontSize: '14px', fontWeight: 600, cursor: 'pointer',
                }}
              >
                🎤 设置唤醒词 "{WAKE_WORD}"
              </button>
            )}
          </div>
        )}
        {messages.map((msg) => <MessageBubble key={msg.id} message={msg} />)}

        {/* 录音中显示实时转写文字 */}
        {micActive && propStreamText && (
          <div className="flex justify-end my-2">
            <div className="px-4 py-2 text-sm italic max-w-[75%] rounded-2xl" style={{ background: 'var(--bg-input)', color: 'var(--text-muted)' }}>
              {propStreamText}<span className="inline-block w-1.5 h-4 ml-0.5 rounded-sm align-middle animate-pulse" style={{ background: 'var(--accent)' }} />
            </div>
          </div>
        )}

        {/* 唤醒状态提示 */}
        {voiceState.phase === 'waiting_for_wake' && (
          <div className="flex justify-center my-2">
            <span className="text-[11px] px-3 py-1 rounded-full" style={{
              background: 'rgba(34,197,94,0.1)', color: '#22c55e', border: '1px solid rgba(34,197,94,0.2)',
            }}>
              🔊 说 "{WAKE_WORD}" 唤醒我
            </span>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* ── CET-6 试卷指示器标签（仅发送试卷时显示，实时更新标题）─── */}
      {cet6Paper && !showCet6Card && (
        <div className="flex justify-center mb-2">
          <button
            onClick={() => setShowCet6Card(true)}
            className="cet6-dot-btn"
            title="点击展开试卷卡片"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '6px',
              padding: '5px 14px',
              borderRadius: '20px',
              border: '1px solid var(--accent-strong, rgba(167,139,250,0.25))',
              cursor: 'pointer',
              background: 'var(--bg-elevated)',
              animation: 'cet6-dot-pulse 2s ease-in-out infinite',
              fontSize: '12px',
              fontWeight: 600,
              color: 'var(--accent)',
              whiteSpace: 'nowrap',
              maxWidth: '90%',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            <span style={{ flexShrink: 0 }}>📖</span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {cet6Paper.title}
            </span>
          </button>
        </div>
      )}

      {/* ── CET-6 备考面板（弹出式卡片，默认收起）─── */}
      {showCet6Card && (cet6Paper || (cet6SearchResults && cet6SearchResults.length > 0)) && (
        <div className="mx-4 mb-2">
          <CET6Panel
            paper={cet6Paper || null}
            answers={cet6Answers || null}
            onClose={() => {
              // 关闭卡片 → 回到圆点（不清除试卷）
              setShowCet6Card(false);
            }}
            searchResults={cet6SearchResults}
            onDownloadPaper={onCet6Download}
          />
        </div>
      )}

      {/* ── 待审批任务提示 ── */}
      {pendingTask && (
        <div className="mx-5 mb-2 px-4 py-3 rounded border text-sm animate-slide-up" style={{ background: 'var(--accent-glow)', borderColor: 'var(--accent)', color: 'var(--text-primary)' }}>
          <span className="font-bold" style={{ color: 'var(--accent)' }}>⏳ 待审批：</span>
          {pendingTask.message || pendingTask.task.slice(0, 60)}
          <span className="ml-2 text-xs" style={{ color: 'var(--text-muted)' }}>回复「允许」开始执行</span>
        </div>
      )}

      {/* ── 底部输入栏 ── */}
      <div className="p-4 border-t" style={{ background: 'var(--bg-surface)', borderColor: 'var(--border)' }}>
        <div className="flex items-end gap-2 max-w-3xl mx-auto">
          {/* 重置对话 */}
          {onResetConversation && (
            <button onClick={onResetConversation} className="p-2.5 rounded hover:opacity-70 transition-opacity" style={{ color: 'var(--text-muted)' }} title="重置对话（清空上下文）">
              <FiRefreshCw size={16} />
            </button>
          )}
          {/* 清空消息 */}
          <button onClick={onClear} className="p-2.5 rounded hover:opacity-70 transition-opacity" style={{ color: 'var(--text-muted)' }} title="清空消息"><FiTrash2 size={16} /></button>
          {/* 播报开关 */}
          {ttsSupported && (
            <button onClick={toggleAutoSpeak} className="p-2.5 rounded transition-opacity" style={{ color: autoSpeak ? 'var(--accent)' : 'var(--text-muted)' }} title={autoSpeak ? '播报中' : '静音'}>
              {autoSpeak ? <FiVolume2 size={16} /> : <FiVolumeX size={16} />}
            </button>
          )}
          {/* 语音助手开关（替代旧的连续对话按钮） */}
          <button
            onClick={handleVoiceAssistantClick}
            style={voiceBtnStyle}
            title={
              voiceState.phase === 'idle' ? '启用语音助手（唤醒词免提交互）' :
              voiceState.phase === 'waiting_for_wake' ? '点击关闭语音助手' :
              voiceState.phase === 'recording' ? '点击关闭语音助手' :
              '语音助手中'
            }
          >
            {voiceBtnLabel}
          </button>
          {/* 输入框 */}
          <div className="flex-1">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); } }}
              placeholder={
                voiceState.phase === 'waiting_for_wake' ? `说 "${WAKE_WORD}" 唤醒我...` :
                micActive ? '聆听中...' : '说点什么...'
              }
              rows={1}
              readOnly={micActive}
              className="input-field w-full px-4 py-3 resize-none text-sm placeholder:opacity-30"
              style={{ maxHeight: '120px' }}
              onInput={(e) => { const el = e.currentTarget; el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 120) + 'px'; }} />
          </div>
          {/* 手动麦克风按钮（备用） */}
          <button
            onClick={handleMicClick}
            className="p-3 rounded transition-all"
            style={{
              color: micActive ? '#dc2626' : 'var(--text-muted)',
              background: micActive ? '#dc2626' : 'var(--bg-input)',
            }}
            title={micActive ? '停止录音' : '手动录音（不需要唤醒词）'}
          >
            {micActive ? <FiMicOff size={18} /> : <FiMic size={18} />}
          </button>
          {/* 发送按钮 */}
          <button onClick={handleSubmit} disabled={!input.trim()} className="accent-btn p-3"><FiSend size={18} /></button>
        </div>

        {/* 录音状态栏 */}
        {micActive && (
          <div className="mt-2 animate-fade-in">
            <div className="flex items-center gap-3 justify-center mb-1">
              <span className="text-[11px] font-mono" style={{ color: 'var(--accent)' }}>
                {String(Math.floor(voiceState.recordingTime / 60)).padStart(2, '0')}:{String(voiceState.recordingTime % 60).padStart(2, '0')}
              </span>
              <div className="flex-1 max-w-[120px] h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--bg-input)' }}>
                <div className="h-full rounded-full transition-all duration-75" style={{
                  width: `${Math.min(voiceState.audioLevel * 100, 100)}%`,
                  background: voiceState.audioLevel > 0.2 ? 'var(--accent)' : 'var(--text-muted)',
                }} />
              </div>
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                {voiceState.phase === 'wake_detected' ? '唤醒中...' : '录音中'}
              </span>
            </div>
            <div className="text-center text-[11px]" style={{ color: 'var(--text-muted)' }}>
              {voiceState.phase === 'wake_detected' ? '即将开始录音...' :
               '说话后静音 2 秒自动停止'}
            </div>
          </div>
        )}

        {/* TTS 播报中 */}
        {speaking && <div className="text-center mt-2 text-[11px]" style={{ color: 'var(--text-muted)' }}>AI 正在播报...</div>}
        {/* 语音错误 */}
        {voiceState.error && <div className="text-center mt-2 text-[11px]" style={{ color: '#ef4444' }}>{voiceState.error}</div>}
      </div>
    </div>
  );
}
