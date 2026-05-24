export type MessageRole = 'user' | 'ai' | 'system';
export type RoutePath = 'xiaoai' | 'llm' | 'reasonix' | 'cache' | 'unknown';

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: number;
  path?: RoutePath;
  isStreaming?: boolean;
  model?: string;
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

export interface SafetyConfirm {
  command: string;
  risk: string;
  reasons: string[];
  message: string;
}

export interface WSResponse {
  type: 'token' | 'done' | 'error' | 'pong' | 'route' | 'device_state'
    | 'cache_list' | 'cache_learned' | 'cache_deleted'
    | 'pending_task' | 'music_control'
    | 'memory_list' | 'memory_learned' | 'memory_deleted' | 'memory_cleared'
    | 'tts_audio' | 'time_sync'
    | 'proactive_alert' | 'alerts_suppressed'
    | 'safety_confirm' | 'engine_config'
    | 'search_status';
  text?: string;
  path?: RoutePath;
  reply?: string;
  reason?: string;
  error?: string;
  action?: string;
  model?: string;
  devices?: Record<string, DeviceInfo>;
  entries?: CacheEntry[];
  id?: string;
  message?: string;
  task?: string;
  audio?: string;
  time?: TimeState;
  alert?: ProactiveAlert;
  suppressed?: boolean;
  command?: string;
  risk?: string;
  reasons?: string[];
  config?: Record<string, unknown>;
  saved?: boolean;
  reset?: boolean;
  status?: string;
  result?: string;
}

export interface DeviceInfo {
  name: string;
  type: 'light' | 'curtain' | 'heater' | 'ac' | 'fan' | 'tv';
  room: string;
  status: string;
  properties: Record<string, unknown>;
}

// ── TTS ──
export interface TTSAudio {
  audio: string;   // base64 mp3
  text: string;    // fallback text
  path: string;
}

// ── 主动提醒 ──
export interface ProactiveAlert {
  id: string;
  message: string;
  reason: string;
  actions?: { device: string; action: string }[];
  timestamp: number;
}

// ── 时间模拟 ──
export interface TimeState {
  simulated: boolean;
  current_time: string;   // HH:MM
  speed: number;          // 加速比, 1 = 实时
  paused: boolean;
}
