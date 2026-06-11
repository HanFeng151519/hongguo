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

function createNativePluginProxy(name) {
  const c = getCapacitor();
  if (typeof c.nativePromise !== "function") {
    throw new Error(`Capacitor 桥接未就绪，无法调用插件 ${name}`);
  }
  return new Proxy(
    {},
    {
      get(_, prop) {
        const key = String(prop);
        if (key === "addListener") {
          return (eventName, callback) => c.addListener(name, eventName, callback);
        }
        if (key === "removeAllListeners") {
          return () => c.nativePromise(name, "removeAllListeners");
        }
        if (key === "removeListener") {
          return (eventName, callback) => c.removeListener(name, eventName, callback);
        }
        if (key === "then" || key === "catch" || key === "finally") {
          return undefined;
        }
        return (options) => c.nativePromise(name, key, options ?? {});
      },
    }
  );
}

export function getPlugin(name) {
  const c = getCapacitor();
  const existing = c.Plugins?.[name];
  if (existing) return existing;
  if (typeof c.registerPlugin === "function") {
    return c.registerPlugin(name);
  }
  if (c.nativePromise) {
    return createNativePluginProxy(name);
  }
  throw new Error(`Capacitor 插件不可用：${name}`);
}

export const Directory = {
  Documents: "DOCUMENTS",
  Data: "DATA",
  Library: "LIBRARY",
  Cache: "CACHE",
  External: "EXTERNAL",
  ExternalStorage: "EXTERNAL_STORAGE",
};
