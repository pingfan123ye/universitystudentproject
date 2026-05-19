import { Message } from '../types';
import { FiUser, FiCpu, FiServer } from 'react-icons/fi';

const pathBadge: Record<string, string> = {
  xiaoai: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  llm: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
  reasonix: 'bg-teal-500/10 text-teal-400 border-teal-500/20',
  cache: 'bg-green-500/10 text-green-400 border-green-500/20',
};

interface Props { message: Message }

export default function MessageBubble({ message }: Props) {
  const { role, content, path, isStreaming } = message;

  if (role === 'system') {
    return (
      <div className="flex justify-center my-3">
        <div className="flex items-center gap-1.5 px-3 py-1.5 bg-surface-2 rounded-full text-[11px] text-white/30">
          <FiServer size={11} /> {content}
        </div>
      </div>
    );
  }

  const isUser = role === 'user';

  return (
    <div className={`flex gap-2.5 my-3 animate-slide-up ${isUser ? 'flex-row-reverse' : ''}`}>
      <div className={`flex-shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-[11px] ${isUser ? 'bg-accent-amber/20 text-accent-amber' : 'bg-surface-3 text-white/40'}`}>
        {isUser ? <FiUser size={13} /> : <FiCpu size={13} />}
      </div>
      <div className={`flex flex-col max-w-[72%] ${isUser ? 'items-end' : 'items-start'}`}>
        <div className={`px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap ${
          isUser
            ? 'bg-accent-amber/15 text-accent-amber/90 rounded-2xl rounded-tr-sm border border-accent-amber/10'
            : 'glass rounded-2xl rounded-tl-sm text-white/80'
        }`}>
          {content}
          {isStreaming && <span className="inline-block w-1.5 h-4 ml-0.5 bg-accent-amber/60 animate-pulse rounded-sm align-middle" />}
        </div>
        {path && !isStreaming && pathBadge[path] && (
          <span className={`mt-1 px-2 py-0.5 rounded-full text-[10px] border ${pathBadge[path]} animate-fade-in`}>
            {path === 'xiaoai' ? '小爱' : path === 'llm' ? 'AI' : path === 'reasonix' ? 'Reasonix' : '缓存'}
          </span>
        )}
      </div>
    </div>
  );
}
