from typing import Optional

from pydantic import BaseModel, Field


class GenerateHookRequest(BaseModel):
    series_id: str = Field(..., min_length=1)
    drama_title: str = Field(default="")
    cover_url: str = Field(default="", description="短剧海报图 URL")
    opening: str = Field(default="", description="已废弃，由 AI 自动生成片头文案")
    keyword: str = Field(
        default="",
        max_length=40,
        description="已废弃：片头关键词（留空即可）",
    )
    episode_item_ids: list[str] = Field(..., min_length=1, max_length=6)
    use_fq_koc_material: bool = Field(
        default=True,
        description="正片仅使用番茄达人中心/推广中心素材（不再下载红果加密原片）",
    )
    use_kuaishou_material: bool = Field(
        default=False,
        description="可选：达人中心失败时再尝试快手/抖音站外素材",
    )
    kuaishou_share_url: str = Field(
        default="",
        description="可选：快手或抖音作品/分享链接，优先于关键词搜索",
    )
    use_ai_edit: bool = Field(
        default=False,
        description="使用 LLM 生成剪辑方案；默认关闭，走两段高光直剪",
    )
    drama_intro: str = Field(
        default="",
        description="剧集简介，供大模型分析剪辑节奏",
    )
    fq_koc_create_url: str = Field(
        default="",
        description="可选：浏览器里 batch_download/create 完整请求 URL",
    )
    fq_koc_cookie: str = Field(
        default="",
        description="可选：番茄达人中心 Cookie（与 .env 二选一，请求级覆盖）",
    )
    fq_koc_ms_token: str = Field(default="", description="可选：msToken（会过期）")
    fq_koc_a_bogus: str = Field(default="", description="可选：a_bogus（会过期）")
    fq_koc_download_body: str = Field(
        default="",
        description="可选：batch_download/create 的 Request Payload JSON",
    )
    fq_koc_mp4_url: str = Field(
        default="",
        description="可选：本集 CDN 直链（F12 下载 MP4 的完整 URL，跳过 batch_download）",
    )
    splash_title_font: Optional[int] = Field(
        default=None,
        ge=48,
        le=220,
        description="片头《关键词》字号（px），不传则用自动或 .env",
    )
    splash_subtitle_font: Optional[int] = Field(
        default=None,
        ge=32,
        le=180,
        description="片头副标题「红果短剧搜索看全集」字号（px）",
    )
    splash_badge: str = Field(
        default="",
        max_length=24,
        description="片头封面下方可选文字，如 1-5",
    )
    edit_plan: Optional[dict] = Field(
        default=None,
        description="手動多段剪輯方案（body_segments.clips）；傳入時跳過 AI/兩段高光自動選段",
    )
    pause_for_manual_edit: bool = Field(
        default=False,
        description="先走生成流程下载正片，允许中断后在时间轴调整（再带 edit_plan 继续成片）",
    )


class FqKocSessionImportRequest(BaseModel):
    curl_text: str = Field(..., min_length=10, description="Chrome Copy as cURL 全文")


class FqKocSessionSyncRequest(BaseModel):
    series_id: str = Field(default="", description="短剧 book_id，用于在页面内试下载")
    item_id: str = Field(default="", description="分集 item_id")
    drama_title: str = Field(default="", description="剧名，浏览器优先按剧名检索")
    open_browser: bool = Field(
        default=True,
        description="True=弹出浏览器（首次登录）；False=无头同步",
    )


class FqKocLoginRequest(BaseModel):
    series_id: str = Field(default="", description="短剧 book_id")
    timeout_sec: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="登录等待秒数（默认 600）",
    )


class KuaishouMaterialRequest(BaseModel):
    keyword: str = Field(default="", description="搜索关键词，默认用 drama_title")
    drama_title: str = Field(default="")
    share_url: str = Field(default="", description="快手分享链接，有则跳过搜索")


class FqKocMaterialRequest(BaseModel):
    series_id: str = Field(..., min_length=1, description="短剧 book_id / series_id")
    item_id: str = Field(..., min_length=1, description="分集 item_id")
    drama_title: str = Field(default="", description="剧名，用于本地文件名")
    fq_koc_download_body: str = Field(default="", description="可选 batch_download Payload")


class FqKocCacheUrlRequest(BaseModel):
    series_id: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    mp4_url: str = Field(..., min_length=20, description="F12 里该集 MP4 完整 URL")


class FqKocCaptureRequest(BaseModel):
    """推广中心助手脚本回传的下载信息。"""

    series_id: str = Field(default="")
    item_id: str = Field(default="")
    mp4_url: str = Field(default="")
    download_url: str = Field(default="")
    create_url: str = Field(default="")
    cookie: str = Field(default="")
    download_body: str = Field(default="")


class EpisodeItem(BaseModel):
    title: str
    item_id: str
