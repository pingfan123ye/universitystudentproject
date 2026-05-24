import { useState, useRef, useCallback, useEffect } from 'react';

// Web Speech API 类型（浏览器内置，但 TS lib 未覆盖）
interface SpeechRecognitionInstance {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: SpeechRecognitionEvent) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
}

type RecorderState = 'idle' | 'listening' | 'error' | 'denied';

interface UseSpeechRecognitionOptions {
  lang?: string;
  onResult?: (text: string, isFinal: boolean) => void;
  onError?: (error: string) => void;
}

export function useSpeechRecognition(options: UseSpeechRecognitionOptions = {}) {
  const { lang = 'zh-CN', onResult, onError } = options;
  const [state, setState] = useState<RecorderState>('idle');
  const [interimText, setInterimText] = useState('');
  const [errorMessage, setErrorMessage] = useState('');
  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null);
  const finalTextRef = useRef('');
  const streamRef = useRef<MediaStream | null>(null);

  const isSupported =
    typeof window !== 'undefined' &&
    ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window);

  // 请求麦克风权限（必须在用户手势中调用）
  const requestMicPermission = useCallback(async (): Promise<boolean> => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      return true;
    } catch (err: unknown) {
      const e = err as DOMException;
      if (e.name === 'NotAllowedError') {
        setState('denied');
        setErrorMessage('麦克风权限被拒绝，请在浏览器设置中允许访问麦克风');
        onError?.('麦克风权限被拒绝');
      } else if (e.name === 'NotFoundError') {
        setState('error');
        setErrorMessage('未检测到麦克风设备，请检查麦克风是否正确连接');
        onError?.('未检测到麦克风设备');
      } else {
        setState('error');
        setErrorMessage(`麦克风访问失败: ${e.message}`);
        onError?.(`麦克风访问失败: ${e.message}`);
      }
      return false;
    }
  }, [onError]);

  // 释放麦克风
  const releaseMic = useCallback(() => {
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
  }, []);

  const start = useCallback(async () => {
    if (!isSupported) {
      setState('error');
      setErrorMessage('您的浏览器不支持语音识别，请使用 Chrome 或 Edge 浏览器');
      onError?.('浏览器不支持语音识别');
      return;
    }

    setErrorMessage('');
    finalTextRef.current = '';
    setInterimText('');

    // 先请求麦克风权限
    const hasPermission = await requestMicPermission();
    if (!hasPermission) return;

    const SpeechRecognitionAPI =
      (window as unknown as { SpeechRecognition?: unknown }).SpeechRecognition ||
      (window as unknown as { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition;

    if (!SpeechRecognitionAPI) return;

    const rec = new (SpeechRecognitionAPI as unknown as { new(): SpeechRecognitionInstance })();
    rec.lang = lang;
    rec.continuous = false;
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    recognitionRef.current = rec;

    rec.onresult = (event: SpeechRecognitionEvent) => {
      let interim = '';
      let final = '';

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) {
          final += result[0].transcript;
        } else {
          interim += result[0].transcript;
        }
      }

      if (final) {
        finalTextRef.current += final;
      }

      const displayText = finalTextRef.current + interim;
      setInterimText(displayText);

      // 所有结果都是 final 时回调
      const allFinal = event.results.length > 0 &&
        Array.from({ length: event.results.length }, (_, i) => event.results[i]).every(r => r.isFinal);
      onResult?.(displayText, allFinal);
    };

    rec.onerror = (event: SpeechRecognitionErrorEvent) => {
      if (event.error === 'no-speech') {
        // 没检测到语音
        setState('idle');
        releaseMic();
        return;
      }
      if (event.error === 'aborted') {
        setState('idle');
        releaseMic();
        return;
      }
      if (event.error === 'not-allowed') {
        setState('denied');
        setErrorMessage('麦克风权限被拒绝');
        onError?.('麦克风权限被拒绝');
        releaseMic();
        return;
      }
      if (event.error === 'audio-capture') {
        setState('error');
        setErrorMessage('无法访问麦克风，请确认麦克风未被其他程序占用');
        onError?.('无法访问麦克风');
        releaseMic();
        return;
      }
      console.warn('SpeechRecognition error:', event.error);
      setState('idle');
      releaseMic();
    };

    rec.onend = () => {
      setState('idle');
      releaseMic();
    };

    rec.onstart = () => {
      setState('listening');
    };

    try {
      rec.start();
    } catch {
      setState('error');
      setErrorMessage('无法启动语音识别，请刷新页面后重试');
      onError?.('无法启动语音识别');
      releaseMic();
    }
  }, [isSupported, lang, onResult, onError, requestMicPermission, releaseMic]);

  const stop = useCallback(() => {
    if (recognitionRef.current) {
      recognitionRef.current.stop();
      recognitionRef.current = null;
    }
    releaseMic();
    setState('idle');
  }, [releaseMic]);

  useEffect(() => {
    return () => {
      recognitionRef.current?.abort();
      releaseMic();
    };
  }, [releaseMic]);

  return {
    isSupported,
    state,
    interimText,
    errorMessage,
    start,
    stop,
  };
}
