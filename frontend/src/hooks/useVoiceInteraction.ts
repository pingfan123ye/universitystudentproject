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
const STORAGE_KEY = 'mellon-xiaozhi-refs';
const MAX_RECORD_SECONDS = 12;        // 最长录音秒数
const WAKE_CONFIDENCE_THRESHOLD = 0.60;  // 唤醒词最低置信度（用户声纹 sim≈0.66~0.68，留 ~0.06 余量）
const WAKE_COOLDOWN_MS = 2000;           // 两次唤醒最小间隔（防止连触发）

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
    recordingRef.current = false;
    setRecordingTime(0);
    setAudioLevel(0);
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
            console.log(`[唤醒词] 🎯🎯🎯 onMatch 触发! trigger="${triggerName}" confidence=${confidence.toFixed(3)}`);

            if (confidence < WAKE_CONFIDENCE_THRESHOLD) {
              console.log(`[唤醒词] 置信度过低 (${confidence.toFixed(3)} < ${WAKE_CONFIDENCE_THRESHOLD})，忽略`);
              return;
            }

            // ★ 录音期间忽略唤醒词（防止用户说话中的"小智"触发 cancel 打断自身）
            if (recordingRef.current) {
              console.log(`[唤醒词] 录音中，忽略唤醒词检测 (confidence=${confidence.toFixed(3)})`);
              return;
            }

            // ★ TTS 反馈防护：录音刚结束的失聪期内忽略唤醒
            if (Date.now() < wakeDeafUntilRef.current) {
              console.log(`[唤醒词] 失聪期内忽略 (剩余${wakeDeafUntilRef.current - Date.now()}ms)`);
              return;
            }

            const now = Date.now();
            if (now - lastWakeTimeRef.current < WAKE_COOLDOWN_MS) {
              console.log(`[唤醒词] 冷却中 (${now - lastWakeTimeRef.current}ms)，忽略`);
              return;
            }
            lastWakeTimeRef.current = now;
            console.log('[唤醒词] ✅✅✅ 唤醒词确认! 置信度=' + confidence.toFixed(3));

            if (detectorRef.current?.listening) {
              await detectorRef.current.stop();
            }
            onWakeDetected();
            setPhase('wake_detected');
            setTimeout(async () => {
              // ★ 600ms 内用户可能已点 disable → 检查 phase 防止死循环
              if (enabledRef.current && phaseRef.current === 'wake_detected') {
                const ok = await startRecording();
                if (ok) console.log('[唤醒词] 自动开始录音');
              } else {
                console.log('[唤醒词] 超时到达但已取消 (phase=' + phaseRef.current + ')');
              }
            }, 600);
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

      // ★ 步骤 5：启动麦克风监听
      console.log('[唤醒词] 🎤 步骤5: detector.start() — 启动麦克风监听...');
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

  const state: VoiceInteractionState = { phase, recordingTime, audioLevel, error, isEnrolled };
  const actions: VoiceInteractionActions = { enable, disable, resumeWakeListening, startManualRecord, stopManualRecord };

  return [state, actions];
}
