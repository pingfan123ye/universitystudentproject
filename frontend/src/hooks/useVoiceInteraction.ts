import { useState, useRef, useCallback, useEffect } from 'react';
import type { VoicePhase } from '../types';

// ── 类型 ──
export interface VoiceInteractionOptions {
  wakeWord: string;
  onWakeDetected: () => void;
  onAudioChunk: (base64: string) => void;       // 增量音频（流式 STT）
  onAudioComplete: (base64: string) => void;    // 最终完整音频
  onAudioFinal: () => void;                     // 录音结束标记
  onError: (error: string) => void;
  onDuckMusic?: () => void;
  onRestoreMusic?: () => void;
}

export interface VoiceInteractionState {
  phase: VoicePhase;
  recordingTime: number;
  audioLevel: number;
  error: string;
  isEnrolled: boolean;
}

export interface VoiceInteractionActions {
  enable: () => Promise<void>;
  disable: () => void;
  resumeWakeListening: () => Promise<void>;
  startManualRecord: () => Promise<void>;
  stopManualRecord: () => Promise<string | null>;
}

// ── 常量 ──
const STORAGE_KEY = 'mellon-xiaoai-refs';
const MAX_RECORD_SECONDS = 30;        // 最长录音秒数
const MIN_RECORDING_BEFORE_AUTO_STOP_MS = 4000;  // 前4秒不触发自动停止
const WAKE_CONFIDENCE_THRESHOLD = 0.7;   // 唤醒词最低置信度（防止误唤醒）
const WAKE_COOLDOWN_MS = 2000;           // 两次唤醒最小间隔（防止连触发）

// AnalyserNode 自适应静音检测参数
const LEVEL_CHECK_INTERVAL_MS = 250;    // 电平检测间隔
const NOISE_WINDOW_SAMPLES = 12;        // 噪声基线窗口 = 12 × 250ms = 3 秒
const SPEECH_RATIO = 3.0;               // 语音阈值 = 噪声基线 × 3
const SPEECH_THRESHOLD_MIN = 0.03;      // 语音阈值最低值（安静环境下）
const MIN_SPEECH_DURATION_MS = 500;     // 最少连续语音时长（避免短噪声触发）
const SILENCE_TIMEOUT_MS = 2500;        // 连续静音超时 → 自动停止（容忍自然停顿）

