import { Message } from '../types';

const pathBadge: Record<string, string> = {
  xiaoai: 'bg-blue-100 text-blue-700', llm: 'bg-purple-100 text-purple-700',
  reasonix: 'bg-teal-100 text-teal-700', cache: 'bg-green-100 text-green-700',
};
const pathLabel: Record<string, string> = {
  xiaoai: '小爱', llm: 'AI', reasonix: 'Reasonix', cache: '缓存',
};

// 模型标签映射
const modelLabel: Record<string, string> = {
  'deepseek:deepseek-v4-flash': 'DeepSeek',
  'deepseek:deepseek-v4-pro': 'DeepSeek',
  'ollama:qwen2.5:7b': 'Qwen',
  'ollama:qwen2.5:14b': 'Qwen',
  xiaoai: '小爱', reasonix: 'Reasonix', cache: '缓存',
};
const modelColor: Record<string, string> = {
  'deepseek:deepseek-v4-flash': 'bg-green-100 text-green-700',
  'deepseek:deepseek-v4-pro': 'bg-green-100 text-green-700',
  'ollama:qwen2.5:7b': 'bg-orange-100 text-orange-700',
  xiaoai: 'bg-blue-100 text-blue-700',
  reasonix: 'bg-teal-100 text-teal-700',
  cache: 'bg-green-100 text-green-700',
};

interface Props { message: Message }

export default function MessageBubble({ message }: Props) {
  const { role, content, path, isStreaming, model } = message;

  if (role === 'system') {
    return (
      <div className="flex justify-center my-3">
        <span className="px-3 py-1 text-[11px] rounded-full" style={{ background: 'var(--bg-input)', color: 'var(--text-muted)' }}>
          {content}
        </span>
      </div>
    );
  }

  const isUser = role === 'user';

  return (
    <div className={`flex gap-2.5 my-3 animate-slide-up ${isUser ? 'flex-row-reverse' : ''}`}>
      <div className={`flex-shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-bold ${isUser ? 'accent-btn' : ''}`}
        style={isUser ? {} : { background: 'var(--bg-input)', color: 'var(--text-muted)' }}>
        {isUser ? 'U' : 'AI'}
      </div>
      <div className={`flex flex-col max-w-[72%] ${isUser ? 'items-end' : 'items-start'}`}>
        <div className="px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap"
          style={{
            background: isUser ? 'var(--bubble-user-bg)' : 'var(--bubble-ai-bg)',
            color: isUser ? 'var(--bubble-user-text)' : 'var(--text-primary)',
            border: isUser ? '1px solid var(--accent-glow)' : '1px solid var(--border)',
            borderRadius: isUser ? '16px 16px 4px 16px' : '16px 16px 16px 4px',
          }}>
          {content}
          {isStreaming && <span className="inline-block w-1.5 h-4 ml-0.5 rounded-sm align-middle animate-pulse" style={{ background: 'var(--accent)' }} />}
        </div>
        <div className="mt-1 flex items-center gap-1.5">
          {path && !isStreaming && pathBadge[path] && (
            <span className={`px-2 py-0.5 rounded-full text-[10px] border ${pathBadge[path]}`}>
              {pathLabel[path] || path}
            </span>
          )}
          {model && !isStreaming && modelLabel[model] && (
            <span className={`px-2 py-0.5 rounded-full text-[10px] border ${modelColor[model] || 'bg-gray-100 text-gray-500'}`}>
              {modelLabel[model] || model}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
