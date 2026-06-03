import { useState, useCallback, useRef } from 'react';

interface TTSOptions {
  lang?: string;
  rate?: number;
  pitch?: number;
  volume?: number;
}

export function useTTS(options: TTSOptions = {}) {
  const { lang = 'zh-CN', rate = 1.05, pitch = 1.0, volume = 1.0 } = options;
  const [speaking, setSpeaking] = useState(false);
  const [autoSpeak, setAutoSpeak] = useState(true);
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const isSupported =
    typeof window !== 'undefined' && 'speechSynthesis' in window;

  // ===== 浏览器 SpeechSynthesis（降级方案） =====
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

  const pickVoice = useCallback(async (): Promise<SpeechSynthesisVoice | null> => {
    const voices = await getVoices();
    const priorities = ['zh-CN', 'zh-Hans', 'zh-TW', 'zh-HK', 'zh'];
    for (const l of priorities) {
      const match = voices.find((v) => v.lang.startsWith(l));
      if (match) return match;
    }
    return voices.find((v) => v.lang.includes('zh')) || null;
  }, [getVoices]);

  const speakBrowser = useCallback(async (text: string, onEnd?: () => void) => {
    if (!isSupported || !text.trim()) { onEnd?.(); return; }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = lang;
    utterance.rate = rate;
    utterance.pitch = pitch;
    utterance.volume = volume;
    const voice = await pickVoice();
    if (voice) utterance.voice = voice;
    utterance.onstart = () => setSpeaking(true);
    utterance.onend = () => { setSpeaking(false); onEnd?.(); };
    utterance.onerror = () => { setSpeaking(false); onEnd?.(); };
    utteranceRef.current = utterance;
    window.speechSynthesis.speak(utterance);
  }, [isSupported, lang, rate, pitch, volume, pickVoice]);

  // ===== 后端 Edge TTS 音频播放（主力） =====
  const playAudioBase64 = useCallback((base64: string, onEnd?: () => void) => {
    try {
      // 停止当前播放
      window.speechSynthesis.cancel();
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }

      const audio = new Audio();
      audioRef.current = audio;

      // base64 → blob → object URL
      const byteChars = atob(base64);
      const byteNums = new Array(byteChars.length);
      for (let i = 0; i < byteChars.length; i++) {
        byteNums[i] = byteChars.charCodeAt(i);
      }
      const byteArray = new Uint8Array(byteNums);
      const blob = new Blob([byteArray], { type: 'audio/mpeg' });
      const url = URL.createObjectURL(blob);

      audio.src = url;
      audio.onended = () => {
        setSpeaking(false);
        URL.revokeObjectURL(url);
        audioRef.current = null;
        onEnd?.();
      };
      audio.onerror = () => {
        setSpeaking(false);
        URL.revokeObjectURL(url);
        audioRef.current = null;
        onEnd?.();
      };
      audio.oncanplay = () => {
        audio.play().catch(() => { setSpeaking(false); onEnd?.(); });
      };
      setSpeaking(true);
      audio.load();
    } catch {
      setSpeaking(false);
    }
  }, []);

  // ===== 统一接口：优先后端 TTS，降级浏览器 =====
  const speak = useCallback((text: string, backendAudio?: string, onEnd?: () => void) => {
    if (!autoSpeak) { onEnd?.(); return; }
    if (backendAudio) {
      playAudioBase64(backendAudio, onEnd);
    } else {
      speakBrowser(text, onEnd);
    }
  }, [autoSpeak, playAudioBase64, speakBrowser]);

  const stop = useCallback(() => {
    window.speechSynthesis.cancel();
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    setSpeaking(false);
  }, []);

  const toggleAutoSpeak = useCallback(() => {
    setAutoSpeak((prev) => {
      if (prev) stop();
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
    playAudioBase64,
  };
}
