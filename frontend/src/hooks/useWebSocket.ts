import { useState, useEffect, useRef, useCallback } from 'react';
import { Message, WSResponse, DeviceInfo, CacheEntry, RoutePath } from '../types';

export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected';

let msgCounter = 0;
function nextId() {
  return `msg-${++msgCounter}-${Date.now()}`;
}

export function useWebSocket() {
  const [status, setStatus] = useState<ConnectionStatus>('disconnected');
  const [messages, setMessages] = useState<Message[]>([]);
  const [devices, setDevices] = useState<Record<string, DeviceInfo>>({});
  const [cacheEntries, setCacheEntries] = useState<CacheEntry[]>([]);
  const [lastPath, setLastPath] = useState<RoutePath>('unknown');
  const [routeLog, setRouteLog] = useState<Array<{ time: number; text: string; path: string; reason: string }>>([]);
  const [musicAction, setMusicAction] = useState<string | null>(null);
  const [memoryEntries, setMemoryEntries] = useState<Array<{id:number;category:string;value:string;source:string;created_at:number}>>([]);
  const [pendingTask, setPendingTask] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const currentAiMsgRef = useRef<string>('');

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
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: 'system',
          content: '已连接到服务器，可以开始对话',
          timestamp: Date.now(),
        },
      ]);
    };

    ws.onmessage = (event) => {
      try {
        const data: WSResponse = JSON.parse(event.data);

        if (data.type === 'pong') return;

        if (data.type === 'music_control' && data.action) {
          setMusicAction(data.action);
          return;
        }

        if (data.type === 'memory_list' && data.entries) {
          setMemoryEntries(data.entries);
          return;
        }

        if (data.type === 'memory_learned' || data.type === 'memory_deleted' || data.type === 'memory_cleared') {
          fetchMemoryList();
          return;
        }

        if (data.type === 'route') {
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
          fetchCacheList();
          return;
        }

        if (data.type === 'pending_task' && data.task) {
          setPendingTask(data.task);
          return;
        }

        if (data.type === 'cache_deleted') {
          fetchCacheList();
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
                return [...prev.slice(0, -1), { ...lastMsg, content: data.reply, isStreaming: false, path: data.path }];
              }
              return [...prev, {
                id: nextId(), role: 'ai', content: data.reply || '',
                timestamp: Date.now(), path: data.path, isStreaming: false,
              }];
            });
          } else {
            const finalContent = currentAiMsgRef.current;
            currentAiMsgRef.current = '';
            setMessages((prev) => {
              const lastMsg = prev[prev.length - 1];
              if (lastMsg?.role === 'ai' && lastMsg.isStreaming) {
                return [...prev.slice(0, -1), { ...lastMsg, content: finalContent, isStreaming: false, path: data.path }];
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

  return { status, messages, devices, cacheEntries, lastPath, routeLog, musicAction, memoryEntries, pendingTask, sendMessage, clearMessages, fetchCacheList, deleteCache, fetchMemoryList, deleteMemory, clearMemories };
}
