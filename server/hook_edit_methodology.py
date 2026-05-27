"""50 秒精华快切方法论：A/B/C 分级、两步剪辑、类型侧重（供 AI 与规则引擎共用）。"""

from __future__ import annotations

import os
import re

# A 类必留 / B 类衔接 / C 类全删（reason 或 AI 标注用）
TIER_A_MARKERS = (
    "A|",
    "A类",
    "冲突",
    "对峙",
    "打脸",
    "反转",
    "身份",
    "揭秘",
    "名场面",
    "爆发",
    "震惊",
    "羞辱",
    "碾压",
    "真相",
    "危机",
    "逆袭",
    "打斗",
    "特效",
    "告白",
    "暧昧",
    "悬念",
    "伏笔",
)
TIER_B_MARKERS = (
    "B|",
    "B类",
    "过渡",
    "衔接",
    "切换",
    "走路",
    "铺垫",
)
TIER_C_MARKERS = (
    "C|",
    "C类",
    "空镜",
    "闲聊",
    "日常",
    "发呆",
    "回忆",
    "注水",
    "片头",
    "片尾",
    "字幕播报",
    "重复",
    "远景",
    "路人",
    "穿梭",
    "走位",
    "走路",
    "换场",
    "过场",
    "跟拍",
    "人物移动",
    "无对白",
)

_DRAMA_TYPE_ALIASES = {
    "general": (),  # 无关键词命中时的全品类默认
    "shuangwen": ("爽文", "逆袭", "打脸", "战神", "赘婿", "重生", "玄幻", "修仙"),
    "sweet": ("甜宠", "恋爱", "言情", "暧昧", "总裁宠", "宠妻", "蜜恋"),
    "suspense": ("悬疑", "推理", "惊悚", "诡", "凶", "谜", "凶手"),
    "urban": ("都市", "豪门", "职场", "商战", "现实"),
    "revenge": ("复仇", "虐恋", "虐心", "重来", "报应"),
    "family": ("家庭", "婆媳", "亲子", "遗产", "亲情"),
}


def drama_type_from_env() -> str:
    raw = os.getenv("HONGGUO_DRAMA_TYPE", "").strip().lower()
    if raw in _DRAMA_TYPE_ALIASES and raw != "general":
        return raw
    if raw in ("爽文", "逆袭", "打脸"):
        return "shuangwen"
    if raw in ("甜宠", "恋爱", "言情"):
        return "sweet"
    if raw in ("悬疑", "推理"):
        return "suspense"
    return ""


def infer_drama_type(drama_title: str, drama_intro: str = "") -> str:
    env = drama_type_from_env()
    if env:
        return env
    blob = f"{drama_title} {drama_intro}"
    scores = {k: 0 for k in _DRAMA_TYPE_ALIASES if k != "general"}
    for key, words in _DRAMA_TYPE_ALIASES.items():
        if key == "general" or not words:
            continue
        for w in words:
            if w in blob:
                scores[key] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


def clip_tier(reason: str) -> str:
    r = (reason or "").strip()
    if not r:
        return "A"
    if r.upper().startswith("C") or any(m in r for m in TIER_C_MARKERS):
        return "C"
    if r.upper().startswith("B") or any(m in r for m in TIER_B_MARKERS):
        return "B"
    return "A"


def universal_short_drama_hint() -> str:
    """全品类短剧通用剪辑取向（都市/甜宠/悬疑/玄幻/虐恋等均适用）。"""
    return (
        "【全品类短剧 · 通用剪辑】按本集实际题材自主选高光，勿套某一题材的固定关键词或分镜：\n"
        "- 共通：冲突对峙、情绪爆发、关系反转、身份/真相揭露、金句对白、尾钩悬念；\n"
        "- 删：无信息过场、重复闲聊、拖沓回忆（删了看不懂因果则留最短必要对白）；\n"
        "- 都市/豪门：身份反差、对峙台词、抉择瞬间；甜宠：暧昧张力、误会与和解；\n"
        "- 悬疑：线索、惊疑、反转揭晓；玄幻/逆袭：打脸、实力反差、爽点爆发；\n"
        "- 家庭/虐恋：情感撕裂、决裂或和解的关键一句。\n"
        "成片须同时做到：叙事流畅、开场高能、霸气叙事、反转、尾钩引流、完整闭环（见六步骨架）。"
    )


