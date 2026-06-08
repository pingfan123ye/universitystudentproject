import { useState, useEffect, useRef, useCallback } from 'react';
import { Message, WSResponse, DeviceInfo, CacheEntry, RoutePath, TTSAudio, TimeState, SafetyConfirm, MusicControlData, Cet6Paper, Cet6SearchResult } from '../types';

export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected';

let msgCounter = 0;
function nextId() {
  return `msg-${++msgCounter}-${Date.now()}`;
}

// ── 会话持久化 ──
const SESSION_KEY = 'smart_speaker_session';
const MAX_PERSISTED_MESSAGES = 50;

function loadPersistedMessages(): Message[] {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (raw) {
      const data = JSON.parse(raw);
      if (Array.isArray(data)) {
        console.log(`[会话] 从本地恢复了 ${data.length} 条历史消息`);
        return data as Message[];
      }
    }
  } catch (e) {
    console.warn('[会话] 恢复历史失败:', e);
  }
  return [];
}

let _saveTimer: ReturnType<typeof setTimeout> | null = null;
function persistMessages(messages: Message[]): void {
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    try {
      const toSave = messages.slice(-MAX_PERSISTED_MESSAGES);
      localStorage.setItem(SESSION_KEY, JSON.stringify(toSave));
    } catch (e) {
      // localStorage 可能已满，静默失败
    }
  }, 2000);
}

function clearPersistedMessages(): void {
  try {
    localStorage.removeItem(SESSION_KEY);
  } catch { /* ignore */ }
}

