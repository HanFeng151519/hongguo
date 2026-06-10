/** cap sync 会覆盖 packageClassList，需把 App 内自定义插件补回去 */
const fs = require("fs");
const path = require("path");

const configPath = path.resolve(
  __dirname,
  "..",
  "ios/App/App/capacitor.config.json"
);

const CUSTOM_PLUGINS = ["RealEsrganPlugin"];

if (!fs.existsSync(configPath)) {
  console.warn("patch-ios-plugins: capacitor.config.json 不存在，跳过");
  process.exit(0);
}

const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
const list = Array.isArray(config.packageClassList) ? [...config.packageClassList] : [];
let changed = false;

for (const name of CUSTOM_PLUGINS) {
  if (!list.includes(name)) {
    list.push(name);
    changed = true;
  }
}

if (changed) {
  config.packageClassList = list;
  fs.writeFileSync(configPath, `${JSON.stringify(config, null, "\t")}\n`);
  console.log("patch-ios-plugins: 已注册", CUSTOM_PLUGINS.join(", "));
} else {
  console.log("patch-ios-plugins: 自定义插件已在 packageClassList");
}