def hook_narrative_arc_block() -> str:
    """好片叙事骨架：流畅 → 高能开场 → 霸气立势 → 反转 → 尾钩 → 闭环。"""
    return """
【六步叙事骨架 · 好片必达（段数/秒数由本集自定，时间顺序不乱）】
1. **叙事流畅**：段与段因果衔接，少碎跳；大跳剪须在 reason 标「跳剪」；同场景高能尽量合并。
2. **开场高能**：第 1 刀尽快让观众停滑——视听冲击或一句狠对白/强音效，忌长过场与无信息穿梭。
3. **霸气叙事**：用强势台词、对峙、身份/实力压制或情感撕扯立住人物与冲突（狠、稳、有压迫感或爽感）。
4. **反转**：至少 1 处认知颠覆（真相、身份、立场、情感逆转）——本片最强转折之一，须让观众「哇」一下。
5. **放出钩子**：末段留悬念、狠话或未解问题引流下一集；不要把本集结局全剧透光。
6. **完整闭环**：mini 弧线有头有尾——观众能答「谁、为啥闹起来、现在咋了」；hook_summary 须写明闭环点与尾钩。

clips 的 reason 建议在对应段标注环节（如 A|开场高能、A|霸气对峙、A|反转、A|尾钩）。

**故事优先（有完整对白表时）**：
- 从对白/画面轴中选出讲清六步叙事的全部好情节；**禁止为控时长短句、删反转/尾钩/关键对白**。
- 大跳剪到反转前尽量加 `B|过渡` 承上启下；只删 C 类无信息过场。
- 时长由情节决定，body duration_sec = clips 之和（可 45~80s 或更长）。
""".strip()


def story_first_editor_brief() -> str:
    return """
【故事完整 · 唯一标准（不是秒数）】
你已收到本集**完整对白表 + 画面/音效轴 + 结构地图**。任务：从中选出能讲清「六步叙事 + 闭环」的全部好情节，写成 clips。

- **保留**：六步各环节的高光对白与画面；因果链上的必要一句；金句须说到 end_sec。
- **删除**：仅 C 类——无信息穿梭、重复闲聊、与主线无关的冗长回忆。
- **禁止**：为凑约 50s/60s 压缩；为单段≤Xs 截断半句；删掉 AI 已识别的反转/尾钩。
- **输出**：body duration_sec = sum(clips.duration_sec)，时长是结果不是目标。
""".strip()


def drama_type_edit_hint(drama_type: str) -> str:
    if drama_type == "general":
        return universal_short_drama_hint()
    if drama_type == "sweet":
        return (
            "【甜宠/恋爱 · 在本集通用原则下】多留：对视、牵手、告白、暧昧张力、误会与和解；"
            "删配角八卦与无效拉扯。"
        )
    if drama_type == "suspense":
        return (
            "【悬疑 · 在本集通用原则下】多留：疑问台词、诡异镜头、线索、真相揭露；"
            "删无关铺垫与重复回忆。"
        )
    if drama_type == "urban":
        return (
            "【都市/豪门 · 在本集通用原则下】多留：身份反差、职场/家族对峙、抉择与打脸瞬间。"
        )
    if drama_type == "revenge":
        return (
            "【复仇/虐恋 · 在本集通用原则下】多留：决裂、真相、情感爆发与反击；"
            "虐点宜短而狠，服务反转。"
        )
    if drama_type == "family":
        return (
            "【家庭伦理 · 在本集通用原则下】多留：亲情撕裂、对峙、和解或决裂的关键对白。"
        )
    return (
        "【逆袭/爽点 · 在本集通用原则下】多留：挑衅、反击、当众打脸、实力/身份反转；"
        "删冗长内心独白与配角废话。"
    )


def drama_type_hint_for_prompt(drama_title: str, drama_intro: str = "") -> str:
    """提示词用：通用底座 + 可选题材轻提示。"""
    dtype = infer_drama_type(drama_title, drama_intro)
    base = universal_short_drama_hint()
    if dtype == "general":
        return base
    return base + "\n" + drama_type_edit_hint(dtype)