export function useWebSocket() {
  const [status, setStatus] = useState<ConnectionStatus>('disconnected');
  const [messages, setMessages] = useState<Message[]>([]);
  const [devices, setDevices] = useState<Record<string, DeviceInfo>>({});
  const [cacheEntries, setCacheEntries] = useState<CacheEntry[]>([] as CacheEntry[]);
  const [lastPath, setLastPath] = useState<RoutePath>('unknown');
  const [routeLog, setRouteLog] = useState<Array<{ time: number; text: string; path: string; reason: string }>>([]);
  const musicActionIdRef = useRef(0);
  const [musicAction, setMusicAction] = useState<(MusicControlData & { id: number }) | null>(null);
  const [memoryEntries, setMemoryEntries] = useState<Array<{id:number;category:string;value:string;source:string;created_at:number}>>([]);
  const [pendingTask, setPendingTask] = useState<string | null>(null);
  const [engineConfig, setEngineConfig] = useState<Record<string, unknown>>({});
  const [safetyConfirm, setSafetyConfirm] = useState<SafetyConfirm | null>(null);
  const [transcriptionText, setTranscriptionText] = useState<string>('');
  const [ttsAudio, setTtsAudio] = useState<TTSAudio | null>(null);
  const [ttsFallbackText, setTtsFallbackText] = useState<string>('');
  const [musicSearchStatus, setMusicSearchStatus] = useState<{
    status: 'searching' | 'copyright_blocked' | 'not_found' | '';
    message?: string;
    query?: string;
  }>({ status: '' });
  const [cet6Paper, setCet6Paper] = useState<Cet6Paper | null>(null);
  const [cet6Answers, setCet6Answers] = useState<{ pdf_url: string } | null>(null);
  const [cet6SearchResults, setCet6SearchResults] = useState<Cet6SearchResult[]>([]);
  const [timeState, setTimeState] = useState<TimeState>({
    simulated: false, current_time: '', speed: 1, paused: false,
  });
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const currentAiMsgRef = useRef<string>('');
  // 唤醒词验证 Promise resolve（用于 sendVerifyWake → onmessage 异步响应）
  const wakeVerifyResolveRef = useRef<((ok: boolean) => void) | null>(null);
  // Refs 用于在 connect 闭包中获取最新的函数引用
  const fetchMemoryListRef = useRef<() => void>(() => {});
  const fetchCacheListRef = useRef<() => void>(() => {});

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setStatus('connecting');
    // 通过 Vite 代理连接后端 WebSocket，无需跨域
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.hostname}:${window.location.port}/api/ws`;

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus('connected');
      // 恢复上次会话的历史消息
      const persisted = loadPersistedMessages();
      if (persisted.length > 0) {
        setMessages([
          ...persisted,
          {
            id: nextId(),
            role: 'system',
            content: `已连接到服务器（恢复了 ${persisted.length} 条历史消息）`,
            timestamp: Date.now(),
          },
        ]);
        // 恢复后清除，避免反复叠加
        clearPersistedMessages();
      } else {
        setMessages((prev) => [
          ...prev,
          {
            id: nextId(),
            role: 'system',
            content: '已连接到服务器，可以开始对话',
            timestamp: Date.now(),
          },
        ]);
      }
    };

    ws.onmessage = (event) => {
      try {
        const data: WSResponse = JSON.parse(event.data);

        if (data.type === 'pong') return;

        if (data.type === 'music_control' && data.action) {
          musicActionIdRef.current += 1;
          setMusicAction({
            id: musicActionIdRef.current,
            action: data.action,
            playlist_name: data.playlist_name,
            song_id: data.song_id,
            song_name: data.song_name,
            singers: data.singers,
            album: data.album,
            source: data.source,
            duration: data.duration,
            duration_s: data.duration_s,
            cover_url: data.cover_url,
            download_url: data.download_url,
            ext: data.ext,
            songs: data.songs as MusicControlData['songs'],
          });
          return;
        }

        if (data.type === 'transcription_text') {
          setTranscriptionText(data.text || '');
          return;
        }

        if (data.type === 'stt_result') {
          setTranscriptionText(data.text || '');
          // 只在最终结果时自动发送（final=true），中间结果仅更新输入框预览
          if (data.final && data.text?.trim() && wsRef.current?.readyState === WebSocket.OPEN) {
            const text = data.text.trim();
            // ★ 过滤噪声：最短 2 个字符 + 非纯标点/语气词（单字噪声如"嗯""啊"直接丢弃）
            const isNoise = /^[，。！？、；：""''（）\s]+$/.test(text) || /^[嗯啊哦呃哎唉嘿喂哟]$/.test(text);
            if (text.length < 2 || isNoise) {
              console.log(`[STT] 🚫 噪声过滤: "${text}" (len=${text.length})`);
              setTranscriptionText('');
              return;
            }
            setTranscriptionText('');  // 立即清空输入框，避免旧文字残留
            // 添加用户消息到列表
            setMessages((prev) => [...prev, {
              id: nextId(), role: 'user' as const, content: text, timestamp: Date.now(),
            }]);
            // 发送到后端
            wsRef.current.send(JSON.stringify({ type: 'chat', text }));
          }
          return;
        }

        if (data.type === 'memory_list' && data.entries) {
          setMemoryEntries(data.entries as unknown as Array<{id:number;category:string;value:string;source:string;created_at:number}>);
          return;
        }

        if (data.type === 'memory_learned' || data.type === 'memory_deleted' || data.type === 'memory_cleared') {
          fetchMemoryListRef.current();
          return;
        }

        if (data.type === 'route') {
          setTranscriptionText('');  // STT 结果已发送，清空输入框
          const path = data.path as RoutePath || 'unknown';
          setLastPath(path);
          setRouteLog((prev) => [{
            time: Date.now(),
            text: data.reason || '',
            path: path,
            reason: data.reason || '',
          }, ...prev].slice(0, 30));
          return;
        }

        if (data.type === 'device_state' && data.devices) {
          setDevices(data.devices);
          return;
        }

        if (data.type === 'cache_list' && data.entries) {
          setCacheEntries(data.entries);
          return;
        }

        if (data.type === 'cache_learned') {
          fetchCacheListRef.current();
          return;
        }

        if (data.type === 'pending_task' && data.task) {
          setPendingTask(data.task);
          return;
        }

        if (data.type === 'cache_deleted') {
          fetchCacheListRef.current();
          return;
        }

        if (data.type === 'time_sync' && data.time) {
          setTimeState(data.time);
          return;
        }

        if (data.type === 'search_status') {
          const msg = data.message || '';
          setMessages((prev) => [
            ...prev,
            {
              id: `search-${Date.now()}`,
              role: 'system',
              content: msg,
              timestamp: Date.now(),
            },
          ]);
          return;
        }

        if (data.type === 'proactive_alert' && data.alert) {
          const alert = data.alert;
          setMessages((prev) => [
            ...prev,
            {
              id: `alert-${Date.now()}`,
              role: 'system',
              content: `🔔 ${alert.message || alert.reason || ''}`,
              timestamp: Date.now(),
            },
          ]);
          return;
        }

        if (data.type === 'alerts_suppressed') {
          const suppressed = data.suppressed;
          setMessages((prev) => [
            ...prev,
            {
              id: `alert-toggle-${Date.now()}`,
              role: 'system',
              content: suppressed ? '🔕 主动提醒已关闭' : '🔔 主动提醒已开启',
              timestamp: Date.now(),
            },
          ]);
          return;
        }

        if (data.type === 'engine_config' && data.config) {
          setEngineConfig(data.config);
          return;
        }

        if (data.type === 'safety_confirm') {
          setSafetyConfirm({
            command: data.command || '',
            risk: data.risk || 'high',
            reasons: data.reasons || [],
            message: data.message || '',
          });
          return;
        }

        if (data.type === 'tts_audio' && data.audio) {
          setTtsAudio({ audio: data.audio, text: data.text || '', path: data.path || '', seq: data.seq ?? 0 });
          return;
        }

        if (data.type === 'tts_failed') {
          setTtsFallbackText(data.text || '');
          return;
        }

        if (data.type === 'music_search_status') {
          setMusicSearchStatus({
            status: (data.status as 'searching' | 'copyright_blocked' | 'not_found') || '',
            message: data.message || '',
            query: data.query || '',
          });
          return;
        }

        if (data.type === 'cet6_paper') {
          setCet6Paper({
            paperId: data.paper_id || '',
            title: data.title || '',
            pdfUrl: data.pdf_url || '',
            hasAudio: data.has_audio || false,
            audioUrl: data.audio_url || '',
            hasAnswers: data.has_answers || false,
            answersUrl: data.answers_url || '',
          });
          setCet6Answers(null);  // 新试卷 → 清除旧答案
          return;
        }

        if (data.type === 'cet6_answers') {
          if (data.pdf_url) {
            setCet6Answers({ pdf_url: data.pdf_url });
          }
          return;
        }

        if (data.type === 'cet6_search_results') {
          setCet6SearchResults(data.results || []);
          return;
        }

        // ── 唤醒词验证响应 ──
        if (data.type === 'wake_verified') {
          if (wakeVerifyResolveRef.current) {
            wakeVerifyResolveRef.current(true);
            wakeVerifyResolveRef.current = null;
          }
          return;
        }

        if (data.type === 'wake_rejected') {
          if (wakeVerifyResolveRef.current) {
            wakeVerifyResolveRef.current(false);
            wakeVerifyResolveRef.current = null;
          }
          // ★ 诊断日志：显示 STT 实际转写内容
          const sttText = data.text || '';
          if (sttText) {
            console.log(`[唤醒词验证] 📝 STT 转写: "${sttText}"`);
          } else {
            console.log('[唤醒词验证] 📝 STT 转写: (空/静音)');
          }
          return;
        }

        // ── 附件消息：在聊天气泡中显示可点击的下载链接 ──
        if (data.type === 'chat_attachment') {
          const label = data.label || '📎 下载文件';
          const url = data.url || '';
          const ext = (url.match(/\.(\w+)(?:\?|$)/) || [])[1] || '文件';
          const attachmentHtml = url
            ? `<a href="${url}" target="_blank" rel="noopener noreferrer" style="color:var(--accent);text-decoration:underline;font-weight:500;">${label}（.${ext}）</a>`
            : label;
          currentAiMsgRef.current = '';
          setMessages((prev) => [...prev, {
            id: nextId(),
            role: 'ai',
            content: attachmentHtml,
            timestamp: Date.now(),
            path: 'cet6',
            isStreaming: false,
          }]);
          return;
        }

        if (data.type === 'token') {
          currentAiMsgRef.current += data.text || '';
          setMessages((prev) => {
            const lastMsg = prev[prev.length - 1];
            if (lastMsg?.role === 'ai' && lastMsg.isStreaming) {
              return [
                ...prev.slice(0, -1),
                { ...lastMsg, content: currentAiMsgRef.current },
              ];
            }
            const newMsg: Message = {
              id: nextId(),
              role: 'ai',
              content: currentAiMsgRef.current,
              timestamp: Date.now(),
              path: data.path,
              isStreaming: true,
            };
            return [...prev, newMsg];
          });
        }

        if (data.type === 'done') {
          // Reasonix 任务完成时清除待审批标记
          if (data.path === 'reasonix') {
            setPendingTask(null);
          }
          // 如果有 reply 字段（小爱/Reasonix 非流式路径），直接用
          if (data.reply) {
            currentAiMsgRef.current = '';
            setMessages((prev) => {
              const lastMsg = prev[prev.length - 1];
              if (lastMsg?.role === 'ai' && lastMsg.isStreaming) {
                return [...prev.slice(0, -1), { ...lastMsg, content: data.reply || '', isStreaming: false, path: data.path, model: data.model }];
              }
              return [...prev, {
                id: nextId(), role: 'ai', content: data.reply || '',
                timestamp: Date.now(), path: data.path, isStreaming: false, model: data.model,
              }];
            });
          } else {
            const finalContent = currentAiMsgRef.current;
            currentAiMsgRef.current = '';
            setMessages((prev) => {
              const lastMsg = prev[prev.length - 1];
              if (lastMsg?.role === 'ai' && lastMsg.isStreaming) {
                return [...prev.slice(0, -1), { ...lastMsg, content: finalContent, isStreaming: false, path: data.path, model: data.model }];
              }
              return prev;
            });
          }
        }

        if (data.type === 'error') {
          currentAiMsgRef.current = '';
          setMessages((prev) => [
            ...prev,
            {
              id: nextId(),
              role: 'system',
              content: `错误：${data.error || '未知错误'}`,
              timestamp: Date.now(),
            },
          ]);
        }

        if (data.type === 'cancelled') {
          // 唤醒词打断：移除最后一个未完成的流式 AI 消息
          currentAiMsgRef.current = '';
          setMessages((prev) => {
            const lastMsg = prev[prev.length - 1];
            if (lastMsg?.role === 'ai' && lastMsg.isStreaming) {
              return prev.slice(0, -1);  // 移除未完成的消息
            }
            return prev;
          });
          return;
        }

        if (data.type === 'chat_error') {
          setMessages((prev) => [
            ...prev,
            {
              id: nextId(),
              role: 'system',
              content: data.message || '操作失败',
              timestamp: Date.now(),
            },
          ]);
        }
      } catch {
        // 忽略解析错误
      }
    };

    ws.onclose = () => {
      setStatus('disconnected');
      wsRef.current = null;
      // 5 秒后自动重连
      reconnectTimer.current = setTimeout(connect, 5000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  // ── 会话持久化：消息变化时自动保存 ──
  useEffect(() => {
    if (messages.length > 0 && status === 'connected') {
      persistMessages(messages);
    }
  }, [messages, status]);

  const sendMessage = useCallback((text: string) => {
    if (!text.trim()) return;

    const userMsg: Message = {
      id: nextId(),
      role: 'user',
      content: text,
      timestamp: Date.now(),
    };
    setMessages((prev) => [...prev, userMsg]);

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'chat', text }));
    } else {
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: 'system',
          content: '未连接到服务器，请稍后重试',
          timestamp: Date.now(),
        },
      ]);
    }
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
    currentAiMsgRef.current = '';
    clearPersistedMessages();
  }, []);

  const fetchCacheList = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'list_cache' }));
    }
  }, []);

  const deleteCache = useCallback((id: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'delete_cache', id }));
    }
  }, []);

  const fetchMemoryList = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'list_memories' }));
    }
  }, []);

  const deleteMemory = useCallback((id: number) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'delete_memory', id }));
    }
  }, []);

  const clearMemories = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'clear_memories' }));
    }
  }, []);

  const consumeTts = useCallback(() => {
    const audio = ttsAudio;
    setTtsAudio(null);
    return audio;
  }, [ttsAudio]);

  // 时间控制发送
  // 安全发送：仅在 WebSocket 已连接时发送
  const safeSend = useCallback((data: object) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data));
    } else {
      console.warn('WebSocket 未连接，消息已丢弃:', JSON.stringify(data).slice(0, 60));
    }
  }, []);

  const setTime = useCallback((hour: number, minute: number) => {
    safeSend({ type: 'set_time', hour, minute });
  }, [safeSend]);
  const setTimeSpeed = useCallback((speed: number) => {
    safeSend({ type: 'set_time_speed', speed });
  }, [safeSend]);
  const toggleTimePause = useCallback(() => {
    safeSend({ type: 'toggle_time_pause' });
  }, [safeSend]);
  const toggleTimeSim = useCallback(() => {
    safeSend({ type: 'toggle_time_simulation', enabled: !timeState.simulated });
  }, [safeSend, timeState.simulated]);

  const toggleSuppressAlerts = useCallback((suppressed: boolean) => {
    safeSend({ type: 'toggle_suppress_alerts', suppressed });
  }, [safeSend]);

  // B-3: 发送完整音频（录音结束后一次发送）
  const sendCompleteAudio = useCallback((audioBase64: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'audio_stream', audio: audioBase64, final: true }));
    }
  }, []);

  // ★ 唤醒词二次验证：发送 2 秒验证音频到后端 STT 检查是否含"小智"
  const sendVerifyWake = useCallback((audioBase64: string): Promise<boolean> => {
    return new Promise((resolve) => {
      // 清除旧的 pending resolve（防止前一次超时残留）
      if (wakeVerifyResolveRef.current) {
        wakeVerifyResolveRef.current(false);
        wakeVerifyResolveRef.current = null;
      }
      wakeVerifyResolveRef.current = resolve;
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'verify_wake', audio: audioBase64 }));
      } else {
        wakeVerifyResolveRef.current = null;
        resolve(false);
      }
      // 超时保护：3 秒后自动拒绝（防止 Promise 永远 pending）
      setTimeout(() => {
        if (wakeVerifyResolveRef.current === resolve) {
          wakeVerifyResolveRef.current = null;
          resolve(false);
        }
      }, 3000);
    });
  }, []);

  // 唤醒词打断：取消正在进行的 LLM 流式生成
  const sendCancel = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'cancel' }));
    }
  }, []);

  const sendCet6Download = useCallback((paperId: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'cet6_download_paper', paper_id: paperId }));
    }
  }, []);

  const sendCet6Close = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'cet6_close' }));
    }
  }, []);

  const safetyReply = useCallback((accept: boolean) => {
    setSafetyConfirm(null);
    safeSend({ type: 'safety_reply', accept });
  }, [safeSend]);

  const fetchEngineConfig = useCallback(() => {
    safeSend({ type: 'get_config' });
  }, [safeSend]);
  const setEngineConfigItem = useCallback((key: string, value: unknown) => {
    safeSend({ type: 'set_config', key, value });
  }, [safeSend]);
  const resetEngineConfig = useCallback(() => {
    safeSend({ type: 'reset_config' });
  }, [safeSend]);

  const resetConversation = useCallback(() => {
    currentAiMsgRef.current = '';
    setMessages([]);
    clearPersistedMessages();
    safeSend({ type: 'reset' });
  }, [safeSend]);

  // 同步 refs，确保 connect 闭包中始终拿到最新的函数引用
  fetchMemoryListRef.current = fetchMemoryList;
  fetchCacheListRef.current = fetchCacheList;

  return {
    status, messages, devices, cacheEntries, lastPath, routeLog, musicAction,
    memoryEntries, pendingTask, ttsAudio, consumeTts, transcriptionText, ttsFallbackText,
    safetyConfirm, safetyReply,
    engineConfig, fetchEngineConfig, setEngineConfigItem, resetEngineConfig,
    timeState, setTime, setTimeSpeed, toggleTimePause, toggleTimeSim, toggleSuppressAlerts,
    sendMessage, clearMessages, fetchCacheList, deleteCache,
    fetchMemoryList, deleteMemory, clearMemories,
    musicSearchStatus,
    cet6Paper, setCet6Paper, cet6Answers, setCet6Answers,
    cet6SearchResults, sendCet6Download,
    sendCompleteAudio, sendVerifyWake, sendCancel, sendCet6Close, resetConversation,
  };
}
