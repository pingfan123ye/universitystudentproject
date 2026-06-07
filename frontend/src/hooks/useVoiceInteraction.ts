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
const MAX_RECORD_SECONDS = 15;        // 最长录音秒数
const WAKE_CONFIDENCE_THRESHOLD = 0.55;  // 唤醒词最低置信度（用户声纹 sim≈0.66~0.68，留 0.1 余量防误触发）
const WAKE_COOLDOWN_MS = 2000;           // 两次唤醒最小间隔（防止连触发）

// AnalyserNode 自适应静音检测参数
// autoGainControl=true 时 AGC 会放大静音底噪、压低语音增益，导致语音/底噪对比度大幅降低。
// 因此使用双阈值滞后（hysteresis）：语音触发阈值较低，静音判定阈值更低。
const LEVEL_CHECK_INTERVAL_MS = 250;    // 电平检测间隔
const NOISE_WINDOW_SAMPLES = 16;        // 噪声基线窗口 = 16 × 250ms = 4 秒
const SPEECH_RATIO = 2.5;               // 语音触发：RMS > 噪声基线 × 2.5（AGC 下约 1.5~3 倍）
const SPEECH_THRESHOLD_MIN = 0.004;     // 语音触发最低绝对值（用户语音 RMS ~0.005）
const SILENCE_RATIO = 1.8;              // 静音判定：RMS < 噪声基线 × 1.8（滞后，避免 AGC 底噪阻止静音检测）
const SILENCE_THRESHOLD_MIN = 0.002;    // 静音判定最低绝对值（与语音阈值保持比例）
const MIN_SPEECH_DURATION_MS = 400;     // 最少连续语音时长
const SILENCE_TIMEOUT_MS = 2000;        // 连续静音超时 → 自动停止

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
      // 新一轮录音开始，重置取消标志
      cancelledRef.current = false;

      // 先确保 Mellon 已停止（释放其麦克风流）
      if (detectorRef.current?.listening) {
        await detectorRef.current.stop();
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,   // ★ 关闭 AEC：录音流上 AEC 过度衰减人声(峰值/9)，导致 STT 音频不可用
          noiseSuppression: true,
          autoGainControl: true,    // 唤醒词 Mellon 和 STT 都需要足够信号
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
      analyser.fftSize = 1024;  // 21ms 窗口（48kHz），RMS 更稳定
      analyser.smoothingTimeConstant = 0.3;
      source.connect(analyser);
      // 不 connect 到 destination — 避免回声
      const timeData = new Float32Array(analyser.fftSize);  // ★ 32-bit 浮点精度，低振幅准确

      // ── 自适应噪声基线 + 双阈值滞后静音检测 ──
      // 关键：AGC 环境下语音/底噪比仅 1.5~3 倍，单阈值无法同时满足"灵敏触发"和"准确静音"
      // 方案：语音触发用较高阈值（SPEECH_RATIO），静音判定用较低阈值（SILENCE_RATIO）
      const noiseWindow: number[] = [];       // 滑动窗口 RMS 历史
      let speechDuration = 0;                 // 连续语音累计
      let silenceDuration = 0;                // 连续静音累计
      let hasSpeech = false;                  // 是否已确认检测到语音
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

        // 噪声基线 = 窗口内最小值（稳定环境 3 秒找底噪）
        const noiseFloor = noiseWindow.length > 0
          ? noiseWindow.reduce((a, b) => Math.min(a, b), Infinity)
          : 0.01;

        // ★ 双阈值：语音触发阈值 > 静音判定阈值（滞后）
        const speechThreshold = Math.max(noiseFloor * SPEECH_RATIO, SPEECH_THRESHOLD_MIN);
        const silenceThreshold = Math.max(noiseFloor * SILENCE_RATIO, SILENCE_THRESHOLD_MIN);

        // 更新 UI 电平条
        setAudioLevel(Math.min(rms / 0.2, 1));

        // ★ 诊断日志：前 N 次采样详细输出，便于调参
        if (sampleCount <= LOG_SAMPLES) {
          console.log(
            `[静音检测 #${sampleCount}] rms=${rms.toFixed(5)} ` +
            `noiseFloor=${noiseFloor.toFixed(5)} ` +
            `speechThr=${speechThreshold.toFixed(5)} ` +
            `silenceThr=${silenceThreshold.toFixed(5)} ` +
            `hasSpeech=${hasSpeech} speechDur=${speechDuration}ms silenceDur=${silenceDuration}ms`
          );
        }

        // ★ 滞后状态机
        if (!hasSpeech) {
          // 状态 A：等待语音触发 → 使用较高阈值
          if (rms > speechThreshold) {
            speechDuration += LEVEL_CHECK_INTERVAL_MS;
            silenceDuration = 0;
            if (speechDuration >= MIN_SPEECH_DURATION_MS) {
              hasSpeech = true;
              silenceDuration = 0;
              console.log(`[静音检测] ✅ 语音确认 (rms=${rms.toFixed(5)} > speechThr=${speechThreshold.toFixed(5)}, 累计${speechDuration}ms)`);
            }
          } else {
            speechDuration = Math.max(0, speechDuration - LEVEL_CHECK_INTERVAL_MS);  // 逐渐衰减，容忍短暂波动
          }
        } else {
          // 状态 B：已确认语音 → 使用较低阈值检测静音
          if (rms < silenceThreshold) {
            silenceDuration += LEVEL_CHECK_INTERVAL_MS;
            if (silenceDuration >= SILENCE_TIMEOUT_MS) {
              const r = recRef.current;
              if (r && r.state === 'recording') {
                console.log(
                  `[静音检测] 🛑 自动停止 ` +
                  `rms=${rms.toFixed(5)} < silenceThr=${silenceThreshold.toFixed(5)} ` +
                  `noiseFloor=${noiseFloor.toFixed(5)} silence=${silenceDuration}ms`
                );
                autoStoppedRef.current = true;
                r.stop();
              }
            }
          } else {
            // 还有语音 → 重置静音计时
            silenceDuration = 0;
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
        if (enabledRef.current) {
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
        console.warn('[唤醒词] ⚠️ 无声纹，需要先注册');
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
              if (enabledRef.current) {
                const ok = await startRecording();
                if (ok) console.log('[唤醒词] 自动开始录音');
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

      // ★ 步骤 4：加载模型 + 初始化
      console.log('[唤醒词] 🧠 步骤4: detector.init() — 加载 ONNX 模型、WASM、声纹嵌入...');
      const t0 = Date.now();
      await detector.init();
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
