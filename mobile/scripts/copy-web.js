/** 将 mobile/src/ 同步到 Capacitor www/ */
const fs = require("fs");
const path = require("path");

const src = path.resolve(__dirname, "..", "src");
const dest = path.resolve(__dirname, "..", "www");

function copyDir(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const name of fs.readdirSync(from)) {
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
console.log("copied mobile/src -> mobile/www");
