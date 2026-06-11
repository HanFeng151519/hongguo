export const HTTP_URL_RE = /https?:\/\/[^\s\]\)"'<>，。；;]+/gi;

export function extractAllHttpUrls(text) {
  const raw = String(text || "").trim();
  if (!raw) return [];
  const urls = [];
  const seen = new Set();
  for (const m of raw.matchAll(HTTP_URL_RE)) {
    const u = m[0].replace(/[，。,.;；'"`]+$/g, "");
    if (!seen.has(u)) {
      seen.add(u);
      urls.push(u);
    }
  }
  return urls;
}

export function unescapeHtml(text) {
  return String(text || "")
    .replace(/\\u002F/g, "/")
    .replace(/\\\//g, "/")
    .replace(/\\u0026/g, "&");
}

export function isHttpUrl(url) {
  const u = String(url || "").trim();
  return u.startsWith("http://") || u.startsWith("https://");
}

export function awemeIdFromUrl(url) {
  const patterns = [/video\/(\d+)/, /note\/(\d+)/, /aweme_id=(\d+)/, /\/(\d{15,})/];
  for (const pat of patterns) {
    const m = String(url || "").match(pat);
    if (m) return m[1];
  }
  return "";
}
