import { useState, useRef, useEffect, useCallback } from 'react';

// 内置测试曲目
const DEFAULT_TRACK = '/music/running-up-that-hill.mp3';

type PlayerState = 'idle' | 'playing' | 'paused';

export function useMusicPlayer() {
  const [playerState, setPlayerState] = useState<PlayerState>('idle');
  const [trackName, setTrackName] = useState('');
  const [error, setError] = useState('');
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    if (!audioRef.current) {
      audioRef.current = new Audio();
      audioRef.current.onended = () => setPlayerState('idle');
      audioRef.current.onerror = () => {
        setError('音频加载失败');
        setPlayerState('idle');
      };
      audioRef.current.onloadeddata = () => setError('');
    }
    return () => {
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }
    };
  }, []);

  const play = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;

    // 如果没加载过，先加载
    if (!audio.src || audio.src === window.location.href) {
      audio.src = DEFAULT_TRACK;
      setTrackName('Running Up That Hill');
    }
    audio.play().then(() => {
      setPlayerState('playing');
      setError('');
    }).catch(() => {
      setError('播放失败，请点击页面任意位置后再试');
    });
  }, []);

  const pause = useCallback(() => {
    audioRef.current?.pause();
    setPlayerState('paused');
  }, []);

  const doAction = useCallback((action: string) => {
    setError('');
    switch (action) {
      case 'play':
        if (playerState === 'paused') {
          audioRef.current?.play().then(() => setPlayerState('playing')).catch(() => {});
        } else {
          play();
        }
        break;
      case 'pause':
        pause();
        break;
      default:
        break;
    }
  }, [playerState, play, pause]);

  return {
    playerState,
    trackName,
    error,
    doAction,
  };
}
