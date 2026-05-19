export type MessageRole = 'user' | 'ai' | 'system';
export type RoutePath = 'xiaoai' | 'llm' | 'reasonix' | 'cache' | 'unknown';

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: number;
  path?: RoutePath;
  isStreaming?: boolean;
}

export interface WSMessage {
  type: 'chat' | 'ping';
  text?: string;
}

export interface PendingTaskInfo {
  task: string;
  message?: string;
}

export interface CacheEntry {
  id: string;
  normalized_text: string;
  original_text: string;
  reply: string;
  hit_count: number;
  created_at: number;
  last_hit_at: number;
}

export interface WSResponse {
  type: 'token' | 'done' | 'error' | 'pong' | 'route' | 'device_state'
    | 'cache_list' | 'cache_learned' | 'cache_deleted'
    | 'pending_task' | 'music_control'
    | 'memory_list' | 'memory_learned' | 'memory_deleted' | 'memory_cleared';
  text?: string;
  path?: RoutePath;
  reply?: string;
  reason?: string;
  error?: string;
  action?: string;
  devices?: Record<string, DeviceInfo>;
  entries?: CacheEntry[];
  id?: string;
  message?: string;
  task?: string;
}

export interface DeviceInfo {
  name: string;
  type: 'light' | 'curtain' | 'heater' | 'ac' | 'fan' | 'tv';
  room: string;
  status: string;
  properties: Record<string, unknown>;
}
