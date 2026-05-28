import { useState, useRef, useCallback, useEffect } from 'react';
import { SongInfo, MusicControlData } from '../types';

// 将第三方音乐 URL 转为后端代理（解决跨域播放）
function proxyUrl(url: string): string {
  if (!url || url.startsWith('/') || url.startsWith('blob:') || url.startsWith('data:')) return url;
  return `/api/proxy/music?url=${encodeURIComponent(url)}`;
}

// 默认退底曲目
const DEFAULT_TRACK = '/music/running-up-that-hill.mp3';
const DEFAULT_SONG: SongInfo = {
  song_id: '__default__',
  song_name: 'Running Up That Hill',
  singers: 'Kate Bush',
  album: '',
  source: 'local',
  duration: '',
  duration_s: 0,
  cover_url: '',
  download_url: DEFAULT_TRACK,
  ext: 'mp3',
  file_size: '',
  file_size_bytes: 0,
  quality: '',
  lyric: '',
};

type PlayerState = 'idle' | 'playing' | 'paused';
type PlaylistSource = 'builtin' | 'search';

export function useMusicPlayer() {
  const [playerState, setPlayerState] = useState<PlayerState>('idle');
  const [currentSong, setCurrentSong] = useState<SongInfo | null>(null);
  const [queue, setQueue] = useState<SongInfo[]>([]);
  const [currentIndex, setCurrentIndex] = useState<number>(-1);
  const [playlistSource, setPlaylistSource] = useState<PlaylistSource>('builtin');
  const [volume, setVolumeState] = useState<number>(0.7);
  const [progress, setProgress] = useState<number>(0);
  const [duration, setDuration] = useState<number>(0);
  const [searchResults, setSearchResults] = useState<SongInfo[]>([]);
  const [error, setError] = useState('');

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const progressInterval = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const queueRef = useRef<SongInfo[]>([]);
  const currentIndexRef = useRef<number>(-1);
  const currentSongRef = useRef<SongInfo | null>(null);
  const playlistSourceRef = useRef<PlaylistSource>('builtin');
  const builtinQueueRef = useRef<SongInfo[]>([]);  // 始终保留内置歌单备份

  // 同步 state → ref
  useEffect(() => { queueRef.current = queue; }, [queue]);
  useEffect(() => { currentIndexRef.current = currentIndex; }, [currentIndex]);
  useEffect(() => { currentSongRef.current = currentSong; }, [currentSong]);
  useEffect(() => { playlistSourceRef.current = playlistSource; }, [playlistSource]);

  // ── Audio 初始化 + 用户手势解锁 ──
  useEffect(() => {
    if (!audioRef.current) {
      const audio = new Audio();
      audio.volume = volume;
      audio.preload = 'auto';

      audio.onended = () => {
        // 自动下一首（用 ref 避免闭包过期）
        const q = queueRef.current;
        const idx = currentIndexRef.current;
        if (q.length > 0 && idx < q.length - 1) {
          const fn = playIndexRef.current;
          if (fn) fn(idx + 1);
        } else {
          setPlayerState('idle');
        }
      };

      audio.onerror = () => {
        setError('音频加载失败，尝试下一首');
        const q = queueRef.current;
        const idx = currentIndexRef.current;
        if (q.length > 0 && idx < q.length - 1) {
          setTimeout(() => {
            const fn = playIndexRef.current;
            if (fn) fn(idx + 1);
          }, 1000);
        }
      };

      audio.onloadeddata = () => {
        setError('');
        setDuration(audio.duration || 0);
      };

      audio.onplay = () => setPlayerState('playing');
      audio.onpause = () => setPlayerState('paused');

      audio.ontimeupdate = () => {
        if (audio.duration) {
          setProgress(audio.currentTime / audio.duration);
        }
      };

      audioRef.current = audio;

      // 用户点击页面时解锁 AudioContext（浏览器自动播放策略）
      const unlockAudio = () => {
        if (audioRef.current) {
          // 创建并立即暂停一个极短的音频片段来解锁
          const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
          if (ctx.state === 'suspended') {
            ctx.resume();
          }
          ctx.close();
          // 尝试播放当前已有的 src（如果有）
          if (audioRef.current.src && audioRef.current.src !== window.location.href) {
            audioRef.current.play().catch(() => {});
          }
        }
        document.removeEventListener('click', unlockAudio);
        document.removeEventListener('touchstart', unlockAudio);
      };
      document.addEventListener('click', unlockAudio, { once: true });
      document.addEventListener('touchstart', unlockAudio, { once: true });
    }

    return () => {
      if (progressInterval.current) clearInterval(progressInterval.current);
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }
    };
  }, []);

  // ── 进度轮询（用于进度条显示） ──
  useEffect(() => {
    if (playerState === 'playing') {
      progressInterval.current = setInterval(() => {
        const a = audioRef.current;
        if (a && a.duration) {
          setProgress(a.currentTime / a.duration);
        }
      }, 250);
    } else {
      if (progressInterval.current) clearInterval(progressInterval.current);
    }
    return () => { if (progressInterval.current) clearInterval(progressInterval.current); };
  }, [playerState]);

  // useRef 包装 playIndex 解决自引用循环
  const playIndexRef = useRef<((i: number) => void) | null>(null);

  // ── 播放指定索引 ──
  const playIndex = useCallback((index: number) => {
    const audio = audioRef.current;
    const q = queueRef.current;
    if (!audio || index < 0 || index >= q.length) return;

    const song = q[index];
    setCurrentIndex(index);
    setCurrentSong(song);

    const url = song.download_url || DEFAULT_TRACK;
    audio.src = proxyUrl(url);
    audio.play().then(() => {
      setPlayerState('playing');
      setError('');
    }).catch((e) => {
      if (e.name === 'NotAllowedError') {
        setError('请点击页面任意位置后开始播放');
      } else if (q.length > 0 && index < q.length - 1) {
        setError('播放失败，切换下一首');
        setTimeout(() => {
          const fn = playIndexRef.current;
          if (fn) fn(index + 1);
        }, 500);
      } else {
        setError(`播放失败: ${e.message}`);
      }
      setPlayerState('paused');
    });
  }, []);

  // 将 playIndex 注入 ref（每次更新时同步）
  playIndexRef.current = playIndex;

  // ── 播放（默认/恢复） ──
  const play = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused && audio.src && !audio.src.startsWith('blob:') && !audio.src.includes('music.163.com')) {
      audio.play().then(() => setPlayerState('playing')).catch(() => { audio.src = ''; });
    } else if (!audio.src || audio.src === window.location.href) {
      audio.src = DEFAULT_TRACK;
      audio.play().then(() => {
        setPlayerState('playing');
        setCurrentSong(DEFAULT_SONG);
        setError('');
      }).catch(() => {
        setError('播放失败，请点击页面任意位置后再试');
      });
    }
  }, []);

  // ── 下一首 ──
  const next = useCallback(() => {
    const q = queueRef.current;
    const idx = currentIndexRef.current;
    if (q.length > 0 && idx < q.length - 1) {
      playIndex(idx + 1);
    } else if (q.length > 0) {
      playIndex(0);  // 循环到开头
    } else {
      play();
    }
  }, [playIndex, play]);

  // ── 上一首 ──
  const prev = useCallback(() => {
    const q = queueRef.current;
    const idx = currentIndexRef.current;
    if (q.length > 0 && idx > 0) {
      playIndex(idx - 1);
    } else if (q.length > 0) {
      playIndex(q.length - 1);  // 循环到末尾
    }
  }, [playIndex]);

  // ── 加载并播放 URL ──
  const playUrl = useCallback((url: string, song?: SongInfo) => {
    const audio = audioRef.current;
    if (!audio) return;

    audio.src = proxyUrl(url);
    audio.play().then(() => {
      setPlayerState('playing');
      setError('');
      if (song) {
        setCurrentSong(song);
      } else {
        setCurrentSong(null);
      }
    }).catch((e) => {
      if (e.name === 'NotAllowedError') {
        setError('请点击页面任意位置后开始播放');
      } else {
        audio.src = '';
        setError(`播放失败: ${e.message}`);
        setTimeout(() => next(), 500);
      }
    });
  }, [next]);

  // ── 暂停 ──
  const pause = useCallback(() => {
    audioRef.current?.pause();
    setPlayerState('paused');
  }, []);

  // ── 录音时自动降低音乐音量（ducking），避免音频设备冲突 ──
  const _volumeBeforeRec = useRef(0.7);
  const _wasPlayingBeforeRec = useRef(false);

  const duckForRecording = useCallback(() => {
    const audio = audioRef.current;
    if (audio) {
      _volumeBeforeRec.current = audio.volume;
      _wasPlayingBeforeRec.current = !audio.paused;
      // 将音量降到原来的 15%，让麦克风能清晰收音
      const duckedVolume = Math.max(0.05, audio.volume * 0.15);
      audio.volume = duckedVolume;
      console.log(`[音频Ducking] 录音开始，音量 ${_volumeBeforeRec.current.toFixed(2)} → ${duckedVolume.toFixed(2)}`);
    }
  }, []);

  const restoreVolumeAfterRecording = useCallback(() => {
    const audio = audioRef.current;
    if (audio && _wasPlayingBeforeRec.current) {
      audio.volume = _volumeBeforeRec.current;
      setVolumeState(_volumeBeforeRec.current);
      console.log(`[音频Ducking] 录音结束，恢复音量 → ${_volumeBeforeRec.current.toFixed(2)}`);
    }
  }, []);

  // ── 设置队列 ──
  const setQueueAndPlay = useCallback((songs: SongInfo[], startIndex: number = 0, source: PlaylistSource = 'builtin') => {
    queueRef.current = songs;
    currentIndexRef.current = startIndex;
    playlistSourceRef.current = source;
    setQueue(songs);
    setCurrentIndex(startIndex);
    setPlaylistSource(source);
    setSearchResults([]);
    if (songs.length > 0) {
      playIndex(startIndex);
    }
  }, [playIndex]);

  // ── 从 MusicControl 处理 ──
  const handleMusicControl = useCallback((mc: MusicControlData) => {
    setError('');

    switch (mc.action) {
      case 'play': {
        const isLocal = mc.source === 'local';
        // 保存内置歌单（首次从后端收到本地歌单时）
        if (isLocal && mc.songs && mc.songs.length > 0) {
          builtinQueueRef.current = mc.songs as SongInfo[];
        }
        // 有歌单 → 同步写 ref
        if (mc.songs && mc.songs.length > 0) {
          const songs = mc.songs as SongInfo[];
          queueRef.current = songs;
          setQueue(songs);
          playlistSourceRef.current = isLocal ? 'builtin' : 'search';
          setPlaylistSource(isLocal ? 'builtin' : 'search');
          if (mc.song_id) {
            const idx = songs.findIndex((s) => s.song_id === mc.song_id);
            if (idx >= 0) {
              currentIndexRef.current = idx;
              setCurrentIndex(idx);
            }
          }
        }
        if (mc.download_url && mc.song_name) {
          const song: SongInfo = {
            song_id: mc.song_id || '',
            song_name: mc.song_name || '',
            singers: mc.singers || '',
            album: mc.album || '',
            source: mc.source || '',
            duration: mc.duration || '',
            duration_s: mc.duration_s || 0,
            cover_url: mc.cover_url || '',
            download_url: mc.download_url,
            ext: mc.ext || 'mp3',
            file_size: '',
            file_size_bytes: 0,
            quality: '',
            lyric: '',
          };
          currentSongRef.current = song;
          setCurrentSong(song);
          playUrl(mc.download_url, song);
        } else if (mc.songs && mc.songs.length > 0) {
          playIndex(0);
        } else {
          console.warn('[音乐控制] 收到播放指令但没有歌曲数据，忽略');
        }
        break;
      }

      case 'pause':
        pause();
        break;

      case 'resume':
        play();
        break;

      case 'stop':
        pause();
        break;

      case 'next':
        // 在当前播放源内切歌，不重新搜索
        next();
        break;

      case 'prev':
        // 在当前播放源内切歌，不重新搜索
        prev();
        break;
    }
  }, [play, pause, next, prev, playUrl, playIndex]);

  const seek = useCallback((fraction: number) => {
    const audio = audioRef.current;
    if (audio && audio.duration) {
      audio.currentTime = fraction * audio.duration;
      setProgress(fraction);
    }
  }, []);

  const setVolume = useCallback((v: number) => {
    const vol = Math.max(0, Math.min(1, v));
    setVolumeState(vol);
    if (audioRef.current) audioRef.current.volume = vol;
  }, []);

  return {
    playerState,
    currentSong,
    queue,
    currentIndex,
    playlistSource,
    volume,
    progress,
    duration,
    searchResults,
    setSearchResults,
    error,
    play,
    pause,
    duckForRecording,
    restoreVolumeAfterRecording,
    next,
    prev,
    playUrl,
    seek,
    setVolume,
    setQueueAndPlay,
    handleMusicControl,
  };
}
