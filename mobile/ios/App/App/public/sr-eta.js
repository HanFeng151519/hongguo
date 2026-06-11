/** 超分预计剩余时间文案 */
export function formatSrEta(sec) {
  const n = Math.ceil(Number(sec) || 0);
  if (n < 1) return "";
  const m = Math.floor(n / 60);
  const s = n % 60;
  if (m > 0) return `约剩 ${m} 分 ${s} 秒`;
  return `约剩 ${s} 秒`;
}

export function etaFromProgressPct(pct, startedAt) {
  const p = Number(pct);
  if (!startedAt || p <= 2 || p >= 98) return null;
  const elapsed = (Date.now() - startedAt) / 1000;
  return Math.max(1, Math.ceil((elapsed / p) * (100 - p)));
}
