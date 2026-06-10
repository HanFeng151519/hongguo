/** 通过 window.Capacitor 访问原生插件（无需 bundler 解析 @capacitor/*） */
export function getCapacitor() {
  const c = globalThis.Capacitor;
  if (!c) {
    throw new Error("Capacitor 未加载，请重新安装 App");
  }
  return c;
}

export function isNativePlatform() {
  try {
    return getCapacitor().isNativePlatform();
  } catch {
    return false;
  }
}

export function getPlugin(name) {
  const c = getCapacitor();
  return c.Plugins?.[name] ?? c.registerPlugin(name);
}

export const Directory = {
  Documents: "DOCUMENTS",
  Data: "DATA",
  Library: "LIBRARY",
  Cache: "CACHE",
  External: "EXTERNAL",
  ExternalStorage: "EXTERNAL_STORAGE",
};
