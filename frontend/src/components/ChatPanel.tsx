import { useState, useRef, useEffect, useCallback } from 'react';
import { Message } from '../types';
import MessageBubble from './MessageBubble';
import { useSpeechRecognition } from '../hooks/useSpeechRecognition';
import { useTTS } from '../hooks/useTTS';
import { FiSend, FiTrash2, FiMic, FiMicOff, FiVolume2, FiVolumeX } from 'react-icons/fi';

interface ChatPanelProps {
  messages: Message[];
  onSend: (text: string) => void;
  onClear: () => void;
  pendingTask?: string | null;
}

export default function ChatPanel({ messages, onSend, onClear, pendingTask }: ChatPanelProps) {
  const [input, setInput] = useState('');
  const [micActive, setMicActive] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const prevLastMsgRef = useRef('');

  const {
    isSupported: asrSupported, interimText, errorMessage: asrError,
    start: startASR, stop: stopASR,
  } = useSpeechRecognition({
    lang: 'zh-CN',
    onResult: (text, isFinal) => {
      setInput(text);
      if (isFinal && text.trim()) { onSend(text.trim()); setInput(''); setMicActive(false); }
    },
    onError: () => setMicActive(false),
  });

  const { isSupported: ttsSupported, speaking, autoSpeak, toggleAutoSpeak, speak } = useTTS({ lang: 'zh-CN', rate: 1.1 });

  useEffect(() => {
    const lastMsg = messages[messages.length - 1];
    if (lastMsg?.role === 'ai' && !lastMsg.isStreaming && lastMsg.id !== prevLastMsgRef.current) {
      prevLastMsgRef.current = lastMsg.id;
      if (autoSpeak) speak(lastMsg.content);
    }
  }, [messages, autoSpeak, speak]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  const handleSubmit = useCallback(() => {
    if (!input.trim()) return;
    onSend(input.trim()); setInput('');
  }, [input, onSend]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); }
  };

  const handleMicToggle = () => {
    if (micActive) {
      stopASR(); setMicActive(false);
      if (interimText.trim()) { onSend(interimText.trim()); setInput(''); }
    } else { startASR(); setMicActive(true); }
  };

  return (
    <div className="flex flex-col h-full bg-surface-0">
      {/* 消息区 */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-white/20">
            <div className="relative mb-6">
              <div className="w-20 h-20 rounded-full bg-surface-3 flex items-center justify-center">
                <div className={`w-12 h-12 rounded-full bg-accent-amber/20 ${micActive ? 'animate-glow-pulse' : ''}`}>
                  <div className="w-full h-full rounded-full bg-accent-amber/10 flex items-center justify-center">
                    <span className="text-3xl">{micActive ? '◉' : '◆'}</span>
                  </div>
                </div>
              </div>
              {micActive && (
                <div className="absolute -bottom-1 left-1/2 -translate-x-1/2 flex gap-0.5">
                  {[1,2,3,4,5].map(i => (
                    <div key={i} className="w-1 bg-accent-amber/60 rounded-full animate-wave" style={{ height: 8+Math.random()*16+'px', animationDelay: i*0.15+'s' }} />
                  ))}
                </div>
              )}
            </div>
            <p className="text-lg font-display text-white/60">AI 语音助手</p>
            <p className="text-sm mt-2 text-white/20">
              {micActive ? '正在聆听...' : '输入文字或点击麦克风开始对话'}
            </p>
          </div>
        )}
        {messages.map((msg) => <MessageBubble key={msg.id} message={msg} />)}
        {micActive && interimText && (
          <div className="flex justify-end my-2 animate-fade-in">
            <div className="px-4 py-2 bg-surface-3 text-white/40 rounded-2xl rounded-tr-sm text-sm italic max-w-[75%]">
              {interimText}
              <span className="inline-block w-1.5 h-4 ml-0.5 bg-accent-amber animate-pulse rounded-sm align-middle" />
            </div>
          </div>
        )}
        {pendingTask && (
          <div className="flex justify-center my-3 animate-fade-in">
            <div className="flex items-center gap-2 px-4 py-2 bg-teal-500/10 border border-teal-500/20 rounded-full text-[11px] text-teal-400">
              <span className="w-1.5 h-1.5 rounded-full bg-teal-400 animate-pulse" />
              📋 Reasonix 任务待审批 — 说「允许」开始执行
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* 输入栏 */}
      <div className="border-t border-white/5 bg-surface-1/60 backdrop-blur-xl p-4">
        <div className="flex items-end gap-2.5 max-w-3xl mx-auto">
          <button onClick={onClear} className="p-2.5 text-white/20 hover:text-white/50 hover:bg-white/5 rounded-xl transition-colors" title="清空">
            <FiTrash2 size={16} />
          </button>
          {ttsSupported && (
            <button onClick={toggleAutoSpeak} className={`p-2.5 rounded-xl transition-colors ${autoSpeak ? 'text-accent-amber bg-accent-amber/10' : 'text-white/20 hover:text-white/50 hover:bg-white/5'}`} title={autoSpeak ? '播报中' : '静音'}>
              {autoSpeak ? <FiVolume2 size={16} /> : <FiVolumeX size={16} />}
            </button>
          )}
          <div className="flex-1 relative">
            <textarea
              value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={handleKeyDown}
              placeholder={micActive ? '聆听中...' : '说点什么...'}
              rows={1} readOnly={micActive}
              className={`w-full px-4 py-3 rounded-2xl resize-none text-sm bg-surface-2 border transition-all text-white/90 placeholder:text-white/15 focus:outline-none ${micActive ? 'border-accent-amber/30 shadow-[0_0_20px_rgba(240,168,64,0.1)]' : 'border-white/5 focus:border-accent-amber/20'}`}
              style={{ maxHeight: '120px' }}
              onInput={(e) => { const el = e.currentTarget; el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 120) + 'px'; }}
            />
          </div>
          {asrSupported && (
            <button onClick={handleMicToggle} className={`p-3 rounded-2xl transition-all ${micActive ? 'bg-accent-rose/20 text-accent-rose shadow-[0_0_20px_rgba(251,113,133,0.3)]' : 'bg-surface-2 text-white/30 hover:text-white/60 hover:bg-surface-3'}`}>
              {micActive ? <FiMicOff size={18} /> : <FiMic size={18} />}
            </button>
          )}
          <button onClick={handleSubmit} disabled={!input.trim()} className="p-3 bg-accent-amber text-surface-0 rounded-2xl hover:bg-accent-amber/90 disabled:opacity-20 disabled:cursor-not-allowed transition-all font-medium">
            <FiSend size={18} />
          </button>
        </div>
        {micActive && <div className="text-center mt-2 text-[11px] text-accent-amber/60 animate-fade-in">正在录音 — 说话内容实时转写</div>}
        {speaking && <div className="text-center mt-2 text-[11px] text-accent-blue/60 animate-fade-in">AI 正在播报...</div>}
        {asrError && <div className="text-center mt-2 text-[11px] text-accent-rose/60">{asrError}</div>}
      </div>
    </div>
  );
}
