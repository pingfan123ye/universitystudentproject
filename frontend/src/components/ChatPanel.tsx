import { useState, useRef, useEffect, useCallback } from 'react';
import { Message } from '../types';
import MessageBubble from './MessageBubble';
import { useSpeechRecognition } from '../hooks/useSpeechRecognition';
import { useTTS } from '../hooks/useTTS';
import { FiSend, FiTrash2, FiMic, FiMicOff, FiVolume2, FiVolumeX } from 'react-icons/fi';

interface ChatPanelProps {
  messages: Message[];
  pendingTask?: { task: string; message?: string } | null;
  pendingTts?: { text: string; audio: string } | null;
  onTtsPlayed?: () => void;
  onSend: (text: string) => void;
  onClear: () => void;
  isMobile?: boolean;
  onToggleSidebar?: () => void;
}

export default function ChatPanel({ messages, pendingTask, pendingTts, onTtsPlayed, onSend, onClear, isMobile, onToggleSidebar }: ChatPanelProps) {
  const [input, setInput] = useState('');
  const [micActive, setMicActive] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const prevLastMsgRef = useRef('');

  const { isSupported: asrSupported, interimText, errorMessage: asrError, start: startASR, stop: stopASR } = useSpeechRecognition({
    lang: 'zh-CN',
    onResult: (text, isFinal) => { setInput(text); if (isFinal && text.trim()) { onSend(text.trim()); setInput(''); setMicActive(false); } },
    onError: () => setMicActive(false),
  });

  const { isSupported: ttsSupported, speaking, autoSpeak, toggleAutoSpeak, speak } = useTTS({ lang: 'zh-CN', rate: 1.1 });

  // 只用 Edge TTS（不降级 speechSynthesis），等后端音频到了就播
  useEffect(() => {
    const lastMsg = messages[messages.length - 1];
    if (lastMsg?.role === 'ai' && !lastMsg.isStreaming && lastMsg.id !== prevLastMsgRef.current) {
      prevLastMsgRef.current = lastMsg.id;
      // 不触发 speechSynthesis，纯等 Edge TTS
    }
  }, [messages]);

  // Edge TTS 音频到达 → 播放
  useEffect(() => {
    if (pendingTts && pendingTts.audio) {
      speak(pendingTts.text, pendingTts.audio);
      onTtsPlayed?.();
    }
  }, [pendingTts, speak, onTtsPlayed]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  const handleSubmit = useCallback(() => {
    if (!input.trim()) return;
    onSend(input.trim()); setInput('');
  }, [input, onSend]);

  return (
    <div className="flex flex-col h-full" style={{ background: 'var(--bg-root)' }}>
      {isMobile && (
        <div className="flex items-center justify-between px-4 py-2 border-b" style={{ borderColor: 'var(--border)', background: 'var(--bg-surface)' }}>
          <span className="text-xs font-bold tracking-wider" style={{ color: 'var(--text-secondary)' }}>AI 语音助手</span>
          <button onClick={onToggleSidebar} className="flex items-center gap-1 text-[11px] px-3 py-1.5 rounded border"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
            ☰ 设备
          </button>
        </div>
      )}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full" style={{ color: 'var(--text-muted)' }}>
            <div className="mb-5 w-16 h-16 rounded-full flex items-center justify-center" style={{ background: 'var(--accent-glow)' }}>
              <span className="text-2xl" style={{ color: 'var(--accent)' }}>◆</span>
            </div>
            <p className="text-lg font-bold" style={{ color: 'var(--text-secondary)' }}>AI 语音助手</p>
            <p className="text-sm mt-2">输入文字或点击麦克风开始对话</p>
          </div>
        )}
        {messages.map((msg) => <MessageBubble key={msg.id} message={msg} />)}
        {micActive && interimText && (
          <div className="flex justify-end my-2">
            <div className="px-4 py-2 text-sm italic max-w-[75%] rounded-2xl" style={{ background: 'var(--bg-input)', color: 'var(--text-muted)' }}>
              {interimText}<span className="inline-block w-1.5 h-4 ml-0.5 rounded-sm align-middle animate-pulse" style={{ background: 'var(--accent)' }} />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {pendingTask && (
        <div className="mx-5 mb-2 px-4 py-3 rounded border text-sm animate-slide-up" style={{ background: 'var(--accent-glow)', borderColor: 'var(--accent)', color: 'var(--text-primary)' }}>
          <span className="font-bold" style={{ color: 'var(--accent)' }}>⏳ 待审批：</span>
          {pendingTask.message || pendingTask.task.slice(0, 60)}
          <span className="ml-2 text-xs" style={{ color: 'var(--text-muted)' }}>回复「允许」开始执行</span>
        </div>
      )}

      <div className="p-4 border-t" style={{ background: 'var(--bg-surface)', borderColor: 'var(--border)' }}>
        <div className="flex items-end gap-2 max-w-3xl mx-auto">
          <button onClick={onClear} className="p-2.5 rounded hover:opacity-70 transition-opacity" style={{ color: 'var(--text-muted)' }} title="清空"><FiTrash2 size={16} /></button>
          {ttsSupported && (
            <button onClick={toggleAutoSpeak} className="p-2.5 rounded transition-opacity" style={{ color: autoSpeak ? 'var(--accent)' : 'var(--text-muted)' }} title={autoSpeak ? '播报中' : '静音'}>
              {autoSpeak ? <FiVolume2 size={16} /> : <FiVolumeX size={16} />}
            </button>
          )}
          <div className="flex-1">
            <textarea value={input} onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); } }}
              placeholder={micActive ? '聆听中...' : '说点什么...'} rows={1} readOnly={micActive}
              className="input-field w-full px-4 py-3 resize-none text-sm placeholder:opacity-30"
              style={{ maxHeight: '120px' }}
              onInput={(e) => { const el = e.currentTarget; el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 120) + 'px'; }} />
          </div>
          {asrSupported && (
            <button onClick={() => { micActive ? (stopASR(), setMicActive(false)) : (startASR(), setMicActive(true)); }}
              className="p-3 rounded transition-all" style={{ color: micActive ? '#fff' : 'var(--text-muted)', background: micActive ? 'var(--accent)' : 'var(--bg-input)' }}>
              {micActive ? <FiMicOff size={18} /> : <FiMic size={18} />}
            </button>
          )}
          <button onClick={handleSubmit} disabled={!input.trim()} className="accent-btn p-3"><FiSend size={18} /></button>
        </div>
        {micActive && <div className="text-center mt-2 text-[11px] animate-fade-in" style={{ color: 'var(--accent)' }}>正在录音 — 说话内容实时转写</div>}
        {speaking && <div className="text-center mt-2 text-[11px]" style={{ color: 'var(--text-muted)' }}>AI 正在播报...</div>}
        {asrError && <div className="text-center mt-2 text-[11px] text-red-500">{asrError}</div>}
      </div>
    </div>
  );
}
