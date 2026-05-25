"""抖音等站内投流合规：禁止引导至第三方 App / 站外搜索下载。"""

from __future__ import annotations

import os
import re

# 站外导流高风险词（片头片尾/字幕/文案中避免出现）
_OFFPLATFORM_RE = re.compile(
    r"红果|番茄|fanqie|fqopen|字节|下载.{0,6}(?:app|应用|软件)|"
    r"(?:去|到|在).{0,4}(?:别的|其他|第三方).{0,6}(?:平台|软件|app|应用)|"
    r"搜索.{0,8}(?:看|追|全集|继续)|搜.{0,4}(?:剧|全集|继续看)",
    re.I,
)

_DEFAULT_SPLASH_SUBTITLE_SAFE = "——  高能短剧 · 关注看全集  ——"
_DEFAULT_SPLASH_SUBTITLE_PROMO = "——  红果短剧搜索看全集  ——"


def douyin_safe_enabled() -> bool:
    """
    抖音投流合规模式。默认随 HONGGUO_AUTHENTIC=1 开启；
    显式 HONGGUO_DOUYIN_SAFE=0 可关闭（仅自用预览，勿直接发抖音）。
    """
    raw = os.getenv("HONGGUO_DOUYIN_SAFE", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    try:
        from video_originality import authentic_preservation_enabled

        return authentic_preservation_enabled()
    except ImportError:
        return True


def compliance_brand_name() -> str:
    if douyin_safe_enabled():
        return (
            os.getenv("HONGGUO_COMPLIANCE_BRAND", "丰丰漫剧").strip() or "丰丰漫剧"
        )
    return os.getenv("HONGGUO_BRAND_NAME", "红果短剧").strip() or "红果短剧"


def splash_subtitle_text() -> str:
    custom = os.getenv("HONGGUO_SPLASH_SUBTITLE", "").strip()
    if custom:
        return sanitize_promo_copy(custom, max_len=48) or custom[:48]
    if douyin_safe_enabled():
        return _DEFAULT_SPLASH_SUBTITLE_SAFE
    return _DEFAULT_SPLASH_SUBTITLE_PROMO


def commentary_footer_hint(drama_title: str) -> str:
    short = (drama_title or "短剧").strip()[:12]
    if douyin_safe_enabled():
        return f"《{short}》· 关注看全集"
    return f"红果短剧搜「{short}」看全集"


def outro_card_lines(keyword: str) -> tuple[str, str]:
    kw = (keyword or "短剧").strip()[:16]
    if douyin_safe_enabled():
        return "全集更精彩", f"《{kw}》· 关注不错过"
    return "想看全集？", f"红果短剧搜「{kw}」"


def safe_post_caption(drama_title: str) -> str:
    short = (drama_title or "短剧").strip()[:20]
    if douyin_safe_enabled():
        return (
            f"🔥《{short}》也太上头了！\n"
            f"第1集就高能，评论区说说你最气/最爽的是谁？\n"
            f"关注不迷路，全集更精彩\n"
            f"#{short} #短剧 #漫剧推荐"
        )
    return (
        f"🔥《{short}》也太上头了！\n"
        f"第1集就高能，评论区说说你最气/最爽的是谁？\n"
        f"👉 红果搜「{short}」继续看\n"
        f"#{short} #短剧 #漫剧推荐"
    )


def safe_commentary_fallbacks(
    drama_title: str, *, meme_mode: bool = False
) -> list[str]:
    short = (drama_title or "短剧").strip()[:10]
    if douyin_safe_enabled():
        if meme_mode:
            return ["前方高能", "这反转绝了", "关注看全集"]
        return [
            f"《{short}》这段太炸了",
            "注意看男主这个眼神",
            "反转来得猝不及防",
            "关注看全集",
        ]
    if meme_mode:
        return ["前方高能", "这反转绝了", f"红果搜{short}"]
    return [
        f"《{short}》这段太炸了",
        "注意看男主这个眼神",
        "反转来得猝不及防",
        f"红果搜「{short}」看全集",
    ]


def sanitize_promo_copy(text: str, *, max_len: int = 200) -> str:
    """去掉站外/App 导流表述，供 AI 输出与烧录字幕兜底。"""
    if not text:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    if douyin_safe_enabled():
        s = _OFFPLATFORM_RE.sub("", s)
        s = re.sub(r"搜.{0,10}(?:看|全集|继续|剧名)?", "", s)
        s = re.sub(r"[👉🔗🔥]{2,}", "", s)
        s = re.sub(r"\s+", " ", s).strip(" ，、；;搜")
        if len(s) < 2:
            return ""
    return s[:max_len]


def ai_compliance_rule_block() -> str:
    if not douyin_safe_enabled():
        return ""
    return (
        "【抖音合规】严禁出现第三方平台/App 名（红果、番茄等）、严禁「搜索/下载/去别的平台看」"
        "等站外导流；post_caption 仅站内互动（评论/关注/蹲后续）；"
        "meme_captions 禁止带 App 名；outro_keyword 仅剧名关键词（8-16 字）。"
    )
