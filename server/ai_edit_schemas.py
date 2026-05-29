from pydantic import BaseModel, Field


class AiEditPlanRequest(BaseModel):
    series_id: str = Field(default="")
    drama_title: str = Field(default="")
    drama_intro: str = Field(default="")
    opening: str = Field(default="")
    keyword: str = Field(default="")
    episode_item_ids: list[str] = Field(default_factory=list)
    episode_titles: dict[str, str] = Field(default_factory=dict)


class ManualEditPlanDraftRequest(BaseModel):
    series_id: str = Field(..., min_length=1)
    drama_title: str = Field(default="")
    opening: str = Field(default="")
    keyword: str = Field(default="")
    episode_item_ids: list[str] = Field(..., min_length=1, max_length=6)
    episode_titles: dict[str, str] = Field(default_factory=dict)
    prefill: str = Field(
        default="simple",
        description="simple=預填兩段高光；empty=空占位片段",
    )
