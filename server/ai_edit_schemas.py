from pydantic import BaseModel, Field


class AiEditPlanRequest(BaseModel):
    series_id: str = Field(default="")
    drama_title: str = Field(default="")
    drama_intro: str = Field(default="")
    opening: str = Field(default="")
    keyword: str = Field(default="")
    episode_item_ids: list[str] = Field(default_factory=list)
    episode_titles: dict[str, str] = Field(default_factory=dict)
