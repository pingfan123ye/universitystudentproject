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
  pauseAudioBuffer: () => void;       // ★ TTS 播放时暂停 PCM 缓冲，防止自触发
  resumeAudioBuffer: () => Promise<void>;  // ★ TTS 播完后恢复 PCM 缓冲
  setDeafUntil: (timestamp: number) => void;  // ★ TTS 播放时设置失聪期，Mellon 触发后直接忽略
}

// ── 常量 ──
const STORAGE_KEY = 'mellon-xiaozhi-refs';
const MAX_RECORD_SECONDS = 12;        // 最长录音秒数
const WAKE_CONFIDENCE_THRESHOLD = 0.55;  // ★ 唤醒词最低置信度（大幅降低：由 STT 验证兜底过滤环境噪声）
const WAKE_COOLDOWN_MS = 5000;           // 假触发冷却（5 秒内不再触发，防止环境噪声连触发）
const STT_VERIFY_RECORD_MS = 2500;       // STT 验证录音时长（毫秒）: 覆盖用户说唤醒词+指令的开头
const CONSECUTIVE_FOR_DIRECT = 3;        // 连续 STT 验证通过次数 → 切换到直接模式（跳过 STT）
const DIRECT_MODE_FALSE_LIMIT = 2;       // 直接模式下连续假触发次数 → 退回 STT 验证模式

// AnalyserNode 自适应静音检测参数（三策略融合）
// 策略A（主）：语音峰值衰减 — RMS < 峰值×20% 判定疑似停止，解决 AGC 放大底噪
// 策略B（辅）：快速停止 — RMS < 峰值×10% 仅需 800ms 确认
// 策略C（兜底）：双阈值滞后 — 噪声基线×比率，适用于安静环境
const LEVEL_CHECK_INTERVAL_MS = 250;    // 电平检测间隔
const NOISE_WINDOW_SAMPLES = 16;        // 噪声基线窗口 = 16 × 250ms = 4 秒
const SPEECH_RATIO = 2.5;               // 语音触发：RMS > 噪声基线 × 2.5（AGC 下约 1.5~3 倍）
const SPEECH_THRESHOLD_MIN = 0.004;     // 语音触发最低绝对值（用户语音 RMS ~0.005）
const SILENCE_RATIO = 2.5;              // 静音判定：RMS < 噪声基线 × 2.5（兜底策略，主策略为峰值衰减检测）
const SILENCE_THRESHOLD_MIN = 0.002;    // 静音判定最低绝对值（与语音阈值保持比例）
const MIN_SPEECH_DURATION_MS = 300;     // 最少连续语音时长
const SILENCE_TIMEOUT_MS = 1500;        // 连续静音超时 → 自动停止
const PEAK_DECAY_RATIO = 0.20;           // 峰值衰减检测：RMS < 语音峰值 × 20% → 疑似停止（AGC 环境下主策略）
const QUICK_STOP_RATIO = 0.10;          // 快速停止：RMS < 语音峰值 × 10% → 确认时间缩短至 800ms
const QUICK_STOP_TIMEOUT_MS = 800;      // 快速停止的静音确认时间
const PEAK_WINDOW_SAMPLES = 8;          // 语音峰值追踪窗口 = 8 × 250ms = 2 秒
const NO_SPEECH_TIMEOUT_MS = 3000;      // 无语音超时：3 秒未检测到语音 → 自动停止（防止幽灵唤醒浪费录音）

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
  } catch (err: any) {
    console.warn('[音频转换] webmToWavBase64 失败:', err?.message || err, 'chunks:', chunks.length);
    return null;
  }
}

// ── PCM Float32 → WAV base64（直接编码，无 WebM 编解码，无 decodeAudioData 失败风险）──
function pcmToWavBase64(pcm: Float32Array, sampleRate: number): string {
  const numChannels = 1;
  const bitsPerSample = 16;
  const byteRate = sampleRate * numChannels * bitsPerSample / 8;
  const blockAlign = numChannels * bitsPerSample / 8;
  const dataSize = pcm.length * 2;

  const buf = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buf);
  view.setUint32(0, 0x52494646, false);
  view.setUint32(4, 36 + dataSize, true);
  view.setUint32(8, 0x57415645, false);
  view.setUint32(12, 0x666D7420, false);
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bitsPerSample, true);
  view.setUint32(36, 0x64617461, false);
  view.setUint32(40, dataSize, true);

  const i16 = new Int16Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) {
    const s = Math.max(-1, Math.min(1, pcm[i]));
    i16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  const u8 = new Uint8Array(buf);
  u8.set(new Uint8Array(i16.buffer), 44);

  let binary = '';
  for (let i = 0; i < u8.length; i++) binary += String.fromCharCode(u8[i]);
  return btoa(binary);
}