// ── WebM → WAV/PCM 16kHz base64 ──
// reuseCtx: 可选，复用已有的 AudioContext 进行解码，避免创建过多实例
async function webmToWavBase64(chunks: Blob[], reuseCtx?: AudioContext | null): Promise<string | null> {
  const blob = new Blob(chunks, { type: 'audio/webm' });
  try {
    const buf = await blob.arrayBuffer();
    const ownCtx = !reuseCtx;
    const decodeCtx = reuseCtx || new AudioContext();
    const audioBuf = await decodeCtx.decodeAudioData(buf);
    const srcData = audioBuf.getChannelData(0);
    if (ownCtx) decodeCtx.close().catch(() => { });

    const targetSr = 16000;
    const ratio = audioBuf.sampleRate / targetSr;
    const outLen = Math.round(srcData.length / ratio);
    const outData = new Float32Array(outLen);
    // 线性插值重采样（替代最近邻，减少混叠失真）
    for (let i = 0; i < outLen; i++) {
      const srcIdx = i * ratio;
      const srcIdxFloor = Math.floor(srcIdx);
      const srcIdxCeil = Math.min(srcIdxFloor + 1, srcData.length - 1);
      const frac = srcIdx - srcIdxFloor;
      outData[i] = srcData[srcIdxFloor] * (1 - frac) + srcData[srcIdxCeil] * frac;
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
}

export function useVoiceInteraction(opts: VoiceInteractionOptions): [VoiceInteractionState, VoiceInteractionActions] {
  const { wakeWord, onWakeDetected, onAudioChunk, onAudioComplete, onAudioFinal, onError, onDuckMusic, onRestoreMusic } = opts;

  const [phase, setPhase] = useState<VoicePhase>('idle');
  const [recordingTime, setRecordingTime] = useState(0);
  const [audioLevel, setAudioLevel] = useState(0);
  const [error, setError] = useState('');
  const [isEnrolled, setIsEnrolled] = useState(false);

  // ── Refs ──
  const detectorRef = useRef<any>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const recChunksRef = useRef<Blob[]>([]);
  const recStreamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const levelTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const enabledRef = useRef(false);
  const phaseRef = useRef<VoicePhase>('idle');
  // 标记 auto-stop 是否已触发（防止手动停止时重复处理）
  const autoStoppedRef = useRef(false);
  // 标记录音是否被用户主动取消（阻止 onstop 发送音频 + 切 processing 状态）
  const cancelledRef = useRef(false);
  // 唤醒词防连触发：记录上次唤醒时间
  const lastWakeTimeRef = useRef(0);

  // 同步 state → ref
  useEffect(() => { phaseRef.current = phase; }, [phase]);

  // ── 清理所有资源 ──
  const cleanup = useCallback(() => {
    // 标记取消：阻止异步 onstop 发送音频 + 切到 processing
    cancelledRef.current = true;
    if (detectorRef.current) {
      detectorRef.current.stop().catch(() => { });
      detectorRef.current = null;
    }
    if (levelTimerRef.current) { clearInterval(levelTimerRef.current); levelTimerRef.current = null; }
    if (audioCtxRef.current) { audioCtxRef.current.close().catch(() => { }); audioCtxRef.current = null; }
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    if (recStreamRef.current) { recStreamRef.current.getTracks().forEach(t => t.stop()); recStreamRef.current = null; }
    if (recRef.current && recRef.current.state !== 'inactive') {
      recRef.current.stop();
    }
    recRef.current = null;
    recChunksRef.current = [];
    autoStoppedRef.current = false;
    setRecordingTime(0);
    setAudioLevel(0);
  }, []);

  // ── 页面隐藏/切换标签时自动暂停 ──
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden && detectorRef.current?.listening) {
        detectorRef.current.stop().catch(() => { });
      } else if (!document.hidden && enabledRef.current && phaseRef.current === 'waiting_for_wake') {
        detectorRef.current?.start().catch(() => { });
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  // ── 组件卸载清理 ──
  useEffect(() => () => { cleanup(); enabledRef.current = false; }, [cleanup]);

  // ═══════════════════════════════════════
  // 录音逻辑（AnalyserNode 自适应噪声基线静音检测）
  // ═══════════════════════════════════════

  const startRecording = useCallback(async (): Promise<boolean> => {
    try {
      // 新一轮录音开始，重置取消标志
      cancelledRef.current = false;

      // 先确保 Mellon 已停止（释放其麦克风流）
      if (detectorRef.current?.listening) {
        await detectorRef.current.stop();
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: true,
          autoGainControl: false,
        },
      });
      recStreamRef.current = stream;
      autoStoppedRef.current = false;
      onDuckMusic?.();

      // ── 电平检测 AudioContext（录音期间复用 1 个）──
      const audioCtx = new AudioContext();
      audioCtxRef.current = audioCtx;
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 256;
      analyser.smoothingTimeConstant = 0.3;
      source.connect(analyser);
      // 不 connect 到 destination — 避免回声
      const timeData = new Uint8Array(analyser.fftSize);

      // ── 自适应噪声基线 + 静音检测状态机 ──
      const noiseWindow: number[] = [];       // 滑动窗口 RMS 历史
      let speechDuration = 0;                 // 连续语音累计
      let silenceDuration = 0;                // 连续静音累计
      let hasSpeech = false;                  // 是否已确认检测到语音

      levelTimerRef.current = setInterval(() => {
        analyser.getByteTimeDomainData(timeData);
        // 计算 RMS
        let sum = 0;
        for (let i = 0; i < timeData.length; i++) {
          const normalized = (timeData[i] - 128) / 128;
          sum += normalized * normalized;
        }
        const rms = Math.sqrt(sum / timeData.length);

        // 更新噪声基线窗口
        noiseWindow.push(rms);
        if (noiseWindow.length > NOISE_WINDOW_SAMPLES) noiseWindow.shift();

        // 自适应语音阈值：max(噪声基线 × 3, 最低 0.03)
        const noiseFloor = noiseWindow.length > 0
          ? noiseWindow.reduce((a, b) => Math.min(a, b), Infinity)
          : 0.01;
        const speechThreshold = Math.max(noiseFloor * SPEECH_RATIO, SPEECH_THRESHOLD_MIN);

        // 更新 UI 电平条（rms 映射到 0-1）
        setAudioLevel(Math.min(rms / 0.2, 1));

        // 状态机：语音检测 + 静音计时
        if (rms > speechThreshold) {
          speechDuration += LEVEL_CHECK_INTERVAL_MS;
          silenceDuration = 0;
          if (speechDuration >= MIN_SPEECH_DURATION_MS) {
            hasSpeech = true;
          }
        } else {
          speechDuration = 0;
          if (hasSpeech) {
            silenceDuration += LEVEL_CHECK_INTERVAL_MS;
          }
        }

        // 确认语音后，连续静音超 2.5 秒 + 录音超过 4 秒最小保护时间 → 自动停止
        if (hasSpeech && silenceDuration >= SILENCE_TIMEOUT_MS) {
          const recordingElapsed = Date.now() - startTime;
          if (recordingElapsed < MIN_RECORDING_BEFORE_AUTO_STOP_MS) {
            // 仍在最小录音保护时间内，不触发自动停止
            return;
          }
          const r = recRef.current;
          if (r && r.state === 'recording') {
            console.log(`[静音检测] noiseFloor=${noiseFloor.toFixed(4)} thr=${speechThreshold.toFixed(4)} rms=${rms.toFixed(4)} silence=${silenceDuration}ms → 自动停止`);
            autoStoppedRef.current = true;
            r.stop();  // → 触发下方的 rec.onstop
          }
        }
      }, LEVEL_CHECK_INTERVAL_MS);

      // ── 录音计时器 ──
      const startTime = Date.now();
      setRecordingTime(0);
      setPhase('recording');
      timerRef.current = setInterval(() => {
        const elapsed = Math.floor((Date.now() - startTime) / 1000);
        setRecordingTime(elapsed);
        if (elapsed >= MAX_RECORD_SECONDS) {
          const r = recRef.current;
          if (r && r.state === 'recording') {
            autoStoppedRef.current = true;
            r.stop();
          }
        }
      }, 200);

      // ── MediaRecorder ──
      let mimeType = 'audio/webm';
      const supportedTypes = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
      for (const t of supportedTypes) {
        if (MediaRecorder.isTypeSupported(t)) { mimeType = t; break; }
      }
      const rec = new MediaRecorder(stream, {
        mimeType,
        audioBitsPerSecond: 128000,
      });
      recChunksRef.current = [];
      recRef.current = rec;

      // ondataavailable：仅收集数据块
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) {
          recChunksRef.current.push(e.data);
        }
      };

      // ═══════════════════════════════════════
      // ★★★ onstop 统一出口 ★★★
      // 静音自动停止 / 超时 / 手动停止 / 用户取消 四条路径均触发此处理
      // ═══════════════════════════════════════
      rec.onstop = async () => {
        // 用户主动取消（disable/cleanup）→ 跳过音频发送，避免误切 processing
        if (cancelledRef.current) {
          console.log('[录音] 已被用户取消，跳过音频处理');
          if (levelTimerRef.current) { clearInterval(levelTimerRef.current); levelTimerRef.current = null; }
          if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
          if (recStreamRef.current) { recStreamRef.current.getTracks().forEach(t => t.stop()); recStreamRef.current = null; }
          if (audioCtxRef.current) { audioCtxRef.current.close().catch(() => { }); audioCtxRef.current = null; }
          setAudioLevel(0);
          setRecordingTime(0);
          onRestoreMusic?.();
          recChunksRef.current = [];
          recRef.current = null;
          return;
        }

        console.log('[录音] onstop 触发，收集音频...');

        // 1. 停止所有定时器
        if (levelTimerRef.current) { clearInterval(levelTimerRef.current); levelTimerRef.current = null; }
        if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }

        // 2. 停止麦克风流
        if (recStreamRef.current) { recStreamRef.current.getTracks().forEach(t => t.stop()); recStreamRef.current = null; }

        // 3. 关闭电平检测 AudioContext
        if (audioCtxRef.current) { audioCtxRef.current.close().catch(() => { }); audioCtxRef.current = null; }

        // 4. 重置 UI
        setAudioLevel(0);
        setRecordingTime(0);
        onRestoreMusic?.();

        // 5. 收集所有音频块
        const chunks = [...recChunksRef.current];
        recChunksRef.current = [];
        recRef.current = null;

        // 6. 发送完整音频 → 后端
        if (chunks.length > 0) {
          setPhase('processing');
          // audioCtx 已关闭，此处创建临时 Context（仅用于解码，用完即关）
          const b64 = await webmToWavBase64(chunks);
          if (b64) {
            onAudioComplete(b64);
            console.log(`[录音] 最终音频已发送: ${b64.length} base64 chars, ${chunks.length} 个数据块`);
          }
        }
        onAudioFinal();

        // 7. 立即恢复唤醒词监听（不等 TTS 播完）
        // 录音结束就重启 Mellon，这样用户在 AI 语音播报期间也能用唤醒词打断
        if (enabledRef.current) {
          try {
            if (detectorRef.current && !detectorRef.current.listening) {
              await detectorRef.current.start();
              setPhase('waiting_for_wake');
              console.log('[唤醒词] 录音结束，已恢复监听（不等 TTS）');
            }
          } catch (err: any) {
            console.error('[唤醒词] 恢复监听失败:', err);
            // 静默失败，不影响录音流程
          }
        }
      };

      rec.start(500);

      // B-3: 不再增量发送。录音结束后一次性将完整音频发送到后端。
      return true;
    } catch (err: any) {
      console.error('[录音] startRecording 失败:', err.message);
      setError(err.message || '麦克风访问失败');
      // 恢复 Mellon 唤醒词监听：startRecording 前已停止 Mellon，失败必须重启
      if (enabledRef.current && detectorRef.current && !detectorRef.current.listening) {
        try {
          await detectorRef.current.start();
          setPhase('waiting_for_wake');
          console.log('[唤醒词] startRecording 失败，已恢复监听');
        } catch (e: any) {
          console.error('[唤醒词] 恢复监听也失败:', e.message);
          setPhase('idle');
        }
      } else {
        setPhase('idle');
      }
      return false;
    }
  }, [onDuckMusic, onAudioChunk, onAudioComplete, onAudioFinal, onRestoreMusic]);

  const stopRecording = useCallback(async (): Promise<string | null> => {
    // 如果 auto-stop 已经触发 → rec 已 inactive，onstop 已处理完毕
    const rec = recRef.current;
    if (!rec || rec.state === 'inactive') {
      console.log('[录音] 已由 auto-stop 处理，跳过手动停止');
      recRef.current = null;
      // auto-stop 已清理资源，直接返回
      return null;
    }

    // 手动停止：先清理定时器，再覆盖 onstop 以接收 Promise 结果
    if (levelTimerRef.current) { clearInterval(levelTimerRef.current); levelTimerRef.current = null; }
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }

    return new Promise((resolve) => {
      rec.onstop = async () => {
        // 清理 stream
        if (recStreamRef.current) { recStreamRef.current.getTracks().forEach(t => t.stop()); recStreamRef.current = null; }
        // 关闭 AudioContext
        if (audioCtxRef.current) { audioCtxRef.current.close().catch(() => { }); audioCtxRef.current = null; }

        setAudioLevel(0);
        setRecordingTime(0);
        onRestoreMusic?.();

        const chunks = [...recChunksRef.current];
        recChunksRef.current = [];
        recRef.current = null;

        if (chunks.length === 0) { resolve(null); return; }

        const b64 = await webmToWavBase64(chunks);
        resolve(b64);
      };
      rec.stop();
    });
  }, [onRestoreMusic]);

  // ═══════════════════════════════════════
  // Mellon 唤醒词逻辑
  // ═══════════════════════════════════════

  const initMellon = useCallback(async (): Promise<boolean> => {
    try {
      const { Detector, Storage } = await import('mellon');

      const savedRefs = Storage.loadWords(STORAGE_KEY);
      let refs = savedRefs || [];

      if (refs.length === 0) {
        setIsEnrolled(false);
        return false;
      }

      setIsEnrolled(true);

      const detector = new Detector(
        [{
          name: 'xiaoai',
          triggers: [{ name: wakeWord }],
          onMatch: async (triggerName: string, confidence: number) => {
            console.log(`[唤醒词] 检测到 "${triggerName}" 置信度=${confidence.toFixed(2)}`);

            // 置信度过滤：低于阈值视为噪声/误触发（如 TTS 回声）
            if (confidence < WAKE_CONFIDENCE_THRESHOLD) {
              console.log(`[唤醒词] 置信度过低 (${confidence.toFixed(2)} < ${WAKE_CONFIDENCE_THRESHOLD})，忽略`);
              return;
            }

            // 冷却检查：防止短时间内连触发
            const now = Date.now();
            if (now - lastWakeTimeRef.current < WAKE_COOLDOWN_MS) {
              console.log(`[唤醒词] 冷却中 (距上次 ${now - lastWakeTimeRef.current}ms < ${WAKE_COOLDOWN_MS}ms)，忽略`);
              return;
            }
            lastWakeTimeRef.current = now;

            if (detectorRef.current?.listening) {
              await detectorRef.current.stop();
            }
            onWakeDetected();
            setPhase('wake_detected');
            // 延时 600ms 确保扬声器 TTS/音乐残响完全消散后再开始录音
            // 300ms 不够：扬声器音频尾巴会被麦克风捕获，导致 STT 识别为噪声
            setTimeout(async () => {
              if (enabledRef.current) {
                const ok = await startRecording();
                if (ok) {
                  console.log('[唤醒词] 自动开始录音');
                }
              }
            }, 600);
          },
        }],
        {
          refsStorageKey: STORAGE_KEY,
          log: false,
        }
      );

      detectorRef.current = detector;
      await detector.init();
      await detector.start();
      setPhase('waiting_for_wake');
      console.log('[唤醒词] Mellon 已启动，等待唤醒词:', wakeWord);
      return true;
    } catch (err: any) {
      console.error('[唤醒词] Mellon 初始化失败:', err);
      setError(`唤醒引擎加载失败: ${err.message}`);
      setPhase('idle');
      return false;
    }
  }, [wakeWord, onWakeDetected, startRecording]);

  // ═══════════════════════════════════════
  // 公开操作
  // ═══════════════════════════════════════

  const enable = useCallback(async () => {
    enabledRef.current = true;
    setError('');
    setPhase('initializing');

    const ok = await initMellon();
    if (!ok) {
      const { Storage } = await import('mellon');
      const savedRefs = Storage.loadWords(STORAGE_KEY);
      if (savedRefs.length === 0) {
        setPhase('idle');
      }
    }
  }, [initMellon]);

  const disable = useCallback(() => {
    enabledRef.current = false;
    cleanup();
    setPhase('idle');
  }, [cleanup]);

  const resumeWakeListening = useCallback(async () => {
    if (!enabledRef.current) return;
    setPhase('processing');
    if (recStreamRef.current) { recStreamRef.current.getTracks().forEach(t => t.stop()); recStreamRef.current = null; }
    if (detectorRef.current) {
      try {
        if (!detectorRef.current.listening) {
          await detectorRef.current.start();
        }
        setPhase('waiting_for_wake');
        console.log('[唤醒词] 恢复监听');
      } catch (err: any) {
        console.error('[唤醒词] 恢复监听失败:', err);
        await initMellon();
      }
    } else {
      await initMellon();
    }
  }, [initMellon, cleanup]);

  const startManualRecord = useCallback(async () => {
    const ok = await startRecording();
    if (!ok) onError('无法开始录音');
  }, [startRecording, onError]);

  const stopManualRecord = useCallback(async (): Promise<string | null> => {
    const b64 = await stopRecording();
    setPhase('processing');
    return b64;
  }, [stopRecording]);

  // ═══════════════════════════════════════
  // 返回
  // ═══════════════════════════════════════

  const state: VoiceInteractionState = { phase, recordingTime, audioLevel, error, isEnrolled };
  const actions: VoiceInteractionActions = { enable, disable, resumeWakeListening, startManualRecord, stopManualRecord };

  return [state, actions];
}
