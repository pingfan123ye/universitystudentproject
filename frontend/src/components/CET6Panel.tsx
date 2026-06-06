/**
 * CET-6 备考面板 —— 试卷卡片 + PDF 预览 + 听力播放 + 答案 PDF + 在线搜索结果
 */
import { useState, useCallback } from 'react';
import { Cet6Paper, Cet6SearchResult } from '../types';

interface CET6PanelProps {
  paper: Cet6Paper | null;
  answers: { pdf_url: string } | null;
  onClose: () => void;
  // 在线搜索
  searchResults?: Cet6SearchResult[];
  onDownloadPaper?: (paperId: string) => void;
}

export default function CET6Panel({
  paper, answers, onClose,
  searchResults, onDownloadPaper,
}: CET6PanelProps) {
  const [showPdf, setShowPdf] = useState(false);
  const [showAnswers, setShowAnswers] = useState(false);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);

  const handleTogglePdf = useCallback(() => {
    setShowPdf((prev) => !prev);
    if (showAnswers) setShowAnswers(false);
  }, [showAnswers]);

  const handleToggleAnswers = useCallback(() => {
    setShowAnswers((prev) => !prev);
    if (showPdf) setShowPdf(false);
  }, [showPdf]);

  const handleDownload = useCallback((paperId: string) => {
    setDownloadingId(paperId);
    onDownloadPaper?.(paperId);
  }, [onDownloadPaper]);

  // ── 在线搜索结果视图 ──
  if (searchResults && searchResults.length > 0 && !paper) {
    return (
      <div className="cet6-panel animate-fade-in">
        <div className="cet6-header">
          <span className="cet6-header-title">🌐 在线真题库</span>
          <button className="cet6-close-btn" onClick={onClose} title="关闭">✕</button>
        </div>
        <div className="cet6-search-results">
          <div className="cet6-search-title">
            找到 {searchResults.length} 套试卷，点击下载
          </div>
          <div className="cet6-search-list">
            {searchResults.map((r) => (
              <div key={r.paper_id} className={`cet6-search-item ${r.downloaded ? 'downloaded' : ''}`}>
                <div className="cet6-search-item-info">
                  <span className="cet6-search-item-title">{r.title}</span>
                  <span className="cet6-search-item-meta">
                    {r.year}年{r.month}月 · 第{r.set_num}套
                    {r.downloaded && <span className="cet6-badge-done"> ✓ 已下载</span>}
                  </span>
                </div>
                <button
                  className="cet6-btn cet6-btn-download"
                  onClick={() => handleDownload(r.paper_id)}
                  disabled={r.downloaded || downloadingId === r.paper_id}
                >
                  {downloadingId === r.paper_id ? '⏳ 下载中...' : r.downloaded ? '已下载' : '⬇ 下载'}
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  // ── 试卷卡片视图（正常 + 答案） ──
  if (!paper) return null;

  return (
    <div className="cet6-panel animate-fade-in">
      <div className="cet6-header">
        <span className="cet6-header-title">📖 CET-6 备考</span>
        <button className="cet6-close-btn" onClick={onClose} title="关闭">✕</button>
      </div>

      <div className="cet6-paper-card">
        <div className="cet6-paper-title">{paper.title}</div>
        <div className="cet6-badges">
          {paper.hasAudio && (
            <span className="cet6-badge cet6-badge-audio">🎧 含听力</span>
          )}
          {paper.hasAnswers && (
            <span className="cet6-badge cet6-badge-answers">✅ 含答案</span>
          )}
        </div>

        <div className="cet6-actions">
          <button className="cet6-btn" onClick={handleTogglePdf}>
            📄 {showPdf ? '收起试卷' : '预览试卷'}
          </button>
          {/* 直接下载 PDF */}
          <a
            href={paper.pdfUrl}
            download
            className="cet6-btn cet6-btn-download"
            title="下载试卷PDF"
          >
            ⬇ 下载试卷
          </a>
          {paper.hasAnswers && (
            <>
              <button className="cet6-btn cet6-btn-secondary" onClick={handleToggleAnswers}>
                📝 {showAnswers ? '收起答案' : '查看答案'}
              </button>
              {/* 下载答案 PDF */}
              {(answers?.pdf_url || paper.answersUrl) && (
                <a
                  href={answers?.pdf_url || paper.answersUrl}
                  target="_blank" rel="noopener noreferrer"
                  className="cet6-btn cet6-btn-secondary"
                  title="下载答案PDF"
                >
                  ⬇ 下载答案
                </a>
              )}
            </>
          )}
        </div>

        {showPdf && (
          <div className="cet6-pdf-container">
            <object
              data={paper.pdfUrl}
              type="application/pdf"
              className="cet6-pdf-iframe"
              title="CET-6 真题试卷预览"
            >
              <p>
                无法直接预览 PDF，
                <a href={paper.pdfUrl} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)' }}>
                  点击打开试卷
                </a>
              </p>
            </object>
          </div>
        )}

        {showAnswers && (
          <div className="cet6-pdf-container">
            <div className="cet6-section-label">📝 答案解析</div>
            {(answers?.pdf_url || paper.answersUrl) ? (
              <object
                data={answers?.pdf_url || paper.answersUrl}
                type="application/pdf"
                className="cet6-pdf-iframe"
                title="CET-6 答案解析"
              >
                <p>
                  无法直接预览答案，
                  <a
                    href={answers?.pdf_url || paper.answersUrl}
                    target="_blank" rel="noopener noreferrer"
                    style={{ color: 'var(--accent)' }}
                  >
                    点击打开答案
                  </a>
                </p>
              </object>
            ) : (
              <p style={{ color: 'var(--text-muted)', fontSize: '13px', padding: '12px 0' }}>
                答案解析暂未加载，请对AI说「查看答案」获取
              </p>
            )}
          </div>
        )}

        {paper.hasAudio && paper.audioUrl && (
          <div className="cet6-audio-section">
            <div className="cet6-audio-label">🎧 听力音频</div>
            <audio controls src={paper.audioUrl} className="cet6-audio" preload="metadata" />
          </div>
        )}
      </div>
    </div>
  );
}
