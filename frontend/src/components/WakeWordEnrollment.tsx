import { useState, useRef, useCallback, useEffect } from 'react';

interface WakeWordEnrollmentProps {
  wakeWord: string;
  onComplete: () => void;   // 注册完成回调
  onCancel: () => void;     // 取消回调
}

export default function WakeWordEnrollment({ wakeWord, onComplete, onCancel }: WakeWordEnrollmentProps) {
  const [step, setStep] = useState<'idle' | 'recording' | 'generating' | 'done' | 'error'>('idle');
  const [sampleCount, setSampleCount] = useState(0);
  const [error, setError] = useState('');
  const [volumeLevel, setVolumeLevel] = useState(0);        // ★ 最近一次录音的峰值振幅（0-1）
  const [volumeWarning, setVolumeWarning] = useState('');   // ★ 音量过低警告
  const [countdown, setCountdown] = useState<number | null>(null);  // ★ 录音前倒计时 3-2-1
  const [qualityError, setQualityError] = useState('');     // ★ 质量门控拒绝提示
  const sessionRef = useRef<any>(null);
  const countdownRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ★ 组件卸载时清理 EnrollmentSession + 倒计时定时器，释放麦克风
  useEffect(() => {
    return () => {
      if (countdownRef.current) { clearTimeout(countdownRef.current); }
      if (sessionRef.current) {
        console.log('[注册] 组件卸载，释放 EnrollmentSession');
        sessionRef.current = null;
      }
    };
  }, []);

  const TOTAL_SAMPLES = 3;

  // ★ 带重试的模型加载：CDN 可能间歇性断连，最多重试 3 次，指数退避
  async function retryFetch<T>(fn: () => Promise<T>, label: string, maxRetries = 3): Promise<T> {
    let lastErr: any;
    for (let i = 0; i <= maxRetries; i++) {
      try {
        if (i > 0) {
          const delay = Math.min(1000 * Math.pow(2, i - 1), 8000);
          console.log(`[注册] ${label} 重试 ${i}/${maxRetries}，等待 ${delay}ms...`);
          await new Promise(r => setTimeout(r, delay));
        }
        return await fn();
      } catch (err: any) {
        lastErr = err;
        if (i < maxRetries) {
          console.warn(`[注册] ${label} 失败 (${err.message})，准备重试...`);
        }
      }
    }
    throw lastErr;
  }

  const MIN_PEAK = 0.12;  // ★ 质量门控：峰值低于此值拒绝样本

  const recordSample = useCallback(async () => {
    try {
      // ★ 倒计时 3-2-1，让用户准备好再开口
      setQualityError('');
      setStep('recording');  // 按钮禁用
      for (let c = 3; c >= 1; c--) {
        setCountdown(c);
        await new Promise<void>((resolve) => {
          countdownRef.current = setTimeout(resolve, 1000);
        });
      }
      setCountdown(null);

      const { EnrollmentSession } = await import('mellon');
      if (!sessionRef.current) {
        sessionRef.current = new EnrollmentSession(wakeWord);
      }

      const count = await sessionRef.current.recordSample();

      // ★ 质量门控：检查录音振幅，拒绝低质量样本
      try {
        const pcm = sessionRef.current.getSample(count - 1) as Float32Array | undefined;
        if (pcm && pcm.length > 0) {
          let peak = 0;
          for (let i = 0; i < pcm.length; i++) {
            const abs = Math.abs(pcm[i]);
            if (abs > peak) peak = abs;
          }
          setVolumeLevel(peak);
          console.log(`[注册] 样本#${count} 峰值振幅: ${peak.toFixed(4)} (门控阈值=${MIN_PEAK})`);

          if (peak < MIN_PEAK) {
            // ★ 拒绝：音量过低，有可能是环境噪声或没说话
            await sessionRef.current.deleteSample(count - 1);
            setQualityError(`⚠️ 未检测到有效声音 (峰值 ${(peak*100).toFixed(1)}%)，请大声说出 "${wakeWord}" 后重录`);
            setVolumeWarning('❌ 录音被拒绝，请重试');
            setStep('idle');
            return;
          }

          // ✅ 通过质量门控
          setQualityError('');
          if (peak < 0.08) {
            setVolumeWarning('🔊 音量偏低，建议大声一些以确保识别准确');
          } else {
            setVolumeWarning('');
          }
        }
      } catch {
        // getSample 可能不兼容，忽略
      }

      setSampleCount(count);
      setStep('idle');

      // 录满 3 次 → 自动生成（带重试，CDN 可能间歇断连）
      if (count >= TOTAL_SAMPLES) {
        setStep('generating');
        const ref = await retryFetch(
          () => sessionRef.current.generateRef(),
          'generateRef'
        );

        const { Storage } = await import('mellon');
        Storage.saveWord(ref, 'mellon-xiaozhi-refs');

        setStep('done');
        setTimeout(() => onComplete(), 800);
      }
    } catch (err: any) {
      console.error('[注册] 录制失败:', err);
      const msg = err.message || '录制失败，请重试';
      // ★ 识别 CDN/网络错误，给用户更明确的提示
      if (msg.includes('fetch') || msg.includes('Network') || msg.includes('Failed to')) {
        setError('网络连接失败，Mellon 模型下载可能被阻断（jsdelivr/huggingface CDN）。请检查网络或使用代理后重试。');
      } else if (msg.includes('microphone') || msg.includes('permission') || msg.includes('NotAllowed')) {
        setError('麦克风权限被拒绝，请在浏览器设置中允许麦克风访问后重试。');
      } else {
        setError(msg);
      }
      setStep('error');
    }
  }, [wakeWord, onComplete]);

  const resetSample = useCallback(async () => {
    if (sessionRef.current && sampleCount > 0) {
      await sessionRef.current.deleteSample(sampleCount - 1);
      setSampleCount(prev => Math.max(0, prev - 1));
    }
    setQualityError('');
    setVolumeWarning('');
  }, [sampleCount]);

  const resetAll = useCallback(() => {
    if (countdownRef.current) { clearTimeout(countdownRef.current); }
    sessionRef.current = null;
    setSampleCount(0);
    setStep('idle');
    setError('');
    setQualityError('');
    setVolumeWarning('');
    setCountdown(null);
  }, []);

  const progressPct = (sampleCount / TOTAL_SAMPLES) * 100;

  return (
    <div style={{
      position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
      zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(4px)',
    }}>
      <div style={{
        background: 'var(--bg-surface, #1e1e2e)',
        borderRadius: '16px', padding: '32px 28px',
        maxWidth: '400px', width: '90%',
        boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
        color: 'var(--text-primary, #e0e0e0)',
        textAlign: 'center',
      }}>
        {/* 标题 */}
        <div style={{ fontSize: '28px', marginBottom: '8px' }}>🎤</div>
        <h2 style={{ fontSize: '18px', fontWeight: 700, margin: '0 0 4px' }}>首次设置语音助手</h2>
        <p style={{ fontSize: '13px', color: 'var(--text-muted, #888)', margin: '0 0 24px' }}>
          请说出唤醒词 <strong style={{ color: 'var(--accent, #6366f1)' }}>"{wakeWord}"</strong> 完成语音注册
        </p>

        {/* 进度条 */}
        <div style={{
          height: '6px', borderRadius: '3px', background: 'var(--border, #333)',
          marginBottom: '16px', overflow: 'hidden',
        }}>
          <div style={{
            height: '100%', width: `${progressPct}%`,
            borderRadius: '3px',
            background: 'var(--accent, #6366f1)',
            transition: 'width 0.3s ease',
          }} />
        </div>

        {/* ★ 音量指示条（录制后显示） */}
        {sampleCount > 0 && step === 'idle' && (
          <div style={{ marginBottom: '12px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
              <span style={{ fontSize: '11px', color: 'var(--text-muted, #888)' }}>📢 录音音量</span>
              <div style={{
                flex: 1, height: '8px', borderRadius: '4px',
                background: 'var(--border, #333)', overflow: 'hidden',
              }}>
                <div style={{
                  height: '100%', width: `${Math.min(volumeLevel * 100, 100)}%`,
                  borderRadius: '4px',
                  background: volumeLevel < 0.03 ? '#ef4444'
                    : volumeLevel < 0.08 ? '#f59e0b'
                    : '#22c55e',
                  transition: 'width 0.3s ease',
                }} />
              </div>
              <span style={{
                fontSize: '10px', color: 'var(--text-muted, #888)', minWidth: '32px', textAlign: 'right',
              }}>
                {(volumeLevel * 100).toFixed(0)}%
              </span>
            </div>
            {volumeWarning && (
              <div style={{
                fontSize: '11px', padding: '6px 10px', borderRadius: '6px',
                background: volumeLevel < 0.03 ? 'rgba(239,68,68,0.15)' : 'rgba(245,158,11,0.15)',
                color: volumeLevel < 0.03 ? '#ef4444' : '#f59e0b',
              }}>
                {volumeWarning}
              </div>
            )}
          </div>
        )}

        {/* 状态提示 */}
        <div style={{ marginBottom: '20px', minHeight: '40px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          {countdown !== null && (
            <span style={{ fontSize: '40px', fontWeight: 900, color: 'var(--accent, #6366f1)', lineHeight: 1 }}>
              {countdown}
            </span>
          )}
          {countdown === null && step === 'idle' && sampleCount < TOTAL_SAMPLES && (
            <span style={{ fontSize: '13px', color: 'var(--text-secondary, #bbb)' }}>
              第 <strong>{sampleCount + 1}</strong>/{TOTAL_SAMPLES} 次 · 点击按钮开始录制
            </span>
          )}
          {countdown === null && step === 'recording' && (
            <span style={{ fontSize: '13px', color: '#ef4444', fontWeight: 600 }}>
              <span style={{ display: 'inline-block', width: '8px', height: '8px', borderRadius: '50%', background: '#ef4444', marginRight: '8px', animation: 'pulse 0.8s infinite' }} />
              录制中... 请大声说出 "{wakeWord}"
            </span>
          )}
          {step === 'generating' && (
            <span style={{ fontSize: '13px', color: 'var(--accent, #6366f1)' }}>
              ⏳ 正在生成语音模型...
            </span>
          )}
          {step === 'done' && (
            <span style={{ fontSize: '13px', color: '#22c55e', fontWeight: 600 }}>
              ✅ 注册成功！正在启动语音助手...
            </span>
          )}
          {step === 'error' && (
            <span style={{ fontSize: '13px', color: '#ef4444' }}>
              ⚠️ {error}
            </span>
          )}
        </div>

        {/* ★ 质量门控拒绝提示 */}
        {qualityError && (
          <div style={{
            fontSize: '12px', padding: '8px 12px', borderRadius: '8px',
            background: 'rgba(239,68,68,0.15)', color: '#ef4444',
            marginBottom: '16px', textAlign: 'center', fontWeight: 600,
          }}>
            {qualityError}
          </div>
        )}

        {/* 按钮区 */}
        <div style={{ display: 'flex', gap: '10px', justifyContent: 'center' }}>
          {step === 'error' ? (
            <>
              <button onClick={resetAll} style={btnSecondary}>
                重新开始
              </button>
              <button onClick={onCancel} style={btnDanger}>
                跳过设置
              </button>
            </>
          ) : step === 'done' ? null : (
            <>
              <button onClick={onCancel} style={btnSecondary}>
                跳过
              </button>
              {sampleCount > 0 && step === 'idle' && (
                <button onClick={resetSample} style={btnSecondary}>
                  重录上一次
                </button>
              )}
              <button
                onClick={recordSample}
                disabled={step === 'recording' || step === 'generating' || countdown !== null}
                style={{
                  ...btnPrimary,
                  opacity: (step === 'recording' || step === 'generating' || countdown !== null) ? 0.6 : 1,
                  cursor: (step === 'recording' || step === 'generating' || countdown !== null) ? 'default' : 'pointer',
                }}
              >
                {countdown !== null ? `准备录音 ${countdown}...` :
                 step === 'recording' ? '录制中...' :
                 step === 'generating' ? '生成中...' :
                 sampleCount === 0 ? '🎙 开始录制' :
                 `继续录制 (${sampleCount}/${TOTAL_SAMPLES})`}
              </button>
            </>
          )}
        </div>

        <p style={{ fontSize: '11px', color: 'var(--text-muted, #666)', marginTop: '16px', marginBottom: 0 }}>
          录制环境尽量安静，用平时说话的音量和语速即可
        </p>
      </div>
    </div>
  );
}

const btnPrimary: React.CSSProperties = {
  padding: '10px 20px', borderRadius: '8px', border: 'none',
  background: 'var(--accent, #6366f1)', color: '#fff',
  fontSize: '13px', fontWeight: 600, cursor: 'pointer',
  transition: 'opacity 0.2s',
};

const btnSecondary: React.CSSProperties = {
  padding: '10px 20px', borderRadius: '8px', border: '1px solid var(--border, #333)',
  background: 'transparent', color: 'var(--text-secondary, #bbb)',
  fontSize: '13px', cursor: 'pointer',
};

const btnDanger: React.CSSProperties = {
  padding: '10px 20px', borderRadius: '8px', border: 'none',
  background: '#ef4444', color: '#fff',
  fontSize: '13px', cursor: 'pointer',
};
