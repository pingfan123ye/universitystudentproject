import { useState, useRef, useEffect, useCallback } from 'react';
import { Message } from '../types';
import MessageBubble from './MessageBubble';
import { useSpeechRecognition } from '../hooks/useSpeechRecognition';
import { useTTS } from '../hooks/useTTS';
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
  musicPlayerVisible?: boolean;
  onResetConversation?: () => void;
}

export default function ChatPanel({ messages, pendingTask, pendingTts, onTtsPlayed, pendingTtsFallback, onTtsFallbackConsumed, onSend, onClear, onSendAudioFinal, onAudioStreamFinal, streamText: propStreamText, onDuckMusic, onRestoreMusic, isMobile, onToggleSidebar, musicPlayerVisible, onResetConversation }: ChatPanelProps) {
  const [input, setInput] = useState('');
  const [micActive, setMicActive] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const prevLastMsgRef = useRef('');

  const { interimText, errorMessage: asrError } = useSpeechRecognition({
    lang: 'zh-CN',
    onResult: (text, isFinal) => { setInput(text); if (isFinal && text.trim()) { onSend(text.trim()); setInput(''); setMicActive(false); } },
    onError: () => setMicActive(false),
  });
  // 统一使用后端 faster-whisper 本地转写

  // 录音可视化状态（纯计时，不使用 AudioContext 避免与音乐播放冲突）
  const [recordingTime, setRecordingTime] = useState(0);
  const [audioLevel, setAudioLevel] = useState(0);
  const animFrameRef = useRef<number>(0);
  const recStreamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 录音：纯 MediaRecorder，零 AudioContext，零音频处理冲突
  // 关键：关闭 echoCancellation/noiseSuppression 防止浏览器在音乐播放时过度抑制麦克风
  const _recRef = useRef<MediaRecorder | null>(null);
  const _recChunksRef = useRef<Blob[]>([]);
  const _incrementalTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const _lastSentChunkIndexRef = useRef<number>(0);
  const _sendChunkDuringRecRef = useRef<((b64: string) => void) | undefined>(undefined);
  const _ampCheckTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 保持 onSendAudioFinal 为最新引用，避免 _stopRec 闭包过期
  const onSendAudioFinalRef = useRef(onSendAudioFinal);
  const _sendFinalRef = useRef<(() => void) | undefined>(undefined);
  useEffect(() => {
    onSendAudioFinalRef.current = onSendAudioFinal;
    _sendChunkDuringRecRef.current = onSendAudioFinal;
  }, [onSendAudioFinal]);
  useEffect(() => { _sendFinalRef.current = onAudioStreamFinal; }, [onAudioStreamFinal]);

  // 连续对话模式
  const [continuousMode, setContinuousMode] = useState(false);
  const continuousModeRef = useRef(false);
  useEffect(() => { continuousModeRef.current = continuousMode; }, [continuousMode]);
  const _silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 开启连续对话时自动开始录音
  useEffect(() => {
    if (continuousMode && !micActive) {
      _startRec().then(ok => { if (ok) setMicActive(true); });
    }
    if (!continuousMode && micActive) {
      // 关闭连续对话时停止录音
      setMicActive(false);
      _stopRec().then(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [continuousMode]);


  // ── WebM Blob → base64 WAV/PCM（抽取为独立函数，供增量发送使用） ──
  const _webmToWavBase64 = useCallback(async (chunks: Blob[]): Promise<string | null> => {
    const blob = new Blob(chunks, { type: 'audio/webm' });
    try {
      const buf = await blob.arrayBuffer();
      const decodeCtx = new AudioContext();
      const audioBuf = await decodeCtx.decodeAudioData(buf);
      const srcData = audioBuf.getChannelData(0);
      decodeCtx.close().catch(() => {});

      const targetSr = 16000;
      const ratio = audioBuf.sampleRate / targetSr;
      const outLen = Math.round(srcData.length / ratio);
      const outData = new Float32Array(outLen);
      for (let i = 0; i < outLen; i++) {
        outData[i] = srcData[Math.min(Math.round(i * ratio), srcData.length - 1)];
      }
      const pcm16 = new Int16Array(outLen);
      for (let i = 0; i < outLen; i++) {
        const s = Math.max(-1, Math.min(1, outData[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }

      const wavHeader = new ArrayBuffer(44);
      const dv = new DataView(wavHeader);
      dv.setUint32(0, 0x52494646, false);
      dv.setUint32(4, 36 + pcm16.byteLength, true);
      dv.setUint32(8, 0x57415645, false);
      dv.setUint32(12, 0x666D7420, false);
      dv.setUint32(16, 16, true);
      dv.setUint16(20, 1, true);
      dv.setUint16(22, 1, true);
      dv.setUint32(24, targetSr, true);
      dv.setUint32(28, targetSr * 2, true);
      dv.setUint16(32, 2, true);
      dv.setUint16(34, 16, true);
      dv.setUint32(36, 0x64617461, false);
      dv.setUint32(40, pcm16.byteLength, true);

      const wavBlob = new Blob([wavHeader, pcm16.buffer], { type: 'audio/wav' });
      const wavBuf = await wavBlob.arrayBuffer();
      const bytes = new Uint8Array(wavBuf);
      let binary = '';
      for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
      return btoa(binary);
    } catch {
      return null;
    }
  }, []);

  const _startRec = useCallback(async (): Promise<boolean> => {
    try {
      // 方案一：禁用所有音频处理，防止浏览器在音乐播放时压低麦克风增益
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      recStreamRef.current = stream;

      // 方案二：录音时降低音乐音量（ducking），录音结束后恢复
      onDuckMusic?.();

      // 计时器
      const startTime = Date.now();
      setRecordingTime(0);
      timerRef.current = setInterval(() => {
        setRecordingTime(Math.floor((Date.now() - startTime) / 1000));
      }, 200);

      // 模拟脉冲动画（不依赖 AudioContext）
      const pulseUpdate = () => {
        setAudioLevel(0.3 + 0.3 * Math.sin(Date.now() / 300));
        animFrameRef.current = requestAnimationFrame(pulseUpdate);
      };
      pulseUpdate();

      // 诊断 + 静音检测：每 1 秒检查一次麦克风输入振幅
      // 使用一个临时的、极短命的 AudioContext 仅用于诊断（不与音乐路径共享）
      const ampCheck = () => {
        if (!recStreamRef.current) return;
        try {
          const tmpCtx = new AudioContext();
          const src = tmpCtx.createMediaStreamSource(recStreamRef.current);
          const analyser = tmpCtx.createAnalyser();
          analyser.fftSize = 256;
          src.connect(analyser);
          const data = new Uint8Array(analyser.frequencyBinCount);
          analyser.getByteFrequencyData(data);
          const avg = data.reduce((a, b) => a + b, 0) / data.length;
          console.log(`[麦克风诊断] 实时频域振幅: ${avg.toFixed(1)}/255 (${(avg/255*100).toFixed(1)}%)`);
          if (avg < 5) {
            console.warn('[麦克风诊断] ⚠️ 实时振幅极低，麦克风可能被占用或静音');
          }

          // 连续对话模式：静音检测 → 自动停止录音
          if (continuousModeRef.current && _recRef.current?.state === 'recording') {
            const SILENCE_THRESHOLD = 12;  // 振幅低于此值视为静音（放宽阈值，避免误触发）
            const elapsed = Date.now() - startTime;
            if (avg < SILENCE_THRESHOLD && elapsed > 1500) {  // 至少录 1.5 秒
              if (!_silenceTimerRef.current) {
                console.log('[连续对话] 检测到静音，2秒后自动停止...');
                _silenceTimerRef.current = setTimeout(() => {
                  console.log('[连续对话] 静音超时，自动停止录音');
                  setMicActive(false);
                  _stopRec().then(b64 => {
                    if (b64 && onSendAudioFinalRef.current) {
                      onSendAudioFinalRef.current(b64);
                      if (_sendFinalRef.current) _sendFinalRef.current();
                    }
                  });
                  return;
                }, 2000);
              }
            } else {
              if (_silenceTimerRef.current) {
                clearTimeout(_silenceTimerRef.current);
                _silenceTimerRef.current = null;
              }
            }
          }
          tmpCtx.close().catch(() => {});
        } catch { /* 诊断失败不影响录音 */ }
      };
      ampCheck(); // 立即检查一次
      _ampCheckTimerRef.current = setInterval(ampCheck, 1000);

      const rec = new MediaRecorder(stream, { mimeType: 'audio/webm' });
      _recChunksRef.current = [];
      rec.ondataavailable = (e) => { if (e.data.size > 0) _recChunksRef.current.push(e.data); };
      rec.start(500); // 每 500ms 切片一次，避免大块数据
      _recRef.current = rec;

      // 新增：每 2 秒将累积的音频片段发送到后端，实现实时转写可视化
      _lastSentChunkIndexRef.current = 0;
      if (_sendChunkDuringRecRef.current !== undefined) {
        if (_incrementalTimerRef.current) clearInterval(_incrementalTimerRef.current);
        _incrementalTimerRef.current = setInterval(async () => {
          const chunks = _recChunksRef.current;
          const from = _lastSentChunkIndexRef.current;
          if (from < chunks.length) {
            const newChunks = chunks.slice(from);
            _lastSentChunkIndexRef.current = chunks.length;
            const b64 = await _webmToWavBase64(newChunks);
            if (b64 && _sendChunkDuringRecRef.current !== undefined) {
              _sendChunkDuringRecRef.current(b64);
            }
          }
        }, 2000);
      }
      return true;
    } catch { return false; }
  }, [onDuckMusic]);
  const _stopRec = useCallback(async (): Promise<string | null> => {
    // 清理定时器
    if (_incrementalTimerRef.current) { clearInterval(_incrementalTimerRef.current); _incrementalTimerRef.current = null; }
    if (_silenceTimerRef.current) { clearTimeout(_silenceTimerRef.current); _silenceTimerRef.current = null; }
    if (_ampCheckTimerRef.current) { clearInterval(_ampCheckTimerRef.current); _ampCheckTimerRef.current = null; }
    if (animFrameRef.current) { cancelAnimationFrame(animFrameRef.current); animFrameRef.current = 0; }
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    if (recStreamRef.current) { recStreamRef.current.getTracks().forEach(t => t.stop()); recStreamRef.current = null; }
    setAudioLevel(0);
    setRecordingTime(0);

    const rec = _recRef.current;
    if (!rec) return null;
    return new Promise((resolve) => {
      rec.onstop = async () => {
        const blob = new Blob(_recChunksRef.current, { type: 'audio/webm' });
        _recChunksRef.current = [];
        _recRef.current = null;
        rec.stream.getTracks().forEach(t => t.stop());
          let decodeCtx: AudioContext | null = null;
        try {
          const buf = await blob.arrayBuffer();
          console.log(`[录音诊断] 录制数据大小: ${buf.byteLength} bytes (${(buf.byteLength/1024).toFixed(1)} KB)`);

                  decodeCtx = new AudioContext();
          const audioBuf = await decodeCtx.decodeAudioData(buf);
          const srcData = audioBuf.getChannelData(0);

          // 诊断振幅
          let maxAmp = 0, sumAmp = 0;
          for (let i = 0; i < srcData.length; i++) {
            const abs = Math.abs(srcData[i]);
            if (abs > maxAmp) maxAmp = abs;
            sumAmp += abs;
          }
          const avgAmp = sumAmp / srcData.length;
          const durationSec = srcData.length / audioBuf.sampleRate;
          console.log(`[录音诊断] 原始: ${audioBuf.sampleRate}Hz ${durationSec.toFixed(1)}s | 最大振幅=${maxAmp.toFixed(4)} 平均振幅=${avgAmp.toFixed(6)}`);
          if (maxAmp < 0.01) {
            console.warn('[录音诊断] ⚠️ 录音振幅极低 (<0.01)，可能是浏览器音频处理抑制了麦克风');
          }

          // 重采样到 16kHz
          const targetSr = 16000;
          const ratio = audioBuf.sampleRate / targetSr;
          const outLen = Math.round(srcData.length / ratio);
          const outData = new Float32Array(outLen);
          for (let i = 0; i < outLen; i++) {
            outData[i] = srcData[Math.min(Math.round(i * ratio), srcData.length - 1)];
          }
          // 转 16-bit PCM
          const pcm16 = new Int16Array(outLen);
          for (let i = 0; i < outLen; i++) {
            const s = Math.max(-1, Math.min(1, outData[i]));
            pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
          }
          // WAV 封装
          const wavHeader = new ArrayBuffer(44);
          const dv = new DataView(wavHeader);
          dv.setUint32(0, 0x52494646, false);
          dv.setUint32(4, 36 + pcm16.byteLength, true);
          dv.setUint32(8, 0x57415645, false);
          dv.setUint32(12, 0x666D7420, false);
          dv.setUint32(16, 16, true);
          dv.setUint16(20, 1, true);
          dv.setUint16(22, 1, true);
          dv.setUint32(24, targetSr, true);
          dv.setUint32(28, targetSr * 2, true);
          dv.setUint16(32, 2, true);
          dv.setUint16(34, 16, true);
          dv.setUint32(36, 0x64617461, false);
          dv.setUint32(40, pcm16.byteLength, true);
          const wavBlob = new Blob([wavHeader, pcm16.buffer], { type: 'audio/wav' });
          const wavBuf = await wavBlob.arrayBuffer();
          const bytes = new Uint8Array(wavBuf);
          let binary = '';
          for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);

          // 关闭临时 AudioContext
          decodeCtx.close().catch(() => {});

          // 录音处理完成，恢复音乐音量
          onRestoreMusic?.();

          resolve(btoa(binary));
        } catch (decodeErr) {
          console.warn('[录音诊断] 音频解码失败（浏览器可能不支持 WebM 解码），跳过转写:', decodeErr);
          decodeCtx?.close?.()?.catch(() => {});
          onRestoreMusic?.();
          resolve(null);  // 返回 null 而非原始 WebM 字节（后端只认 WAV/PCM）
        }
      };
      rec.stop();
    });
  }, [onRestoreMusic]);

  // 后端 STT 转写结果 → 写入输入框（含清空）
  useEffect(() => {
    if (propStreamText !== undefined && propStreamText !== input) {
      setInput(propStreamText);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [propStreamText]);

  const { isSupported: ttsSupported, speaking, autoSpeak, toggleAutoSpeak, speak } = useTTS({ lang: 'zh-CN', rate: 1.1 });

  // 只用 Edge TTS（不降级 speechSynthesis），等后端音频到了就播
  useEffect(() => {
    const lastMsg = messages[messages.length - 1];
    if (lastMsg?.role === 'ai' && !lastMsg.isStreaming && lastMsg.id !== prevLastMsgRef.current) {
      prevLastMsgRef.current = lastMsg.id;
      // 不触发 speechSynthesis，纯等 Edge TTS
    }
  }, [messages]);

  // Edge TTS 音频到达 → 播放，播完后连续对话模式下自动开始录音
  useEffect(() => {
    if (pendingTts && pendingTts.audio) {
      speak(pendingTts.text, pendingTts.audio, () => {
        if (continuousModeRef.current) {
          _startRec().then(ok => { if (ok) setMicActive(true); });
        }
      });
      onTtsPlayed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingTts, speak, onTtsPlayed]);

  // 后端 TTS 失败 → 降级浏览器 speechSynthesis，播完后连续对话模式下自动开始录音
  useEffect(() => {
    if (pendingTtsFallback) {
      speak(pendingTtsFallback, undefined, () => {
        if (continuousModeRef.current) {
          _startRec().then(ok => { if (ok) setMicActive(true); });
        }
      });
      onTtsFallbackConsumed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingTtsFallback]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  const handleSubmit = useCallback(() => {
    if (!input.trim()) return;
    onSend(input.trim()); setInput('');
  }, [input, onSend]);

  return (
    <div className="flex flex-col h-full" style={{ background: 'var(--bg-root)', paddingBottom: musicPlayerVisible ? '60px' : '0px', transition: 'padding-bottom 0.2s' }}>
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
          {onResetConversation && (
            <button onClick={onResetConversation} className="p-2.5 rounded hover:opacity-70 transition-opacity" style={{ color: 'var(--text-muted)' }} title="重置对话（清空上下文）">
              <FiRefreshCw size={16} />
            </button>
          )}
          <button onClick={onClear} className="p-2.5 rounded hover:opacity-70 transition-opacity" style={{ color: 'var(--text-muted)' }} title="清空消息"><FiTrash2 size={16} /></button>
          {ttsSupported && (
            <button onClick={toggleAutoSpeak} className="p-2.5 rounded transition-opacity" style={{ color: autoSpeak ? 'var(--accent)' : 'var(--text-muted)' }} title={autoSpeak ? '播报中' : '静音'}>
              {autoSpeak ? <FiVolume2 size={16} /> : <FiVolumeX size={16} />}
            </button>
          )}
          <button
            onClick={() => setContinuousMode(prev => !prev)}
            className={`p-2.5 rounded transition-all text-[11px] font-bold whitespace-nowrap ${continuousMode ? 'animate-pulse' : ''}`}
            style={{
              color: continuousMode ? '#fff' : 'var(--text-muted)',
              background: continuousMode ? 'var(--accent)' : 'var(--bg-input)',
            }}
            title={continuousMode ? '连续对话中，点击关闭' : '开启连续对话，免点击录音'}
          >
            {continuousMode ? '🔊 连续中' : '🎤 连续'}
          </button>
          <div className="flex-1">
            <textarea value={input} onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); } }}
              placeholder={micActive ? '聆听中...' : '说点什么...'} rows={1} readOnly={micActive}
              className="input-field w-full px-4 py-3 resize-none text-sm placeholder:opacity-30"
              style={{ maxHeight: '120px' }}
              onInput={(e) => { const el = e.currentTarget; el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 120) + 'px'; }} />
          </div>
          <button onClick={async () => {
            if (micActive) {
              setMicActive(false);
              const b64 = await _stopRec();
              if (b64 && onSendAudioFinal) {
                onSendAudioFinal(b64);
                onAudioStreamFinal?.();
              }
            } else {
              _startRec().then(ok => { if (ok) setMicActive(true); });
            }
          }}
            className="p-3 rounded transition-all" style={{ color: micActive ? '#dc2626' : 'var(--text-muted)', background: micActive ? '#dc2626' : 'var(--bg-input)' }}>
            {micActive ? <FiMicOff size={18} /> : <FiMic size={18} />}
          </button>
          <button onClick={handleSubmit} disabled={!input.trim()} className="accent-btn p-3"><FiSend size={18} /></button>
        </div>
        {micActive && (
          <div className="mt-2 animate-fade-in">
            <div className="flex items-center gap-3 justify-center mb-1">
              <span className="text-[11px] font-mono" style={{ color: 'var(--accent)' }}>
                {String(Math.floor(recordingTime / 60)).padStart(2, '0')}:{String(recordingTime % 60).padStart(2, '0')}
              </span>
              <div className="flex-1 max-w-[120px] h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--bg-input)' }}>
                <div className="h-full rounded-full transition-all duration-75" style={{
                  width: `${Math.min(audioLevel * 100, 100)}%`,
                  background: audioLevel > 0.3 ? 'var(--accent)' : 'var(--text-muted)',
                }} />
              </div>
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>录音中</span>
            </div>
            <div className="text-center text-[11px]" style={{ color: 'var(--text-muted)' }}>点击停止按钮结束录音</div>
          </div>
        )}
        {speaking && <div className="text-center mt-2 text-[11px]" style={{ color: 'var(--text-muted)' }}>AI 正在播报...</div>}
        {asrError && <div className="text-center mt-2 text-[11px] text-red-500">{asrError}</div>}
      </div>
    </div>
  );
}