def ai_editor_autonomy_enabled() -> bool:
    """每集由 AI 结合对白/画面轴自主构思简版叙事，不套用单集固定分镜模板。"""
    v = os.getenv("HONGGUO_AI_EDITOR_AUTONOMY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def human_impact_script_brief(*, body_sec: float = 50.0) -> str:
    """产品北极星：六步叙事 + 故事完整（时长由情节决定）。"""
    try:
        from hook_timeline import (
            ai_script_duration_guidance,
            story_first_edit_enabled,
        )

        duration_line = ai_script_duration_guidance()
        if story_first_edit_enabled():
            return f"""
【创作任务 · 故事完整优先】
{duration_line}
{story_first_editor_brief()}

{hook_narrative_arc_block()}

hook_summary 六句式：①叙事线 ②开场高能 ③霸气立势 ④反转 ⑤尾钩 ⑥闭环。
""".strip()
    except ImportError:
        duration_line = f"参考约 {body_sec:.0f}s"
    else:
        duration_line = ai_script_duration_guidance()
    return f"""
【你的创作任务 · 闭环完整优先（{duration_line}）】
你是本集剪辑导演（全品类短剧）。结合「结构地图 + 对白表 + 画面/音效轴」剪出一条**流畅、霸气、有反转、能引流、能闭环**的简版故事。

{hook_narrative_arc_block()}

执行要点：高光与因果完整；对白说到句末；4~6 段覆盖六步即可。
hook_summary 六句式：①叙事线 ②开场高能点 ③霸气立势点 ④反转点 ⑤尾钩 ⑥闭环说明。
""".strip()


def editing_rules_block(*, body_sec: float = 45.0, opening_sec: float = 5.0) -> str:
    """嵌入 AI 剪辑提示的核心规则（与产品口播结构对齐）。"""
    try:
        from hook_timeline import (
            ai_script_duration_guidance,
            story_first_edit_enabled,
        )

        duration_line = ai_script_duration_guidance()
        if story_first_edit_enabled():
            return f"""
【核心筛选 · 故事完整优先（已提供完整对白表）】
{duration_line}
A 类必留：六步叙事各环节高光 + 因果链必要对白；金句说到 end_sec。
B 类：大跳剪到反转/尾钩前的承上启下（须有对白）。
C 类删：无信息过场、重复闲聊、无关回忆。
0~{opening_sec:.0f}s：系统口播 TTS；其后全为原片 A+B，按剧情顺序，原速 1x。
body duration_sec = clips 之和；超上限须删 C 类/压缩 B，**禁止**为凑秒数删 A 类/反转/尾钩。
""".strip()
    except ImportError:
        pass
    return f"""
【核心筛选 · 按优先级凑满正片约 {body_sec:.0f} 秒】
A 类（必留·高光）：冲突对峙、情绪爆发、关系/身份反转、名场面对话、爽点/暧昧/惊悚瞬间；
关键推进台词与动作；打斗/特效/高颜值互动。动感段 6~15s，对白高光 3~10s；禁止 5 秒以上纯平淡静止画面。
B 类（酌情·衔接）：极短承上启下，仅当删掉会导致叙事断裂时保留，且须有对白或明确情绪。
C 类（优先删）：重复空镜、无信息闲聊、拖沓身世回忆、纯走路/穿梭且无对白的过场（有对白的换场可保留最短必要一段）。

【时长卡位 · 口播结构】
0~{opening_sec:.0f}s：口播开场（系统固定 TTS，画面用 A 类高能，原速）。
{opening_sec:.0f}s 起：纯原片精华约 {body_sec:.0f} 秒（仅 A+B，按剧情顺序硬切，全程原速 1x；打斗激动段可标 micro_speed=1.05，默认勿整体加速）。

【两步剪辑】
1) 粗剪：先标红(A)黄(B)删灰(C)，按剧情顺序拼 A，再插少量 B；超时长先缩短 B（如 3s→1s），再删重复情绪/相似角度 A；严禁乱序、严禁拆断单句关键台词。
2) 精剪：以故事完整与高光为准，正片合计须落在时长要求内；同场景连续高光尽量一镜到底；连贯对话整句保留。

【避坑】不凑水时长；不大跨度乱拼；金句必须说完再切；不要为了时长保留 C 类。
每条 clips.reason 必须以 A| 或 B| 开头并写明类型（如 A|高能打脸：当众碾压）。
""".strip()


def creative_editor_autonomy_block() -> str:
    """强调每集独立创作，不套用某一集成功案例的固定分镜。"""
    return """
【自主剪辑 · 禁止套用固定模板】
- 下方「推荐分镜」若有，仅为算法草稿，**你必须按本集实际对白/画面轴重写**入点、段数、时长，可全部推翻。
- 不同剧种、不同集的开篇、段数、跳剪位置均可不同；以「六步叙事骨架 + 完整闭环」为质量标准。
- 不要为凑满固定秒数或固定 4 段而拆碎/合并；也不要照搬其他集的秒数。
""".strip()


def opening_hook_pattern_block() -> str:
    """兼容旧引用：已合并为自主创作说明。"""
    return creative_editor_autonomy_block()


def clips_count_for_body(body_sec: float, *, cohesive: bool = True) -> int:
    """连贯少切：约 5 段×9s；密集快切：约 15 段×3s。"""
    if cohesive:
        return max(4, min(6, int(round(body_sec / 9.5))))
    return max(8, min(16, int(round(body_sec / 3.0))))
