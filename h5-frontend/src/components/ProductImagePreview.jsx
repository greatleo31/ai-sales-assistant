import { useEffect, useState } from 'react';

export default function ProductImagePreview({
  src,
  alt = '商品图片'
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [imageError, setImageError] = useState(false);
  const [previewError, setPreviewError] = useState(false);

  useEffect(() => {
    setImageError(false);
    setPreviewError(false);
    setIsOpen(false);
  }, [src]);

  useEffect(() => {
    if (!isOpen) return;
    const handleEscape = (event) => {
      if (event.key === 'Escape') {
        setIsOpen(false);
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isOpen]);

  const openPreview = () => {
    if (!imageError) {
      setPreviewError(false);
      setIsOpen(true);
    }
  };

  const closePreview = () => setIsOpen(false);

  return (
    <>
      <button
        type="button"
        className="hg-image-preview-trigger"
        aria-label={`放大查看${alt}`}
        title="点击查看大图"
        disabled={imageError}
        onClick={openPreview}
      >
        {imageError ? (
          <span className="hg-image-placeholder">
            图片暂不可用
          </span>
        ) : (
          <img
            className="hg-prod-thumb"
            src={src}
            alt={alt}
            loading="lazy"
            onError={() => setImageError(true)}
          />
        )}
      </button>

      {isOpen && (
        <div
          className="hg-image-preview-overlay"
          role="dialog"
          aria-modal="true"
          aria-label={`${alt}大图预览`}
          onClick={(event) => {
            if (event.target === event.currentTarget) {
              closePreview();
            }
          }}
        >
          <div className="hg-image-preview-panel">
            <button
              type="button"
              className="hg-image-preview-close"
              aria-label="关闭图片预览"
              onClick={closePreview}
            >
              ×
            </button>

            {previewError ? (
              <p role="alert">商品大图加载失败。</p>
            ) : (
              <img
                src={src}
                alt={alt}
                className="hg-image-preview-large"
                onError={() => setPreviewError(true)}
              />
            )}

            <p className="hg-image-preview-caption">
              {alt}
            </p>
          </div>
        </div>
      )}
    </>
  );
}