export function useVoiceInteraction(opts: VoiceInteractionOptions): [VoiceInteractionState, VoiceInteractionActions] {
  const { wakeWord, onWakeDetected, onAudioChunk, onAudioComplete, onAudioFinal, onError, onDuckMusic, onRestoreMusic } = opts;

  const [phase, setPhase] = useState<VoicePhase>('idle');
  const [recordingTime, setRecordingTime] = useState(0);
  const [audioLevel, setAudioLevel] = useState(0);
  const [error, setError] = useState('');
  const [isEnrolled, setIsEnrolled] = useState(false);
  const [lastConfidence, setLastConfidence] = useState(0);
  const [wakeMode, setWakeMode] = useState<'stt_verify' | 'direct'>('stt_verify');

  // ── Refs ──
  const detectorRef = useRef<any>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const recChunksRef = useRef<Blob[]>([]);
  const recStreamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);  // Mellon 心跳监控
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
  // 标记是否正在录音（防止录音期间唤醒词误触发 → cancel 打断自身）
  const recordingRef = useRef(false);
  // TTS 反馈防护：录音结束后短暂失聪期，防止扬声器 TTS 音频被麦克风捕获误触发唤醒
  const wakeDeafUntilRef = useRef(0);
  // ★ 验证中标记：防止心跳自愈在 STT 验证期间误触发
  const verifyingRef = useRef(false);
  // ★ 自适应唤醒模式：stt_verify（低阈值+STT验证） / direct（纯Mellon直接唤醒）
  const wakeModeRef = useRef<'stt_verify' | 'direct'>('stt_verify');
  // ★ 连续 STT 验证通过计数（≥3 次 → 切换到 direct 模式）
  const consecutiveSttPassesRef = useRef(0);
  // ★ 直接模式下假触发计数（连续假触发 → 退回 stt_verify 模式）
  const directModeFalseCountRef = useRef(0);
  // ★ PCM 循环缓冲：在 Mellon 监听期间持续录制原始 PCM，确保唤醒词触发时
  //   缓冲中已有 4 秒历史音频（包含唤醒词本身），彻底解决"post-trigger 录音
  //   只捕获沉默"的问题。ScriptProcessor → Float32 循环数组 → 直接 WAV 编码
  const PCM_BUFFER_SIZE = 4096;          // ScriptProcessor 块大小
  const PCM_BUFFER_CHUNKS = 16;          // 16 × 4096 / 16000 ≈ 4.1 秒循环缓冲
  const pcmChunksRef = useRef<Float32Array[]>([]);
  const bufferStreamRef = useRef<MediaStream | null>(null);
  const bufferAudioCtxRef = useRef<AudioContext | null>(null);
  const bufferProcessorRef = useRef<ScriptProcessorNode | null>(null);

  // 同步 state → ref
  useEffect(() => { phaseRef.current = phase; }, [phase]);

  // ── 清理所有资源 ──
  const cleanup = useCallback(() => {
    // 标记取消：阻止异步 onstop 发送音频 + 切到 processing
    cancelledRef.current = true;
    // ★ 停止 PCM 循环缓冲（先于 Mellon 停止，释放麦克风）
    if (bufferProcessorRef.current) {
      bufferProcessorRef.current.disconnect();
      bufferProcessorRef.current = null;
    }
    if (bufferAudioCtxRef.current) {
      bufferAudioCtxRef.current.close().catch(() => {});
      bufferAudioCtxRef.current = null;
    }
    if (bufferStreamRef.current) {
      bufferStreamRef.current.getTracks().forEach(t => t.stop());
      bufferStreamRef.current = null;
    }
    pcmChunksRef.current = [];
    if (detectorRef.current) {
      detectorRef.current.stop().catch(() => { });
      detectorRef.current = null;
    }
    // ★ 停止循环音频缓冲
    setPhase('idle');
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
    recordingRef.current = false;
    verifyingRef.current = false;
    // ★ 重置自适应模式追踪
    consecutiveSttPassesRef.current = 0;
    directModeFalseCountRef.current = 0;
    setRecordingTime(0);
    setAudioLevel(0);
    setLastConfidence(0);
  }, []);

  // ═══════════════════════════════════════
  // ★ PCM 循环缓冲：在 Mellon 监听期间持续录制 4 秒原始音频
  //   启动顺序：缓冲先于 Mellon（缓冲拿主流麦克风，避免双流 WebM 损坏）
  //   当 Mellon 触发 onMatch → 直接取缓冲中的历史 PCM → WAV 编码 → STT 验证
  // ═══════════════════════════════════════
  const startAudioBuffer = useCallback(async (): Promise<boolean> => {
    try {
      // 停止旧缓冲（如果存在）
      if (bufferProcessorRef.current) {
        bufferProcessorRef.current.disconnect();
        bufferProcessorRef.current = null;
      }
      if (bufferAudioCtxRef.current) {
        await bufferAudioCtxRef.current.close().catch(() => {});
        bufferAudioCtxRef.current = null;
      }
      if (bufferStreamRef.current) {
        bufferStreamRef.current.getTracks().forEach(t => t.stop());
        bufferStreamRef.current = null;
      }

      // 1. 获取麦克风流（先于 Mellon 启动，成为主流）
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,   // 关闭 AEC → 保留人声振幅，STT 需要完整信号
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      bufferStreamRef.current = stream;

      // 2. AudioContext → ScriptProcessor → PCM 循环缓冲
      const audioCtx = new AudioContext({ sampleRate: 16000 });
      bufferAudioCtxRef.current = audioCtx;

      const source = audioCtx.createMediaStreamSource(stream);
      const processor = audioCtx.createScriptProcessor(PCM_BUFFER_SIZE, 1, 1);
      bufferProcessorRef.current = processor;

      pcmChunksRef.current = [];
      processor.onaudioprocess = (e: AudioProcessingEvent) => {
        const inputData = e.inputBuffer.getChannelData(0);
        // 复制一份（inputData 在回调返回后可能被复用）
        pcmChunksRef.current.push(new Float32Array(inputData));
        // 循环：保持最近 ~4 秒
        while (pcmChunksRef.current.length > PCM_BUFFER_CHUNKS) {
          pcmChunksRef.current.shift();
        }
      };

      source.connect(processor);
      processor.connect(audioCtx.destination);  // 必须连接以触发 onaudioprocess

      console.log(`[PCM缓冲] 🟢 循环缓冲已启动 (${PCM_BUFFER_CHUNKS} chunks, ~${(PCM_BUFFER_SIZE * PCM_BUFFER_CHUNKS / 16000).toFixed(1)}s)`);
      return true;
    } catch (err: any) {
      console.error('[PCM缓冲] 启动失败:', err.message);
      return false;
    }
  }, []);

  const stopAudioBuffer = useCallback(() => {
    if (bufferProcessorRef.current) {
      bufferProcessorRef.current.disconnect();
      bufferProcessorRef.current = null;
    }
    if (bufferAudioCtxRef.current) {
      bufferAudioCtxRef.current.close().catch(() => {});
      bufferAudioCtxRef.current = null;
    }
    if (bufferStreamRef.current) {
      bufferStreamRef.current.getTracks().forEach(t => t.stop());
      bufferStreamRef.current = null;
    }
    pcmChunksRef.current = [];
    console.log('[PCM缓冲] 🔴 循环缓冲已停止');
  }, []);

  // ★ TTS 播放时暂停 PCM 缓冲（保留 Mellon 运行以支持打断）
  const bufferPausedRef = useRef(false);
  const pauseAudioBuffer = useCallback(() => {
    if (bufferPausedRef.current) return;  // 已暂停，避免重复操作
    stopAudioBuffer();
    bufferPausedRef.current = true;
    console.log('[PCM缓冲] ⏸️ 已暂停（TTS 播放中，Mellon 仍在运行）');
  }, [stopAudioBuffer]);

  const resumeAudioBuffer = useCallback(async () => {
    if (!bufferPausedRef.current) return;  // 未暂停，无需操作
    bufferPausedRef.current = false;
    await startAudioBuffer();
    await new Promise(r => setTimeout(r, 500));  // 积累 500ms 音频
    console.log('[PCM缓冲] ▶️ 已恢复');
  }, [startAudioBuffer]);

  // ★ TTS 播放时设置失聪期：Mellon 仍运行（支持打断），但 onMatch 在失聪期内直接忽略
  const setDeafUntil = useCallback((timestamp: number) => {
    wakeDeafUntilRef.current = Math.max(wakeDeafUntilRef.current, timestamp);
    console.log(`[唤醒词] 🔇 失聪期已设置: ${((timestamp - Date.now()) / 1000).toFixed(1)}s`);
  }, []);

  // ── 页面隐藏/切换标签时自动暂停/恢复 ──
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden && detectorRef.current?.listening) {
        detectorRef.current.stop().catch(() => { });
      } else if (!document.hidden && enabledRef.current) {
        // 切回页面时，只要非录音中/空闲，都尝试重启 Mellon
        const ph = phaseRef.current;
        if (ph !== 'recording' && ph !== 'idle') {
          detectorRef.current?.start().catch(() => { });
          if (ph !== 'waiting_for_wake') {
            setPhase('waiting_for_wake');
          }
        }
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
      // ★ 如果已被外部取消（disable/cleanup 在 setTimeout 间隙触发），不再启动
      if (cancelledRef.current) {
        console.log('[录音] 已被外部取消，跳过 startRecording');
        return false;
      }

      // 先确保 Mellon 已停止（释放其麦克风流）
      if (detectorRef.current?.listening) {
        await detectorRef.current.stop();
      }
      // ★ 标记录音中：防止 onMatch 在录音期间误触发 cancel
      recordingRef.current = true;

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,   // ★ 关闭 AEC：录音流上 AEC 过度衰减人声(峰值/9)，导致 STT 音频不可用
          noiseSuppression: true,
          autoGainControl: true,    // 唤醒词 Mellon 和 STT 都需要足够信号
        },
      });
      recStreamRef.current = stream;
      autoStoppedRef.current = false;
      cancelledRef.current = false;  // ★ 确认启动成功后才清除取消标志（防止覆盖 disable() 的取消）
      onDuckMusic?.();

      // ── 电平检测 AudioContext（录音期间复用 1 个）──
      const audioCtx = new AudioContext();
      audioCtxRef.current = audioCtx;
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 1024;  // 21ms 窗口（48kHz），RMS 更稳定
      analyser.smoothingTimeConstant = 0.3;
      source.connect(analyser);
      // 不 connect 到 destination — 避免回声
      const timeData = new Float32Array(analyser.fftSize);  // ★ 32-bit 浮点精度，低振幅准确

      // ── 三策略融合静音检测 ──
      // 策略A（主）：语音峰值衰减 — RMS < 峰值×20% 判定疑似停止，解决 AGC 放大底噪
      // 策略B（辅）：快速停止 — RMS < 峰值×10% 仅需 800ms 确认
      // 策略C（兜底）：噪声基线双阈值滞后 — 安静环境下噪声基线×比率
      const noiseWindow: number[] = [];       // 滑动窗口 RMS 历史（用于噪声基线）
      const peakWindow: number[] = [];        // 语音峰值追踪窗口
      let speechDuration = 0;                 // 连续语音累计
      let silenceDuration = 0;                // 连续静音累计
      let hasSpeech = false;                  // 是否已确认检测到语音
      let speechPeak = 0.05;                  // 语音峰值（默认 0.05，启动后由实际语音更新）
      let noSpeechDuration = 0;               // 无语音累计（用于幽灵唤醒检测：3 秒无声 → 停止）
      let sampleCount = 0;                    // ★ 诊断：采样计数
      const LOG_SAMPLES = 20;                 // ★ 诊断：前 20 次采样详细日志

      levelTimerRef.current = setInterval(() => {
        sampleCount++;
        analyser.getFloatTimeDomainData(timeData);  // ★ 32-bit 浮点：值已在 [-1,1]，无需归一化
        // 计算 RMS
        let sum = 0;
        for (let i = 0; i < timeData.length; i++) {
          sum += timeData[i] * timeData[i];
        }
        const rms = Math.sqrt(sum / timeData.length);

        // 更新噪声基线窗口
        noiseWindow.push(rms);
        if (noiseWindow.length > NOISE_WINDOW_SAMPLES) noiseWindow.shift();

        // 噪声基线 = 窗口内最小值（稳定环境找底噪）
        const noiseFloor = noiseWindow.length > 0
          ? noiseWindow.reduce((a, b) => Math.min(a, b), Infinity)
          : 0.01;

        // ★ 双阈值：语音触发阈值 > 静音判定阈值（滞后，兜底策略C）
        const speechThreshold = Math.max(noiseFloor * SPEECH_RATIO, SPEECH_THRESHOLD_MIN);
        const silenceThreshold = Math.max(noiseFloor * SILENCE_RATIO, SILENCE_THRESHOLD_MIN);

        // ★ 策略A/B：基于语音峰值的衰减阈值
        const peakDecayThreshold = speechPeak * PEAK_DECAY_RATIO;   // 峰值 20% → 疑似停止
        const quickStopThreshold = speechPeak * QUICK_STOP_RATIO;   // 峰值 10% → 快速停止

        // 更新 UI 电平条
        setAudioLevel(Math.min(rms / 0.2, 1));

        // ★ 诊断日志：前 N 次采样详细输出，便于调参
        if (sampleCount <= LOG_SAMPLES) {
          console.log(
            `[静音检测 #${sampleCount}] rms=${rms.toFixed(5)} ` +
            `noiseFloor=${noiseFloor.toFixed(5)} speechPeak=${speechPeak.toFixed(5)} ` +
            `speechThr=${speechThreshold.toFixed(5)} silenceThr=${silenceThreshold.toFixed(5)} ` +
            `peakDecayThr=${peakDecayThreshold.toFixed(5)} quickStopThr=${quickStopThreshold.toFixed(5)} ` +
            `hasSpeech=${hasSpeech} speechDur=${speechDuration}ms silenceDur=${silenceDuration}ms`
          );
        }

        // ★★★ 三策略融合状态机 ★★★
        if (!hasSpeech) {
          // 状态 A：等待语音触发 → 使用较高阈值
          if (rms > speechThreshold) {
            speechDuration += LEVEL_CHECK_INTERVAL_MS;
            silenceDuration = 0;
            noSpeechDuration = 0;  // 有声音 → 重置无语音计时
            // 语音激活期间追踪峰值
            peakWindow.push(rms);
            if (peakWindow.length > PEAK_WINDOW_SAMPLES) peakWindow.shift();
            if (rms > speechPeak) speechPeak = rms;  // 即时更新峰值（上升沿）
            if (speechDuration >= MIN_SPEECH_DURATION_MS) {
              hasSpeech = true;
              silenceDuration = 0;
              // 确认语音后，用窗口内最大值校准 speechPeak
              if (peakWindow.length > 0) {
                speechPeak = peakWindow.reduce((a, b) => Math.max(a, b), speechPeak);
              }
              console.log(`[静音检测] ✅ 语音确认 (rms=${rms.toFixed(5)} > speechThr=${speechThreshold.toFixed(5)}, peak=${speechPeak.toFixed(5)}, 累计${speechDuration}ms)`);
            }
          } else {
            speechDuration = Math.max(0, speechDuration - LEVEL_CHECK_INTERVAL_MS);  // 逐渐衰减，容忍短暂波动
            // 无语音时清空峰值窗口（避免残留噪声污染峰值）
            if (peakWindow.length > 0 && speechDuration === 0) peakWindow.length = 0;
            // ★ 无语音超时：幽灵唤醒（环境噪声误触发）→ 3 秒无声自动停止
            noSpeechDuration += LEVEL_CHECK_INTERVAL_MS;
            if (noSpeechDuration >= NO_SPEECH_TIMEOUT_MS) {
              const r = recRef.current;
              if (r && r.state === 'recording') {
                console.log(
                  `[静音检测] 🛑 无语音超时停止 ` +
                  `rms=${rms.toFixed(5)} < speechThr=${speechThreshold.toFixed(5)} ` +
                  `noSpeech=${noSpeechDuration}ms (阈值${NO_SPEECH_TIMEOUT_MS}ms)`
                );
                autoStoppedRef.current = true;
                r.stop();
              }
            }
          }
        } else {
          // 状态 B：已确认语音 → 三策略融合判定停止
          // 策略A：峰值衰减检测（AGC 环境主策略）
          const isSilenceByPeakDecay = rms < peakDecayThreshold;
          // 策略C：噪声基线兜底（安静环境）
          const isSilenceByNoiseFloor = rms < silenceThreshold;
          // 策略B：快速停止条件
          const isQuickStop = rms < quickStopThreshold;

          // 综合判断：峰值衰减 OR 噪声基线 任一满足即视为静音
          const isSilence = isSilenceByPeakDecay || isSilenceByNoiseFloor;

          if (isSilence) {
            silenceDuration += LEVEL_CHECK_INTERVAL_MS;
            // 策略B：快速停止使用更短的确认时间
            const effectiveTimeout = isQuickStop ? QUICK_STOP_TIMEOUT_MS : SILENCE_TIMEOUT_MS;
            if (silenceDuration >= effectiveTimeout) {
              const r = recRef.current;
              if (r && r.state === 'recording') {
                const trigger = isQuickStop ? '快速停止' : (isSilenceByPeakDecay ? '峰值衰减' : '噪声基线');
                console.log(
                  `[静音检测] 🛑 自动停止(${trigger}) ` +
                  `rms=${rms.toFixed(5)} peakDecayThr=${peakDecayThreshold.toFixed(5)} ` +
                  `silenceThr=${silenceThreshold.toFixed(5)} silence=${silenceDuration}ms`
                );
                autoStoppedRef.current = true;
                r.stop();
              }
            }
          } else {
            // 还有语音 → 重置静音计时 + 更新语音峰值
            silenceDuration = 0;
            peakWindow.push(rms);
            if (peakWindow.length > PEAK_WINDOW_SAMPLES) peakWindow.shift();
            // 仅在 RMS 上升时更新峰值，防止噪声污染峰值
            if (rms > speechPeak) speechPeak = rms;
            // 定期用窗口最大值校准（捕获缓慢上升的语音趋势）
            if (peakWindow.length >= PEAK_WINDOW_SAMPLES && sampleCount % 4 === 0) {
              const windowMax = peakWindow.reduce((a, b) => Math.max(a, b), 0);
              if (windowMax > speechPeak * 0.8) speechPeak = Math.max(speechPeak, windowMax * 0.9);
            }
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
          recordingRef.current = false;
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

        // 0. 清除录音标记（防止后续 onMatch 误触发）
        recordingRef.current = false;

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

        // 7. 恢复唤醒词监听（设置短暂失聪期防止 TTS 反馈触发误唤醒）
        if (enabledRef.current) {
          wakeDeafUntilRef.current = Date.now() + 1500;  // 1.5 秒失聪期
          await restartDetector();
        }
      };

      rec.start(500);

      // B-3: 不再增量发送。录音结束后一次性将完整音频发送到后端。
      return true;
    } catch (err: any) {
      console.error('[录音] startRecording 失败:', err.message);
      setError(err.message || '麦克风访问失败');
      // ★ 恢复唤醒词监听（startRecording 前已停止 Mellon）
      if (enabledRef.current) {
        restartDetector();
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

  /**
   * 简单唤醒词恢复：先尝试 detector.start()，失败则重建
   * 普通 async 函数，不是 useCallback，通过闭包访问 initMellon
   */
  const restartDetector = async () => {
    if (!enabledRef.current) return;
    try {
      // ★ 先启动 PCM 缓冲（占主流麦克风），再启动 Mellon
      if (!bufferStreamRef.current || !bufferAudioCtxRef.current) {
        console.log('[唤醒词] 重建 PCM 缓冲...');
        await startAudioBuffer();
        await new Promise(r => setTimeout(r, 500));  // 积累 0.5s 音频
      }
      if (detectorRef.current && !detectorRef.current.listening) {
        await detectorRef.current.start();
        console.log('[唤醒词] 恢复监听完成, listening=' + detectorRef.current.listening);
      } else if (!detectorRef.current) {
        console.warn('[唤醒词] 检测器为空，重新初始化');
        await initMellon();
      }
      if (detectorRef.current?.listening) {
        setPhase('waiting_for_wake');
      }
    } catch (err: any) {
      console.error('[唤醒词] 恢复监听失败:', err.message);
      // 重试一次
      try {
        console.warn('[唤醒词] 重试初始化...');
        await initMellon();
      } catch (e2: any) {
        console.error('[唤醒词] 重试也失败:', e2.message);
      }
    }
  };
  // ═══════════════════════════════════════
  // ★ STT 唤醒词二次验证：从 PCM 循环缓冲取历史音频（含唤醒词）→ WAV → STT
  //   缓冲先于 Mellon 启动 → 触发时缓冲已有 ~4s 历史音频 → 必然包含唤醒词
  //   彻底解决"post-trigger 录音只捕获沉默"问题
  // ═══════════════════════════════════════
  const verifyWakeWord = useCallback(async (): Promise<boolean> => {
    try {
      // 1. 停止 Mellon 释放麦克风
      if (detectorRef.current?.listening) {
        await detectorRef.current.stop();
      }

      // 2. 从循环缓冲中取出历史 PCM（在停止缓冲前取出）
      const chunks = [...pcmChunksRef.current];
      pcmChunksRef.current = [];

      // 3. 关闭 PCM 缓冲（释放麦克风给后续录音）
      if (bufferProcessorRef.current) {
        bufferProcessorRef.current.disconnect();
        bufferProcessorRef.current = null;
      }
      if (bufferAudioCtxRef.current) {
        bufferAudioCtxRef.current.close().catch(() => {});
        bufferAudioCtxRef.current = null;
      }
      if (bufferStreamRef.current) {
        bufferStreamRef.current.getTracks().forEach(t => t.stop());
        bufferStreamRef.current = null;
      }

      // 4. 缓冲为空 → 回退直接录音
      if (chunks.length === 0) {
        console.warn('[唤醒词验证] ⚠️ PCM缓冲为空，回退到直接录音 2.5s...');
        const fallbackStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true },
        });
        const fbChunks: Blob[] = [];
        const fbMimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
          ? 'audio/webm;codecs=opus' : 'audio/webm';
        const fbRec = new MediaRecorder(fallbackStream, { mimeType: fbMimeType });
        fbRec.ondataavailable = (e) => { if (e.data.size > 0) fbChunks.push(e.data); };
        const fbB64 = await new Promise<string | null>((resolve) => {
          fbRec.onstop = async () => {
            fallbackStream.getTracks().forEach(t => t.stop());
            if (fbChunks.length === 0) { resolve(null); return; }
            resolve(await webmToWavBase64(fbChunks));
          };
          fbRec.start(250);
          setTimeout(() => { if (fbRec.state !== 'inactive') fbRec.stop(); }, 2500);
        });
        if (!fbB64) return false;
        console.log(`[唤醒词验证] 回退录音完成 (${fbB64.length} chars)，发送后端 STT...`);
        const result = await (opts.onVerifyWake?.(fbB64) ?? Promise.resolve(false));
        console.log(`[唤醒词验证] 后端返回: ${result ? '✅ 验证通过' : '❌ 验证拒绝'}`);
        return result;
      }

      // 5. 合并 PCM → WAV → base64
      const totalLen = chunks.reduce((s, c) => s + c.length, 0);
      const merged = new Float32Array(totalLen);
      let offset = 0;
      for (const c of chunks) {
        merged.set(c, offset);
        offset += c.length;
      }

      // 取最后 4 秒（16kHz × 4 = 64000 samples）
      const maxSamples = 16000 * 4;
      const trimmed = totalLen > maxSamples
        ? merged.slice(totalLen - maxSamples)
        : merged;

      const b64 = pcmToWavBase64(trimmed, 16000);
      const audioLen = (trimmed.length / 16000).toFixed(1);
      console.log(`[唤醒词验证] PCM缓冲: ${chunks.length} chunks, ${trimmed.length} samples (${audioLen}s), WAV ${b64.length} chars`);

      const result = await (opts.onVerifyWake?.(b64) ?? Promise.resolve(false));
      console.log(`[唤醒词验证] 后端返回: ${result ? '✅ 验证通过' : '❌ 验证拒绝'}`);
      return result;
    } catch (err: any) {
      console.error('[唤醒词验证] 失败:', err.message);
      return false;
    }
  }, [opts.onVerifyWake]);

  // ═══════════════════════════════════════
  // ★ 假触发处理：5 秒冷却 + 自适应模式退回
  // ═══════════════════════════════════════
  const handleFalsePositive = useCallback(() => {
    if (wakeModeRef.current === 'direct') {
      directModeFalseCountRef.current += 1;
      if (directModeFalseCountRef.current >= DIRECT_MODE_FALSE_LIMIT) {
        wakeModeRef.current = 'stt_verify';
        setWakeMode('stt_verify');
        consecutiveSttPassesRef.current = 0;
        directModeFalseCountRef.current = 0;
        console.log('[唤醒词] ⚠️ 直接模式连续假触发，退回 STT 验证模式');
      }
    }
    // 固定 5 秒冷却
    wakeDeafUntilRef.current = Date.now() + WAKE_COOLDOWN_MS;
    verifyingRef.current = false;
    console.log(`[唤醒词] ❌ 假触发，${WAKE_COOLDOWN_MS / 1000}秒冷却后恢复监听`);
    // 冷却后恢复 PCM 缓冲 + Mellon 监听
    setTimeout(async () => {
      if (enabledRef.current && phaseRef.current !== 'recording') {
        await startAudioBuffer();
        await new Promise(r => setTimeout(r, 500));
        await restartDetector();
      }
    }, WAKE_COOLDOWN_MS);
  }, []);

  const initMellon = useCallback(async (): Promise<boolean> => {
    try {
      // ★ 步骤 1：动态导入 mellon 模块
      console.log('[唤醒词] 📦 步骤1: 动态导入 mellon ...');
      const { Detector, Storage } = await import('mellon');
      console.log('[唤醒词] ✅ mellon 模块导入成功');

      // ★ 步骤 2：加载声纹
      console.log('[唤醒词] 💾 步骤2: 加载声纹 (key=' + STORAGE_KEY + ') ...');
      let savedRefs = Storage.loadWords(STORAGE_KEY);
      let refs: any[] = savedRefs || [];

      // 从旧键迁移
      if (refs.length === 0) {
        const oldRefs = Storage.loadWords('mellon-xiaoai-refs');
        if (oldRefs && oldRefs.length > 0) {
          console.log('[唤醒词] 从旧键迁移 %d 条声纹', oldRefs.length);
          for (const r of oldRefs) {
            try { Storage.saveWord(r, STORAGE_KEY); } catch { }
          }
          refs = Storage.loadWords(STORAGE_KEY) || [];
        }
      }

      // ★ 打印每条声纹详情
      console.log('[唤醒词] 声纹总数: %d', refs.length);
      refs.forEach((r: any, i: number) => {
        console.log(`[唤醒词]   声纹#${i+1}: word_name="${r.word_name}" embeddings=${r.embeddings?.length || 0}组`);
      });

      if (refs.length === 0) {
        // ★ IndexedDB 可能暂时不可读（Mellon 异步操作未完成），重试一次
        console.warn('[唤醒词] ⚠️ 第一次读取声纹为空，1 秒后重试...');
        await new Promise(r => setTimeout(r, 1000));
        savedRefs = Storage.loadWords(STORAGE_KEY);
        refs = savedRefs || [];
        console.log('[唤醒词] 重试后声纹总数: %d', refs.length);
      }

      if (refs.length === 0) {
        console.warn('[唤醒词] ⚠️ 重试仍无声纹，需要重新注册');
        setIsEnrolled(false);
        return false;
      }

      setIsEnrolled(true);
      console.log('[唤醒词] ✅ 声纹加载成功');

      // ★ 步骤 3：创建 Detector
      console.log('[唤醒词] 🔧 步骤3: 创建 Detector, trigger="%s"', wakeWord);
      const detector = new Detector(
        [{
          name: 'xiaozhi',
          triggers: [{ name: wakeWord }],
          onMatch: async (triggerName: string, confidence: number) => {
            // ── 诊断日志：输出所有有效匹配（≥0.40），包含当前模式便于调参 ──
            if (confidence >= 0.40) {
              console.log(
                `[唤醒词] 🔊 检测到匹配 trigger="${triggerName}" confidence=${confidence.toFixed(3)} ` +
                `(阈值=${WAKE_CONFIDENCE_THRESHOLD}) mode=${wakeModeRef.current} ` +
                `${confidence >= WAKE_CONFIDENCE_THRESHOLD ? '✅ 通过' : '❌ 低于阈值'}`
              );
            }

            // ── 门控 1：置信度阈值（统一 0.55，由 STT 验证兜底过滤噪声）──
            if (confidence < WAKE_CONFIDENCE_THRESHOLD) return;

            // ── 门控 2：录音期间互斥 ──
            if (recordingRef.current) return;

            // ── 门控 3：冷却期（假触发后 5 秒内不响应）──
            const now = Date.now();
            if (now < wakeDeafUntilRef.current) return;

            // ── 门控 4：唤醒间隔（防止连触发）──
            if (now - lastWakeTimeRef.current < WAKE_COOLDOWN_MS) return;

            console.log(`[唤醒词] 🎯 声纹匹配 confidence=${confidence.toFixed(3)}`);
            setLastConfidence(confidence);

            // ═══════════════════════════════════════
            // ★ 自适应唤醒模式（简化：单阈值 0.55 + STT 验证兜底）
            //   direct 模式：纯 Mellon 直接唤醒（已积累 ≥3 次 STT 验证通过）
            //   stt_verify 模式：低阈值触发 → PCM 缓冲 → STT 验证 → 唤醒/拒绝
            // ═══════════════════════════════════════

            if (wakeModeRef.current === 'direct') {
              // ⚡ 直接模式：即时唤醒，无 STT 延迟
              console.log('[唤醒词] ⚡ 直接模式，立即唤醒');
              lastWakeTimeRef.current = now;
              verifyingRef.current = true;
              // ★ 停止 PCM 缓冲（startRecording 中也会做，但提前停止减少冲突）
              stopAudioBuffer();
              onWakeDetected();
              setPhase('wake_detected');
              setTimeout(async () => {
                if (enabledRef.current && phaseRef.current === 'wake_detected') {
                  verifyingRef.current = false;
                  const ok = await startRecording();
                  if (ok) console.log('[唤醒词] 直接模式自动开始录音');
                }
              }, 200);
              return;
            }

            // 🔍 STT 验证模式：从 PCM 循环缓冲取历史音频 → STT 验证
            //   （缓冲中已包含唤醒词，无需重新录音，解决"post-trigger 录音只捕获沉默"问题）
            console.log('[唤醒词] 🔍 STT 验证模式，从 PCM 缓冲取历史音频...');
            verifyingRef.current = true;
            setPhase('verifying');

            try {
              // ★ 调用 verifyWakeWord：取 PCM 缓冲 → WAV → 后端 STT
              const passed = await verifyWakeWord();

              if (passed) {
                // ✅ STT 验证通过 → 真实人声
                console.log('[唤醒词验证] ✅ STT 验证通过，唤醒！');
                consecutiveSttPassesRef.current += 1;
                directModeFalseCountRef.current = 0;
                lastWakeTimeRef.current = Date.now();
                verifyingRef.current = false;

                // ★ 自适应升级：连续 N 次 STT 通过 → 切换到直接模式
                if (consecutiveSttPassesRef.current >= CONSECUTIVE_FOR_DIRECT) {
                  wakeModeRef.current = 'direct';
                  setWakeMode('direct');
                  console.log(`[唤醒词] 🚀 已切换到直接模式（连续${CONSECUTIVE_FOR_DIRECT}次STT验证通过）`);
                }

                onWakeDetected();
                setPhase('wake_detected');
                // ★ PCM 缓冲已释放麦克风，200ms 足够清理
                setTimeout(async () => {
                  if (enabledRef.current && phaseRef.current === 'wake_detected') {
                    const ok = await startRecording();
                    if (ok) console.log('[唤醒词] STT验证通过，自动开始录音');
                  }
                }, 200);
              } else {
                // ❌ STT 验证拒绝 → 假触发（需重建 PCM 缓冲 + 重启 Mellon）
                handleFalsePositive();
                if (enabledRef.current && phaseRef.current !== 'recording') {
                  await startAudioBuffer();
                  await restartDetector();
                }
              }
            } catch (err: any) {
              console.error('[唤醒词验证] 异常:', err.message);
              handleFalsePositive();
              if (enabledRef.current && phaseRef.current !== 'recording') {
                await startAudioBuffer();
                await restartDetector();
              }
            }
          },
        }],
        {
          refsStorageKey: STORAGE_KEY,
          log: true,
        }
      );

      detectorRef.current = detector;
      console.log('[唤醒词] ✅ Detector 创建完成');

      // ★ 步骤 4：加载模型 + 初始化（带重试，CDN 可能间歇断连）
      console.log('[唤醒词] 🧠 步骤4: detector.init() — 加载 ONNX 模型、WASM、声纹嵌入...');
      const t0 = Date.now();
      let initOk = false;
      let initErr: any;
      for (let retry = 0; retry <= 3; retry++) {
        try {
          if (retry > 0) {
            const delay = Math.min(1000 * Math.pow(2, retry - 1), 8000);
            console.log(`[唤醒词] init 重试 ${retry}/3，等待 ${delay}ms...`);
            await new Promise(r => setTimeout(r, delay));
          }
          await detector.init();
          initOk = true;
          break;
        } catch (err: any) {
          initErr = err;
          if (retry < 3) {
            console.warn(`[唤醒词] init 失败 (${err.message})，准备重试...`);
          }
        }
      }
      if (!initOk) {
        throw initErr || new Error('detector.init 所有重试均失败');
      }
      console.log('[唤醒词] ✅ detector.init() 完成 (%dms)', Date.now() - t0);

      // ★ 步骤 5：先启动 PCM 循环缓冲（占主流麦克风），再启动 Mellon
      console.log('[唤醒词] 🎤 步骤5a: startAudioBuffer() — 启动 PCM 循环缓冲...');
      const bufOk = await startAudioBuffer();
      if (bufOk) {
        // 缓冲先跑 1 秒积累音频，确保任何时刻触发都有 ≥1s 历史
        await new Promise(r => setTimeout(r, 1000));
        console.log('[唤醒词] ✅ PCM 缓冲已积累 1s+');
      } else {
        console.warn('[唤醒词] ⚠️ PCM 缓冲启动失败，回退到 post-trigger 录音模式');
      }

      console.log('[唤醒词] 🎤 步骤5b: detector.start() — 启动麦克风监听...');
      const t1 = Date.now();
      await detector.start();
      console.log('[唤醒词] ✅ detector.start() 完成 (%dms), listening=%s', Date.now() - t1, detector.listening);

      if (!detector.listening) {
        console.error('[唤醒词] ⚠️⚠️⚠️ detector.start() 返回但 listening=false！重新尝试...');
        // 多等一会，有时后台初始化较慢
        await new Promise(r => setTimeout(r, 1000));
        if (!detector.listening) {
          console.error('[唤醒词] ⚠️⚠️⚠️ 1s后仍 listening=false，尝试 restart...');
          await detector.start();
          console.log('[唤醒词] restart 后 listening=%s', detector.listening);
        }
      }

      setPhase('waiting_for_wake');
      console.log('[唤醒词] 🟢🟢🟢 Mellon 已就绪，等待唤醒词 "%s" (listening=%s)', wakeWord, detector.listening);
      return true;
    } catch (err: any) {
      console.error('[唤醒词] ❌❌❌ 初始化异常:', err.message, err.stack);
      const msg = err.message || '唤醒引擎加载失败';
      if (msg.includes('fetch') || msg.includes('Network') || msg.includes('Failed to')) {
        setError('唤醒引擎网络加载失败，CDN 可能被阻断');
      } else if (msg.includes('microphone') || msg.includes('permission') || msg.includes('NotAllowed')) {
        setError('麦克风权限被拒绝');
      } else {
        setError(`唤醒引擎加载失败: ${msg}`);
      }
      setPhase('idle');
      return false;
    }
  }, [wakeWord, onWakeDetected, startRecording]);

  // ── Mellon 心跳监控：每 8 秒检查，静默死亡自动恢复 ──
  useEffect(() => {
    heartbeatRef.current = setInterval(() => {
      if (!enabledRef.current) return;
      if (phaseRef.current !== 'waiting_for_wake') return;
      // ★ 验证期间不检查（Mellon 被主动停止以释放麦克风）
      if (verifyingRef.current) return;
      if (!detectorRef.current || !detectorRef.current.listening) {
        console.warn('[唤醒词心跳] listening=%s，触发自愈', detectorRef.current?.listening ?? 'null');
        restartDetector();
      }
    }, 8000);
    return () => {
      if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
    };
  }, []);

  // ═══════════════════════════════════════
  // 公开操作
  // ═══════════════════════════════════════

  const enable = useCallback(async () => {
    enabledRef.current = true;
    cancelledRef.current = false;  // ★ 新会话：重置取消标志（上次 disable 可能遗留 true）
    setError('');
    setPhase('initializing');

    let ok = await initMellon();
    if (!ok) {
      const { Storage } = await import('mellon');
      const savedRefs = Storage.loadWords(STORAGE_KEY);
      if (savedRefs.length === 0) {
        // 无声纹 → 回到 idle，提示用户注册
        setPhase('idle');
        return;
      }
      // ★ 声纹存在但 initMellon 失败（CDN/麦克风），延迟 1.5 秒后重试一次
      console.warn('[唤醒词] 首次初始化失败（声纹存在），1.5 秒后重试...');
      setError('唤醒引擎加载中，正在重试...');
      await new Promise(r => setTimeout(r, 1500));
      ok = await initMellon();
    }
    if (!ok) {
      // 重试仍失败
      setPhase('idle');
      setError('唤醒引擎启动失败，请检查网络后手动重试');
      console.error('[唤醒词] enable() 重试后仍失败');
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
    // 清理录音流（如果有残留）
    if (recStreamRef.current) { recStreamRef.current.getTracks().forEach(t => t.stop()); recStreamRef.current = null; }
    // ★ 统一恢复入口
    restartDetector();
  }, []);  // restartDetector 是普通函数，通过闭包访问 initMellon

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

  const state: VoiceInteractionState = { phase, recordingTime, audioLevel, error, isEnrolled, lastConfidence, wakeMode };
  const actions: VoiceInteractionActions = { enable, disable, resumeWakeListening, startManualRecord, stopManualRecord, pauseAudioBuffer, resumeAudioBuffer, setDeafUntil };

  return [state, actions];
}
