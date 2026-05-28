import { Component, ErrorInfo, ReactNode } from 'react';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('ErrorBoundary caught:', error, info);
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback || (
        <div className="flex flex-col items-center justify-center h-screen p-8"
          style={{ background: 'var(--bg-root)', color: 'var(--text-primary)' }}>
          <div className="mb-4 w-14 h-14 rounded-full flex items-center justify-center"
            style={{ background: 'var(--accent-glow)' }}>
            <span className="text-xl" style={{ color: 'var(--accent)' }}>⚠</span>
          </div>
          <h2 className="text-lg font-bold mb-2" style={{ color: 'var(--text-secondary)' }}>
            出了点问题
          </h2>
          <p className="text-sm mb-4" style={{ color: 'var(--text-muted)' }}>
            {this.state.error?.message || '未知错误'}
          </p>
          <button
            onClick={() => this.setState({ hasError: false, error: null })}
            className="px-4 py-2 rounded border text-sm transition-colors"
            style={{ borderColor: 'var(--border)', color: 'var(--text-secondary)' }}>
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
