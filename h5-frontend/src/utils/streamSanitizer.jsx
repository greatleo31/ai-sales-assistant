import React from 'react';

/**
 * 第三道防线：前端流式自愈文本净水器 (streamSanitizer.jsx)
 * 确保 C 端消费者界面 100% 杜绝裸露：
 *   1. `<br>`、`<br/>`、`<div>`、`</p>` 等任何 HTML 标签或实体 (`&nbsp;`, `&yen;`)；
 *   2. `$...$`、`$$...$$`、`\rightarrow`、`\ge`、`\le`、`\times`、`\Delta` 等 LaTeX 数学公式符号；
 *   3. 裸露的 Markdown 标题井号 (`###`)、表格分隔线 (`|---|`) 与流式生成半途未闭合的 `**` 星号闪烁。
 */
export function sanitizeCustomerStreamText(rawText, isStreaming = false) {
  if (!rawText) return '';
  let text = String(rawText);

  // 1. 剥离内部协议标记
  text = text.replace(/<\|FunctionCallBegin\|>[\s\S]*?<\|FunctionCallEnd\|>/g, '');
  text = text.replace(/<\|[A-Za-z0-9_]+?\|>/g, '');

  // 2. HTML 换行与块级标签 -> 自然换行，其余 HTML 标签安全剥离
  text = text.replace(/<\s*br\s*\/?\s*>/gi, '\n');
  text = text.replace(/<\s*\/?\s*(?:p|div|li|ul|ol|tr|h[1-6])\b[^>]*>/gi, '\n');
  text = text.replace(/<\s*\/?\s*(?:span|strong|em|b|i|code|pre|table|thead|tbody|td|th|hr|a|img)\b[^>]*>/gi, '');
  text = text.replace(/<\/?[a-zA-Z][a-zA-Z0-9_-]*(?:\s+[^<>]*)?>/g, '');

  // 3. HTML 实体还原
  text = text
    .replace(/&nbsp;/gi, ' ')
    .replace(/&yen;/gi, '¥')
    .replace(/&gt;/gi, '>')
    .replace(/&lt;/gi, '<')
    .replace(/&amp;/gi, '&')
    .replace(/&quot;/gi, '"');

  // 4. LaTeX 数学符号与公式自然语言化
  const latexMap = [
    [/\\rightarrow\b|\\to\b|\\Rightarrow\b/g, ' → '],
    [/\\leftarrow\b|\\Leftarrow\b/g, ' ← '],
    [/\\leftrightarrow\b|\\Leftrightarrow\b/g, ' ↔ '],
    [/\\ge\b|\\geq\b|\\geqslant\b/g, '≥'],
    [/\\le\b|\\leq\b|\\leqslant\b/g, '≤'],
    [/\\neq\b|\\ne\b/g, '≠'],
    [/\\approx\b/g, '≈'],
    [/\\times\b/g, '×'],
    [/\\div\b/g, '÷'],
    [/\\pm\b/g, '±'],
    [/\\Delta\b/g, '差额'],
    [/\\infty\b/g, '∞'],
    [/\\%/g, '%'],
    [/\\\$/g, '¥'],
  ];
  for (const [pattern, replacement] of latexMap) {
    text = text.replace(pattern, replacement);
  }
  text = text.replace(/\\(?:text|mathrm|mathbf|mathit|textbf)\s*\{([^{}]*)\}/g, '$1');
  text = text.replace(/\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}/g, '$1/$2');
  text = text.replace(/\$\$([\s\S]*?)\$\$/g, '$1');
  text = text.replace(/\$([^$\n]+?)\$/g, '$1');
  text = text.replace(/\$/g, '');

  // 5. 流式尾部半截标签防闪烁（如末尾刚输出 `<b` 或 `\right`）
  if (isStreaming) {
    text = text.replace(/<[a-zA-Z/][^>]{0,16}$/g, '');
    text = text.replace(/\\[a-zA-Z]{1,14}$/g, '');
  }

  // 6. 逐行清理裸 Markdown 标题 `#` 与表格分隔线 `|---|`
  const cleanedLines = [];
  for (const rawLine of text.split('\n')) {
    const trimmed = rawLine.trim();
    if (/^\|?[\s:|-]+\|[\s:|-]*$/.test(trimmed) && trimmed.includes('-')) {
      continue;
    }
    let line = rawLine.replace(/^\s*#{1,6}\s+/, '');
    if (line.trim().startsWith('|') && line.trim().endsWith('|')) {
      const cells = line
        .trim()
        .slice(1, -1)
        .split('|')
        .map((c) => c.trim())
        .filter(Boolean);
      line = cells.join(' · ');
    }
    cleanedLines.push(line);
  }
  text = cleanedLines.join('\n').replace(/\n{3,}/g, '\n\n').trim();

  // 7. 流式未闭合粗体 `**` 自动自愈闭合
  const boldCount = (text.match(/\*\*/g) || []).length;
  if (boldCount % 2 === 1) {
    if (text.endsWith('**')) {
      text = text.slice(0, -2);
    } else {
      text = `${text}**`;
    }
  }
  if (isStreaming && text.endsWith('*') && !text.endsWith('**')) {
    text = text.slice(0, -1);
  }

  return text;
}

