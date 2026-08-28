/* ============================================================
 * 评估报告解析器
 * 把模型输出（预期为 SKILL.md 定义的 JSON）转换为统一的报告数据结构。
 * 不依赖 Markdown 字符串解析；解析失败时返回带 error 的降级结构，页面不崩。
 * ============================================================ */

/**
 * 从任意模型输出文本中提取 JSON 对象。
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

/** 安全取整数值，非法返回 fallback */
function toInt(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) ? Math.round(n) : fallback;
}

/** 安全取字符串，非法/缺失返回 fallback */
function toStr(v, fallback) {
  if (v === null || v === undefined) return fallback;
  return String(v);
}

/**
 * 解析评估结果为统一报告结构。
 * @param {string} rawText 模型原始输出
 * @returns {{
 *   ok: boolean,
 *   overall_issue: string,
 *   dimensions: {name: string, score: number, max_score: number, issue: string}[],
 *   total_score: number,      // 前端根据 dimensions 自算，不信任模型 total_score
 *   max_total: number,
 *   main_issues: {dimension: string, problem_type: string, score: number, description: string, round: number|null, evidence: string}[],
 *   suggestions: string[],
 *   raw: string
 * }}
 */
function parseEvalReport(rawText) {
  const result = {
    ok: false,
    overall_issue: '',
    dimensions: [],
    total_score: 0,
    max_total: 0,
    main_issues: [],
    suggestions: [],
    raw: rawText || '',
  };
  const obj = extractJsonObject(rawText);
  if (!obj || typeof obj !== 'object') return result;

  // dimensions：至少要解析出一个带 name 的维度才算成功
  const dimsRaw = Array.isArray(obj.dimensions) ? obj.dimensions : [];
  const dimensions = dimsRaw
    .filter(d => d && typeof d === 'object')
    .map(d => {
      const max = toInt(d.max_score, 2);
      let score = toInt(d.score, 0);
      if (score < 0) score = 0;
      if (score > max) score = max;
      return {
        name: toStr(d.name, '未知维度'),
        score,
        max_score: max,
        issue: toStr(d.issue, ''),
      };
    });
  if (!dimensions.length) return result;

  // 总分自算（不信任模型返回的 total_score）
  const total = dimensions.reduce((s, d) => s + d.score, 0);
  const maxTotal = dimensions.reduce((s, d) => s + d.max_score, 0);

  // main_issues：兼容 SKILL.md 的六字段结构与简化的三字段结构
  const issuesRaw = Array.isArray(obj.main_issues) ? obj.main_issues : [];
  const mainIssues = issuesRaw
    .filter(i => i && typeof i === 'object')
    .map(i => ({
      dimension: toStr(i.dimension, ''),
      problem_type: toStr(i.problem_type, ''),
      score: toInt(i.severity, null) ?? toInt(i.score, null),
      description: toStr(i.description || i.evidence || '', ''),
      round: i.round !== undefined && i.round !== null && Number.isFinite(Number(i.round)) && Number(i.round) > 0
        ? Number(i.round) : null,
      evidence: toStr(i.evidence, ''),
    }));

  const suggestions = (Array.isArray(obj.suggestions) ? obj.suggestions : [])
    .map(s => toStr(s, '').trim()).filter(Boolean);

  result.ok = true;
  result.overall_issue = toStr(obj.overall_issue, '').trim();
  result.dimensions = dimensions;
  result.total_score = total;
  result.max_total = maxTotal;
  result.main_issues = mainIssues;
  result.suggestions = suggestions;
  return result;
}

/**
 * 生成报告标题徽标用的等级：按得分比例分档。
 * @param {number} score
 * @param {number} max
 * @returns {{label: string, cls: string}}
 */
function scoreGrade(score, max) {
  if (!max) return { label: '—', cls: 'unknown' };
  const ratio = score / max;
  if (ratio >= 0.9) return { label: '优秀', cls: 'good' };
  if (ratio >= 0.7) return { label: '表现良好', cls: 'mid' };
  return { label: '待提升', cls: 'low' };
}
