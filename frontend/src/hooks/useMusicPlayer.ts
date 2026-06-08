import { useState, useRef, useCallback, useEffect } from 'react';
import { SongInfo, MusicControlData } from '../types';

// 将第三方音乐 URL 转为后端代理（解决跨域播放）
// 对本地路径中的中文字符做 URL 编码，避免浏览器 Audio 加载失败
function proxyUrl(url: string): string {
  if (!url || url.startsWith('blob:') || url.startsWith('data:')) return url;
  if (url.startsWith('/')) {
    // 对路径中的非 ASCII 字符进行编码（如 /music/playlists/轻音乐/song.mp3）
    return url.split('/').map(segment => {
      try {
        // 如果 segment 还没被编码过，就编码；已编码的跳过
        return decodeURIComponent(segment) === segment ? encodeURIComponent(segment) : segment;
      } catch {
        return encodeURIComponent(segment);
      }
    }).join('/');
  }
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

type PlayerState = 'idle' | 'searching' | 'playing' | 'paused';
type PlaylistSource = 'builtin' | 'search';

export function useMusicPlayer() {
  const [playerState, setPlayerState] = useState<PlayerState>('idle');
  const [currentSong, setCurrentSong] = useState<SongInfo | null>(null);
  const [queue, setQueue] = useState<SongInfo[]>([]);
  const [currentIndex, setCurrentIndex] = useState<number>(-1);
  const [playlistSource, setPlaylistSource] = useState<PlaylistSource>('builtin');
  const [currentPlaylist, setCurrentPlaylist] = useState<string | null>(null);
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
  const currentPlaylistRef = useRef<string | null>(null);

  // 同步 state → ref
  useEffect(() => { queueRef.current = queue; }, [queue]);
  useEffect(() => { currentIndexRef.current = currentIndex; }, [currentIndex]);
  useEffect(() => { currentSongRef.current = currentSong; }, [currentSong]);
  useEffect(() => { playlistSourceRef.current = playlistSource; }, [playlistSource]);
  useEffect(() => { currentPlaylistRef.current = currentPlaylist; }, [currentPlaylist]);

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
        const playlist = currentPlaylistRef.current;

        if (playlist && q.length > 0) {
          // 歌单模式：播完循环（重新打乱顺序）
          if (idx < q.length - 1) {
            const fn = playIndexRef.current;
            if (fn) fn(idx + 1);
          } else {
            // 歌单播完 → 重新打乱 → 从头播放
            const reshuffled = [...q].sort(() => Math.random() - 0.5);
            queueRef.current = reshuffled;
            setQueue(reshuffled);
            currentIndexRef.current = 0;
            setCurrentIndex(0);
            const fn = playIndexRef.current;
            if (fn) fn(0);
          }
        } else if (q.length > 0 && idx < q.length - 1) {
          // 普通模式：自动下一首
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
        const playlist = currentPlaylistRef.current;

        if (playlist && q.length > 0) {
          // 歌单模式：跳到下一首（或重新打乱循环）
          if (idx < q.length - 1) {
            setTimeout(() => {
              const fn = playIndexRef.current;
              if (fn) fn(idx + 1);
            }, 800);
          } else {
            // 最后一首失败 → 重新打乱 → 从头播放
            const reshuffled = [...q].sort(() => Math.random() - 0.5);
            queueRef.current = reshuffled;
            setQueue(reshuffled);
            currentIndexRef.current = 0;
            setCurrentIndex(0);
            setTimeout(() => {
              const fn = playIndexRef.current;
              if (fn) fn(0);
            }, 800);
          }
        } else if (q.length > 0 && idx < q.length - 1) {
          setTimeout(() => {
            const fn = playIndexRef.current;
            if (fn) fn(idx + 1);
          }, 800);
        } else {
          setPlayerState('idle');
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

    // 网易云歌曲无 URL → 跳过，尝试下一首（后端已预取前5首，这里是安全兜底）
    if (!song.download_url && song.source === 'netease') {
      console.warn(`[音乐播放] 网易云歌曲无 URL，跳过: "${song.song_name}"`);
      if (index < q.length - 1) {
        setTimeout(() => {
          const fn = playIndexRef.current;
          if (fn) fn(index + 1);
        }, 0);
      } else {
        setError('队列中的歌曲暂无播放链接');
        setPlayerState('idle');
      }
      return;
    }

    setCurrentIndex(index);
    setCurrentSong(song);

    const url = song.download_url || DEFAULT_TRACK;
    const proxied = proxyUrl(url);
    console.log(`[音乐播放] 索引=${index} 歌名="${song.song_name}" 原始URL="${url}" 代理URL="${proxied}"`);
    audio.src = proxied;
    audio.play().then(() => {
      setPlayerState('playing');
      setError('');
      console.log(`[音乐播放] 播放成功: "${song.song_name}"`);
    }).catch((e) => {
      console.error(`[音乐播放] 播放失败: "${song.song_name}"`, e.name, e.message);
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
    const playlist = currentPlaylistRef.current;

    if (q.length > 0 && idx < q.length - 1) {
      playIndex(idx + 1);
    } else if (q.length > 0 && playlist) {
      // 歌单最后一首切歌 → 重新打乱 → 从头播放（与 onended 行为统一）
      const reshuffled = [...q].sort(() => Math.random() - 0.5);
      queueRef.current = reshuffled;
      setQueue(reshuffled);
      currentIndexRef.current = 0;
      setCurrentIndex(0);
      playIndex(0);
    } else if (q.length > 0) {
      playIndex(0);  // 普通模式循环到开头
    }
    // 队列为空时 next 无害，不播默认曲目
  }, [playIndex]);

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

  // ── 录音时降低音乐音量（ducking），避免扬声器内容被麦克风拾取 ──
  const _volumeBeforeRec = useRef(0.7);
  const _wasPlayingBeforeRec = useRef(false);
  const _isDuckedRef = useRef(false);  // ★ 防止重复保存原始音量（Light Duck → Deep Duck 会覆盖）

  const duckForRecording = useCallback((factor: number = 0.08) => {
    const audio = audioRef.current;
    if (audio && !audio.paused) {
      // ★ 只在首次进入 Duck 状态时保存原始音量，防止 Light Duck(0.35) 保存 0.70
      //   后被 Deep Duck(0.08) 覆盖为 0.245，导致恢复时只能恢复到 0.245
      if (!_isDuckedRef.current) {
        _volumeBeforeRec.current = audio.volume;
        _wasPlayingBeforeRec.current = true;
        _isDuckedRef.current = true;
      }
      const duckedVolume = Math.max(0.03, _volumeBeforeRec.current * factor);
      audio.volume = duckedVolume;
      console.log(`[音频Ducking] 音量 ${_volumeBeforeRec.current.toFixed(2)} → ${duckedVolume.toFixed(2)} (factor=${factor})`);
    }
  }, []);

  const restoreVolumeAfterRecording = useCallback(() => {
    const audio = audioRef.current;
    if (audio && _wasPlayingBeforeRec.current) {
      audio.volume = _volumeBeforeRec.current;
      setVolumeState(_volumeBeforeRec.current);
      _isDuckedRef.current = false;  // ★ 重置标记，下次 Duck 重新保存原始音量
      _wasPlayingBeforeRec.current = false;
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
        // ── 歌单模式 ──
        if (mc.playlist_name && mc.songs && mc.songs.length > 0) {
          const songs = mc.songs as SongInfo[];
          currentPlaylistRef.current = mc.playlist_name;
          setCurrentPlaylist(mc.playlist_name);
          queueRef.current = songs;
          setQueue(songs);
          playlistSourceRef.current = 'builtin';
          setPlaylistSource('builtin');
          builtinQueueRef.current = songs;
          setSearchResults([]);
          // 找到要播放的第一首
          if (mc.song_id) {
            const idx = songs.findIndex((s) => s.song_id === mc.song_id);
            if (idx >= 0) {
              currentIndexRef.current = idx;
              setCurrentIndex(idx);
              playIndex(idx);
            } else {
              currentIndexRef.current = 0;
              setCurrentIndex(0);
              playIndex(0);
            }
          } else {
            currentIndexRef.current = 0;
            setCurrentIndex(0);
            playIndex(0);
          }
          break;
        }

        // ── 非歌单播放：清除歌单状态 ──
        currentPlaylistRef.current = null;
        setCurrentPlaylist(null);

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
    setPlayerState,
    currentSong,
    currentPlaylist,
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