/**
 * 将单行文本中的 `**加粗**`、`¥价格` 与 `` `SKU/单号` `` 解析为富视觉 React 节点
 */
function renderInlineTokens(lineText, keyPrefix = 'in') {
  if (!lineText) return null;
  const tokenRegex = /(\*\*[^*]+\*\*|`[^`]+`|¥\d+(?:\.\d{1,2})?)/g;
  const parts = lineText.split(tokenRegex);

  return parts.map((part, idx) => {
    if (!part) return null;
    const key = `${keyPrefix}-${idx}`;
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      const inner = part.slice(2, -2);
      // 若加粗内容本身是价格，赋予高光珊瑚金样式
      if (/^¥\d+(?:\.\d{1,2})?$/.test(inner.trim())) {
        return (
          <span key={key} className="ios-inline-price">
            {inner}
          </span>
        );
      }
      return (
        <strong key={key} className="ios-inline-bold">
          {inner}
        </strong>
      );
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <span key={key} className="ios-inline-code">
          {part.slice(1, -1)}
        </span>
      );
    }
    if (/^¥\d+(?:\.\d{1,2})?$/.test(part)) {
      return (
        <span key={key} className="ios-inline-price">
          {part}
        </span>
      );
    }
    return <React.Fragment key={key}>{part}</React.Fragment>;
  });
}

/**
 * 面向 C 端消费者的零污染富文本组件
 */
export function CustomerRichText({ text, isStreaming = false }) {
  const safeText = sanitizeCustomerStreamText(text, isStreaming);
  if (!safeText) {
    return isStreaming ? (
      <div className="ios-typing-dots" aria-label="正在思考">
        <span />
        <span />
        <span />
      </div>
    ) : null;
  }

  const lines = safeText.split('\n');
  const blocks = [];

  lines.forEach((rawLine, idx) => {
    const line = rawLine.trim();
    if (!line) return;

    const bulletMatch = line.match(/^(?:[-•*]|(\d+)\.)\s+(.*)$/);
    if (bulletMatch) {
      const numBadge = bulletMatch[1];
      const content = bulletMatch[2];
      blocks.push(
        <div key={`b-${idx}`} className="ios-rich-bullet">
          <span className="ios-bullet-dot">{numBadge ? `${numBadge}.` : '✦'}</span>
          <div className="ios-bullet-body">{renderInlineTokens(content, `bl-${idx}`)}</div>
        </div>
      );
    } else {
      blocks.push(
        <p key={`p-${idx}`} className="ios-rich-paragraph">
          {renderInlineTokens(line, `pl-${idx}`)}
        </p>
      );
    }
  });

  return (
    <div className="ios-rich-text">
      {blocks}
      {isStreaming && <span className="ios-stream-caret" />}
    </div>
  );
}
