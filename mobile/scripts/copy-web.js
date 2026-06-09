/** 将 public/ 同步到 Capacitor www/（排除大体积缓存） */
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..", "..");
const src = path.join(root, "public");
const dest = path.join(root, "mobile", "www");

const SKIP_DIRS = new Set([
  "materials",
  "downloads",
  "cache",
  "uploads",
  "tts_cache",
]);

function copyDir(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const name of fs.readdirSync(from)) {
    if (SKIP_DIRS.has(name)) continue;
    const s = path.join(from, name);
    const d = path.join(to, name);
    const st = fs.statSync(s);
    if (st.isDirectory()) copyDir(s, d);
    else fs.copyFileSync(s, d);
  }
}

if (fs.existsSync(dest)) {
  fs.rmSync(dest, { recursive: true, force: true });
}
copyDir(src, dest);
console.log("copied public -> mobile/www");
