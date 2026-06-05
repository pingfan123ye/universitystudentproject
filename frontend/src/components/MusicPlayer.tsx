import { useState, useCallback, useRef } from 'react';
import { SongInfo } from '../types';

interface MusicPlayerProps {
  playerState: 'idle' | 'searching' | 'playing' | 'paused';
  currentSong: SongInfo | null;
  currentPlaylist?: string | null;
  queue: SongInfo[];
  currentIndex: number;
  volume: number;
  progress: number;
  error: string;
  searchResults: SongInfo[];
  onPlay: () => void;
  onPause: () => void;
  onNext: () => void;
  onPrev: () => void;
  onSeek: (fraction: number) => void;
  onSetVolume: (v: number) => void;
  onSetQueueAndPlay: (songs: SongInfo[], startIndex: number) => void;
  onSearchResults: (results: SongInfo[]) => void;
}

export default function MusicPlayer({
  playerState, currentSong, currentPlaylist, queue, currentIndex,
  volume, progress, error, searchResults,
  onPlay, onPause, onNext, onPrev,
  onSeek, onSetVolume, onSetQueueAndPlay, onSearchResults,
}: MusicPlayerProps) {
  const [expanded, setExpanded] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [showQueue, setShowQueue] = useState(false);
  const volumeTrackRef = useRef<HTMLDivElement>(null);

  // 音量滑块交互：点击轨道跳转
  const handleVolumeTrackClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const track = volumeTrackRef.current;
    if (!track) return;
    const rect = track.getBoundingClientRect();
    const fraction = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    onSetVolume(fraction);
  }, [onSetVolume]);

  // 音量滑块拖拽
  const handleVolumeDrag = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    const track = volumeTrackRef.current;
    if (!track) return;
    const onMove = (ev: MouseEvent) => {
      const rect = track.getBoundingClientRect();
      const fraction = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
      onSetVolume(fraction);
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [onSetVolume]);

  // 搜索歌曲
  const handleSearch = useCallback(async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const resp = await fetch(`/api/music/search?q=${encodeURIComponent(searchQuery)}`);
      const data = await resp.json();
      if (data.songs && data.songs.length > 0) {
        onSearchResults(data.songs);
      }
    } catch (e) {
      console.error('音乐搜索失败:', e);
    } finally {
      setSearching(false);
    }
  }, [searchQuery, onSearchResults]);

  // 播放搜索结果
  const handlePlaySong = useCallback((_song: SongInfo, index: number) => {
    // 直接播放选中歌曲，并把搜索结果设为队列
    const allSongs = searchResults;
    onSetQueueAndPlay(allSongs, index);
    setShowQueue(true);
    setExpanded(false);
  }, [searchResults, onSetQueueAndPlay]);

  // 播放/暂停切换
  const togglePlay = useCallback(() => {
    if (playerState === 'playing') {
      onPause();
    } else {
      onPlay();
    }
  }, [playerState, onPlay, onPause]);


  // 当空闲且无歌曲/队列/搜索结果时隐藏播放器
  if (playerState === 'idle' && searchResults.length === 0 && currentSong === null && queue.length === 0) {
    return null;
  }

  const songName = currentSong?.song_name || '';
  const singerName = currentSong?.singers || '';
  const coverUrl = currentSong?.cover_url || '';

  return (
    <div style={{
      background: 'var(--bg-elevated)',
      borderTop: '1px solid var(--border)',
      padding: expanded ? '12px 14px' : '6px 10px',
      transition: 'all 0.2s',
      flexShrink: 0,
      position: 'relative',
    }}>
      {/* 主控制条 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '4px', maxWidth: '900px', margin: '0 auto' }}>
        {/* 封面 */}
        <div style={{
          width: '36px', height: '36px', borderRadius: '6px',
          background: coverUrl ? `url(${coverUrl}) center/cover` : 'var(--bg-input)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '14px', flexShrink: 0,
        }}>
          {!coverUrl && (playerState === 'playing' ? '▶' : '🎵')}
        </div>

        {/* 歌曲信息 */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {playerState === 'searching' ? (
            <>
              <div style={{
                fontSize: '12px', fontWeight: 600, whiteSpace: 'nowrap',
                overflow: 'hidden', textOverflow: 'ellipsis', color: 'var(--accent)',
              }}>
                🔍 正在搜索歌曲...
              </div>
              <div style={{
                fontSize: '10px', color: 'var(--text-muted)', whiteSpace: 'nowrap',
                overflow: 'hidden', textOverflow: 'ellipsis',
              }}>
                请稍候
              </div>
            </>
          ) : currentPlaylist ? (
            <>
              {/* 歌单名 — 优先显示 */}
              <div style={{
                fontSize: '13px', fontWeight: 700, whiteSpace: 'nowrap',
                overflow: 'hidden', textOverflow: 'ellipsis',
                color: 'var(--accent)',
              }}>
                📋 {currentPlaylist}
              </div>
              {/* 当前曲名 */}
              <div style={{
                fontSize: '11px', color: 'var(--text-muted)', whiteSpace: 'nowrap',
                overflow: 'hidden', textOverflow: 'ellipsis',
              }}>
                {songName || '未选择歌曲'}
              </div>
              {/* 进度信息 */}
              <div style={{
                fontSize: '9px', color: 'var(--text-muted)', whiteSpace: 'nowrap',
              }}>
                {queue.length > 0 ? `${currentIndex + 1}/${queue.length} · 循环` : ''}
              </div>
            </>
          ) : (
            <>
              <div style={{
                fontSize: '12px', fontWeight: 600, whiteSpace: 'nowrap',
                overflow: 'hidden', textOverflow: 'ellipsis',
              }}>
                {songName || '未选择歌曲'}
              </div>
              <div style={{
                fontSize: '10px', color: 'var(--text-muted)', whiteSpace: 'nowrap',
                overflow: 'hidden', textOverflow: 'ellipsis',
              }}>
                {singerName || (queue.length > 0 ? `${queue.length} 首歌曲` : '')}
              </div>
            </>
          )}
        </div>

        {/* 错误提示 */}
        {error && (
          <div style={{ fontSize: '12px', color: '#e74c3c', fontWeight: 500, maxWidth: '180px', overflow: 'hidden', background: 'rgba(231,76,60,0.08)', padding: '2px 6px', borderRadius: '4px' }}>
            ⚠ {error}
          </div>
        )}

        {/* 控制按钮 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '2px' }}>
          <button onClick={onPrev} style={btnStyle} title="上一首">⏮</button>
          <button onClick={togglePlay} style={{
            ...btnStyle,
            width: '32px', height: '32px', borderRadius: '50%',
            background: playerState === 'searching' ? 'var(--text-muted)' : 'var(--accent)',
            color: '#fff',
            fontSize: '14px',
            cursor: playerState === 'searching' ? 'default' : 'pointer',
          }}>
            {playerState === 'searching' ? '⏳' : playerState === 'playing' ? '⏸' : '▶'}
          </button>
          <button onClick={onNext} style={btnStyle} title="下一首">⏭</button>
        </div>

        {/* 音量 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', width: '80px', flexShrink: 0 }}>
          <span style={{ fontSize: '12px', color: 'var(--text-muted)', flexShrink: 0, cursor: 'pointer', userSelect: 'none' }}
            onClick={() => onSetVolume(volume === 0 ? 0.7 : 0)}
            title={volume === 0 ? '取消静音' : '静音'}
          >
            {volume === 0 ? '🔇' : volume < 0.5 ? '🔉' : '🔊'}
          </span>
          {/* 自定义音量滑块 — 规整统一的跨浏览器外观 */}
          <div
            ref={volumeTrackRef}
            onClick={handleVolumeTrackClick}
            style={{
              flex: 1, minWidth: 0, height: '22px',
              display: 'flex', alignItems: 'center',
              cursor: 'pointer',
            }}
          >
            <div style={{
              position: 'relative', width: '100%', height: '4px',
              borderRadius: '2px', background: 'var(--border)',
              overflow: 'visible',
            }}>
              {/* 已填充部分 */}
              <div style={{
                position: 'absolute', top: 0, left: 0, height: '100%',
                width: `${volume * 100}%`,
                borderRadius: '2px',
                background: 'var(--accent)',
                transition: 'width 0.05s linear',
              }} />
              {/* 拖拽圆点 */}
              <div
                onMouseDown={handleVolumeDrag}
                style={{
                  position: 'absolute', top: '50%',
                  left: `${volume * 100}%`,
                  transform: 'translate(-50%, -50%)',
                  width: '12px', height: '12px',
                  borderRadius: '50%',
                  background: 'var(--accent)',
                  border: '2px solid var(--bg-elevated)',
                  boxShadow: '0 1px 3px rgba(0,0,0,0.25)',
                  cursor: 'grab',
                  transition: 'left 0.05s linear',
                }}
              />
            </div>
          </div>
        </div>

        {/* 展开按钮 */}
        <button
          onClick={() => setExpanded(!expanded)}
          style={{
            ...btnStyle, fontSize: '10px',
            color: expanded ? 'var(--accent)' : 'var(--text-muted)',
          }}
          title="搜索/队列"
        >
          {expanded ? '▼' : '▲'}
        </button>
      </div>

      {/* 进度条 */}
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0, height: '2px',
        background: 'var(--border)', cursor: 'pointer',
      }}
        onClick={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          onSeek((e.clientX - rect.left) / rect.width);
        }}
      >
        <div style={{
          height: '100%', width: `${progress * 100}%`,
          background: 'var(--accent)',
          transition: 'width 0.25s linear',
        }} />
      </div>

      {/* 展开面板：搜索 + 队列 */}
      {expanded && (
        <div style={{
          marginTop: '12px', maxHeight: '300px', overflow: 'auto',
          display: 'flex', gap: '12px', flexDirection: 'column',
        }}>
          {/* 搜索区 */}
          <div style={{ display: 'flex', gap: '6px' }}>
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              placeholder="搜索歌曲（歌名/歌手）..."
              style={{
                flex: 1, padding: '6px 10px', borderRadius: '6px', border: '1px solid var(--border)',
                background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '12px', outline: 'none',
              }}
            />
            <button
              onClick={handleSearch}
              disabled={searching}
              style={{
                padding: '6px 12px', borderRadius: '6px', border: 'none',
                background: 'var(--accent)', color: '#fff', fontSize: '11px', cursor: 'pointer',
                opacity: searching ? 0.6 : 1,
              }}
            >
              {searching ? '搜索中...' : '搜索'}
            </button>
          </div>

          {/* 搜索结果 */}
          {searchResults.length > 0 && (
            <div>
              <div style={{
                fontSize: '10px', fontWeight: 600, textTransform: 'uppercase',
                letterSpacing: '0.5px', color: 'var(--text-muted)', marginBottom: '6px',
              }}>
                搜索结果 ({searchResults.length})
              </div>
              {searchResults.slice(0, 20).map((song, i) => (
                <div
                  key={`${song.source}-${song.song_id}-${i}`}
                  onClick={() => handlePlaySong(song, i)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '6px 8px', borderRadius: '6px', cursor: 'pointer',
                    background: currentSong?.song_id === song.song_id ? 'var(--bg-active, rgba(255,255,255,0.05))' : 'transparent',
                    transition: 'background 0.15s',
                  }}
                  onMouseEnter={(e) => e.currentTarget.style.background = 'var(--bg-hover, rgba(255,255,255,0.08))'}
                  onMouseLeave={(e) => e.currentTarget.style.background = currentSong?.song_id === song.song_id ? 'var(--bg-active, rgba(255,255,255,0.05))' : 'transparent'}
                >
                  <div style={{
                    width: '28px', height: '28px', borderRadius: '4px', flexShrink: 0,
                    background: song.cover_url ? `url(${song.cover_url}) center/cover` : 'var(--bg-input)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '10px',
                  }}>
                    {!song.cover_url && '🎵'}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: '12px', fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {song.song_name}
                    </div>
                    <div style={{ fontSize: '10px', color: 'var(--text-muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {song.singers} · {song.source.replace('MusicClient', '')} · {song.duration}
                    </div>
                  </div>
                  <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>
                    {song.ext?.toUpperCase()}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* 播放队列 */}
          {showQueue && queue.length > 0 && (
            <div>
              <div style={{
                fontSize: '10px', fontWeight: 600, textTransform: 'uppercase',
                letterSpacing: '0.5px', color: 'var(--text-muted)', marginBottom: '6px',
              }}>
                {currentPlaylist
                  ? `📋 ${currentPlaylist} (${queue.length}) · 随机循环`
                  : `播放队列 (${queue.length})`}
              </div>
              {queue.map((song, i) => (
                <div
                  key={`q-${song.source}-${song.song_id}-${i}`}
                  onClick={() => onSetQueueAndPlay(queue, i)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '6px',
                    padding: '4px 8px', borderRadius: '4px', cursor: 'pointer', fontSize: '11px',
                    background: i === currentIndex ? 'var(--accent-glow, rgba(99,102,241,0.15))' : 'transparent',
                    color: i === currentIndex ? 'var(--accent)' : 'var(--text-primary)',
                  }}
                >
                  <span>{i === currentIndex ? '▶' : `${i + 1}.`}</span>
                  <span style={{ flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {song.song_name}
                  </span>
                  <span style={{ color: 'var(--text-muted)', fontSize: '10px' }}>
                    {song.singers}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* 空状态 */}
          {searchResults.length === 0 && !showQueue && (
            <div style={{ textAlign: 'center', padding: '20px', color: 'var(--text-muted)', fontSize: '12px' }}>
              🔍 搜索你喜欢的歌曲，或对小智说"播放[歌名]"
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  cursor: 'pointer',
  fontSize: '14px',
  padding: '4px 6px',
  borderRadius: '4px',
  color: 'var(--text-secondary)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  lineHeight: 1,
};
