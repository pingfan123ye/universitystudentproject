import { useState, useCallback, useRef } from 'react';

interface TTSOptions {
  lang?: string;
  rate?: number;
  pitch?: number;
  volume?: number;
}

export function useTTS(options: TTSOptions = {}) {
  const { lang = 'zh-CN', rate = 1.0, pitch = 1.0, volume = 1.0 } = options;
  const [speaking, setSpeaking] = useState(false);
  const [autoSpeak, setAutoSpeak] = useState(true);
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);

  const isSupported =
    typeof window !== 'undefined' && 'speechSynthesis' in window;

  // 等待语音列表加载（某些浏览器异步加载）
  const getVoices = useCallback((): Promise<SpeechSynthesisVoice[]> => {
    return new Promise((resolve) => {
      const voices = window.speechSynthesis.getVoices();
      if (voices.length > 0) {
        resolve(voices);
      } else {
        window.speechSynthesis.onvoiceschanged = () => {
          resolve(window.speechSynthesis.getVoices());
        };
      }
    });
  }, []);

  // 选择最佳中文语音
  const pickVoice = useCallback(async (): Promise<SpeechSynthesisVoice | null> => {
    const voices = await getVoices();
    // 优先级：简体中文 > 台湾中文 > 任何含中文的
    const priorities = ['zh-CN', 'zh-Hans', 'zh-TW', 'zh-HK', 'zh'];
    for (const lang of priorities) {
      const match = voices.find((v) => v.lang.startsWith(lang));
      if (match) return match;
    }
    // 有些语音虽然标注为 zh-CN 但 lang 不同
    const anyChinese = voices.find(
      (v) => v.lang.includes('zh') || v.name.includes('Tingting') || v.name.includes('Yaoyao')
    );
    return anyChinese || null;
  }, [getVoices]);

  const speak = useCallback(async (text: string) => {
    if (!isSupported || !text.trim()) return;

    // 先停掉当前正在播放的
    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = lang;
    utterance.rate = rate;
    utterance.pitch = pitch;
    utterance.volume = volume;

    const voice = await pickVoice();
    if (voice) {
      utterance.voice = voice;
    }

    utterance.onstart = () => setSpeaking(true);
    utterance.onend = () => setSpeaking(false);
    utterance.onerror = () => setSpeaking(false);

    utteranceRef.current = utterance;
    window.speechSynthesis.speak(utterance);
  }, [isSupported, lang, rate, pitch, volume, pickVoice]);

  const stop = useCallback(() => {
    window.speechSynthesis.cancel();
    setSpeaking(false);
  }, []);

  const toggleAutoSpeak = useCallback(() => {
    setAutoSpeak((prev) => {
      if (prev) {
        // 关闭自动播报，同时停止当前播放
        stop();
      }
      return !prev;
    });
  }, [stop]);

  return {
    isSupported,
    speaking,
    autoSpeak,
    speak,
    stop,
    toggleAutoSpeak,
  };
}
