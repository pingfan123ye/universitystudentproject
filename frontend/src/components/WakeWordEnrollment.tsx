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
  const sessionRef = useRef<any>(null);

  // ★ 组件卸载时清理 EnrollmentSession，释放麦克风
  useEffect(() => {
    return () => {
      if (sessionRef.current) {
        console.log('[注册] 组件卸载，释放 EnrollmentSession');
        sessionRef.current = null;
      }
    };
  }, []);

  const TOTAL_SAMPLES = 3;

  const recordSample = useCallback(async () => {
    try {
      setStep('recording');
      setError('');

      const { EnrollmentSession } = await import('mellon');
      if (!sessionRef.current) {
        sessionRef.current = new EnrollmentSession(wakeWord);
      }

      const count = await sessionRef.current.recordSample();
      setSampleCount(count);
      setStep('idle');

      // 录满 3 次 → 自动生成
      if (count >= TOTAL_SAMPLES) {
        setStep('generating');
        const ref = await sessionRef.current.generateRef();

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
  }, [sampleCount]);

  const resetAll = useCallback(() => {
    sessionRef.current = null;
    setSampleCount(0);
    setStep('idle');
    setError('');
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
          marginBottom: '20px', overflow: 'hidden',
        }}>
          <div style={{
            height: '100%', width: `${progressPct}%`,
            borderRadius: '3px',
            background: 'var(--accent, #6366f1)',
            transition: 'width 0.3s ease',
          }} />
        </div>

        {/* 状态提示 */}
        <div style={{ marginBottom: '20px', minHeight: '40px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          {step === 'idle' && sampleCount < TOTAL_SAMPLES && (
            <span style={{ fontSize: '13px', color: 'var(--text-secondary, #bbb)' }}>
              第 <strong>{sampleCount + 1}</strong>/{TOTAL_SAMPLES} 次 · 点击按钮开始录制
            </span>
          )}
          {step === 'recording' && (
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
                disabled={step === 'recording' || step === 'generating'}
                style={{
                  ...btnPrimary,
                  opacity: (step === 'recording' || step === 'generating') ? 0.6 : 1,
                  cursor: (step === 'recording' || step === 'generating') ? 'default' : 'pointer',
                }}
              >
                {step === 'recording' ? '录制中...' :
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
