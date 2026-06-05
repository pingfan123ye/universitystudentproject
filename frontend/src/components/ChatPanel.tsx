import { useState, useRef, useEffect, useCallback } from 'react';
import { Message } from '../types';
import MessageBubble from './MessageBubble';
import WakeWordEnrollment from './WakeWordEnrollment';
import { useTTS } from '../hooks/useTTS';
import { useVoiceInteraction } from '../hooks/useVoiceInteraction';
import { FiSend, FiTrash2, FiRefreshCw, FiMic, FiMicOff, FiVolume2, FiVolumeX } from 'react-icons/fi';

interface ChatPanelProps {
  messages: Message[];
  pendingTask?: { task: string; message?: string } | null;
  pendingTts?: { text: string; audio: string } | null;
  onTtsPlayed?: () => void;
  pendingTtsFallback?: string;
  onTtsFallbackConsumed?: () => void;
  onSend: (text: string) => void;
  onClear: () => void;
  onSendAudioFinal?: (base64: string) => void;
  onAudioStreamFinal?: () => void;
  streamText?: string;
  onDuckMusic?: () => void;
  onRestoreMusic?: () => void;
  isMobile?: boolean;
  onToggleSidebar?: () => void;
  onResetConversation?: () => void;
}

const WAKE_WORD = '小爱同学';

export default function ChatPanel({
  messages, pendingTask, pendingTts, onTtsPlayed, pendingTtsFallback, onTtsFallbackConsumed,
  onSend, onClear, onSendAudioFinal, onAudioStreamFinal, streamText: propStreamText,
  onDuckMusic, onRestoreMusic, isMobile, onToggleSidebar, onResetConversation,
}: ChatPanelProps) {
  const [input, setInput] = useState('');
  const [micActive, setMicActive] = useState(false);
  const [showEnrollment, setShowEnrollment] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const prevLastMsgRef = useRef('');

  // ═══════════════════════════════════════
  // 语音交互状态机（唤醒词 + 录音）
  // ═══════════════════════════════════════
  const [voiceState, voiceActions] = useVoiceInteraction({
    wakeWord: WAKE_WORD,
    onWakeDetected: () => {
      console.log('[语音助手] 唤醒词检测成功');
    },
    onAudioChunk: (b64: string) => {
      onSendAudioFinal?.(b64);
    },
    onAudioComplete: (b64: string) => {
      onSendAudioFinal?.(b64);
    },
    onAudioFinal: () => {
      onAudioStreamFinal?.();
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
      // 停止手动录音
      setMicActive(false);
      const b64 = await voiceActions.stopManualRecord();
      if (b64 && onSendAudioFinal) {
        onSendAudioFinal(b64);
        onAudioStreamFinal?.();
      }
    } else {
      // 开始手动录音
      const ok = await voiceActions.startManualRecord();
      if (ok) setMicActive(true);
    }
  }, [micActive, voiceActions, onSendAudioFinal, onAudioStreamFinal]);

  // ── 语音状态同步 micActive ──
  useEffect(() => {
    if (voiceState.phase === 'recording' || voiceState.phase === 'wake_detected') {
      setMicActive(true);
    } else if (voiceState.phase === 'processing' || voiceState.phase === 'waiting_for_wake' || voiceState.phase === 'idle') {
      setMicActive(false);
    }
  }, [voiceState.phase]);

  // ═══════════════════════════════════════
  // TTS
  // ═══════════════════════════════════════
  const { isSupported: ttsSupported, speaking, autoSpeak, toggleAutoSpeak, speak } = useTTS({ lang: 'zh-CN', rate: 1.1 });

  // 后端 STT 转写结果 → 写入输入框
  useEffect(() => {
    if (propStreamText !== undefined && propStreamText !== input) {
      setInput(propStreamText);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [propStreamText]);

  // Edge TTS 音频到达 → 播放，播完后恢复唤醒词监听
  useEffect(() => {
    if (pendingTts && pendingTts.audio) {
      speak(pendingTts.text, pendingTts.audio, () => {
        // TTS 播完 → 恢复唤醒词监听
        voiceActions.resumeWakeListening();
      });
      onTtsPlayed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingTts, speak, onTtsPlayed]);

  // 后端 TTS 失败 → 降级浏览器 speechSynthesis，播完后恢复
  useEffect(() => {
    if (pendingTtsFallback) {
      speak(pendingTtsFallback, undefined, () => {
        voiceActions.resumeWakeListening();
      });
      onTtsFallbackConsumed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingTtsFallback]);

  useEffect(() => {
    const lastMsg = messages[messages.length - 1];
    if (lastMsg?.role === 'ai' && !lastMsg.isStreaming && lastMsg.id !== prevLastMsgRef.current) {
      prevLastMsgRef.current = lastMsg.id;
    }
  }, [messages]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

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
