/* ============================================================
 * 通用输出格式化器（原「评估报告解析器」）
 * 前端不再绑定任何特定 Skill 的输出结构：模型返回什么就正常输出什么。
 * 支持三种展示形态（自动识别）：
 *   1. 纯 JSON          → 缩进格式化展示（便于阅读）
 *   2. 含 Markdown 表格 → 渲染成 HTML 表格（Skill 输出格式可据此调节页面样式）
 *   3. 普通文本         → 原样保留换行展示
 * 解析失败时原样输出，页面不崩。
 * ============================================================ */

/**
 * HTML 转义（report-parser 独立于 index.html 的 esc，命名区分避免冲突）
 * @param {*} s
 * @returns {string}
 */
function escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/**
 * 从任意文本中提取 JSON 对象（用于识别「模型输出的是纯 JSON」）。
 * 兼容：纯 JSON / ```json 代码块 / 文本前后夹杂说明文字。
 * @param {string} text
 * @returns {object|null}
 */
function extractJsonObject(text) {
  if (!text || typeof text !== 'string') return null;
  const trimmed = text.trim();
  // 1) 直接 parse
  try { return JSON.parse(trimmed); } catch (e) { /* fallthrough */ }
  // 2) ```json ... ``` 代码块
  const fence = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence) {
    try { return JSON.parse(fence[1].trim()); } catch (e) { /* fallthrough */ }
  }
  // 3) 首个 { 到末个 } 之间
  const start = trimmed.indexOf('{');
  const end = trimmed.lastIndexOf('}');
  if (start !== -1 && end > start) {
    try { return JSON.parse(trimmed.slice(start, end + 1)); } catch (e) { /* fallthrough */ }
  }
  return null;
}

/**
 * 把文本中的 Markdown 表格（含分隔行的连续 | 行）渲染成 HTML <table>，
 * 其余内容按普通段落/换行文本输出。
 * @param {string} text
 * @returns {string} HTML 片段
 */
function markdownToHtml(text) {
  const lines = text.split(/\r?\n/);
  const out = [];
  let i = 0;
  const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l);

  while (i < lines.length) {
    if (isTableRow(lines[i])) {
      // 收集连续的表头/分隔/数据行
      const block = [];
      while (i < lines.length && isTableRow(lines[i])) {
        block.push(lines[i].replace(/^\s*\||\|\s*$/g, ''));
        i++;
      }
      // 第二行是 |---|---| 分隔行才视为表格
      if (block.length >= 2 && /^\s*:?-+:?\s*(\|\s*:?-+:?\s*)*$/.test(block[1])) {
        const head = block[0].split('|').map(c => c.trim());
        const rows = block.slice(2).filter(r => r.trim() !== '');
        out.push(
          '<table class="gen-table"><thead><tr>' +
          head.map(c => '<th>' + escHtml(c) + '</th>').join('') +
          '</tr></thead><tbody>' +
          rows.map(r => '<tr>' + r.split('|').map(c => '<td>' + escHtml(c.trim()) + '</td>').join('') + '</tr>').join('') +
          '</tbody></table>'
        );
      } else {
        // 不是标准表格：按换行文本输出
        out.push('<pre class="gen-text">' + escHtml(block.join('\n')) + '</pre>');
      }
    } else if (lines[i].trim() === '') {
      i++; // 空行跳过
    } else {
      out.push('<p class="gen-p">' + escHtml(lines[i]) + '</p>');
      i++;
    }
  }
  return out.join('');
}

/**
 * 通用输出入口：自动识别 JSON / Markdown 表格 / 纯文本，返回展示 HTML。
 * @param {string} rawText 模型原始输出
 * @returns {{kind: string, html: string}}
 */
function formatGenericOutput(rawText) {
  if (!rawText || !String(rawText).trim()) {
    return { kind: 'empty', html: '' };
  }
  const text = String(rawText);
  const obj = extractJsonObject(text);
  if (obj) {
    return { kind: 'json', html: '<pre class="gen-json">' + escHtml(JSON.stringify(obj, null, 2)) + '</pre>' };
  }
  return { kind: 'text', html: markdownToHtml(text) };
}
