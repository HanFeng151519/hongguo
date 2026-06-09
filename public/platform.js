/**
 * 浏览器跨平台：Windows / macOS 桌面、iPhone / Android 手机浏览器。
 */
(function (global) {
  function detect() {
    const ua = navigator.userAgent || "";

    const isIPadOS =
      /iPad/i.test(ua) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const isIPhone = /iPhone|iPod/i.test(ua);
    const isAndroid = /Android/i.test(ua);

    let os = "unknown";
    if (isIPhone || isIPadOS) os = "ios";
    else if (isAndroid) os = "android";
    else if (/Mac/i.test(ua)) os = "mac";
    else if (/Win/i.test(ua)) os = "windows";
    else if (/Linux/i.test(ua)) os = "linux";

    const isDesktopBrowser = os === "windows" || os === "mac" || os === "linux";
    const isMobileWeb = !isDesktopBrowser && (os === "ios" || os === "android");

    let label = "网页";
    if (os === "ios") label = "iOS 浏览器";
    else if (os === "android") label = "Android";
    else if (os === "mac") label = "macOS";
    else if (os === "windows") label = "Windows";
    else if (os === "linux") label = "Linux";

    return {
      os,
      isDesktopBrowser,
      isMobileWeb,
      isWindows: os === "windows",
      isMac: os === "mac",
      label,
      caps: {
        folderBatch: isDesktopBrowser,
        multiImagePick: isMobileWeb,
        directDownload: true,
      },
    };
  }

  let cached = null;

  function get() {
    if (!cached) cached = detect();
    return cached;
  }

  function applyPlatformUi() {
    const p = get();
    const root = document.documentElement;
    root.dataset.platform = p.os;
    root.classList.add(`platform-${p.os}`);
    if (p.isMobileWeb) root.classList.add("platform-mobile-web");
    if (p.isDesktopBrowser) root.classList.add("platform-desktop");

    document.querySelectorAll(".desktop-only").forEach((el) => {
      el.classList.toggle("hidden", !p.caps.folderBatch);
    });
    document.querySelectorAll(".mobile-web-only").forEach((el) => {
      el.classList.toggle("hidden", !p.isMobileWeb);
    });

    const badge = document.getElementById("platform-badge");
    if (badge) badge.textContent = p.label;
  }

  global.HongguoPlatform = {
    get,
    detect,
    applyPlatformUi,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", applyPlatformUi);
  } else {
    applyPlatformUi();
  }
})(window);
