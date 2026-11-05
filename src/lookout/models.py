"""Public request/response models. These define the /v1 contract."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Mode = Literal["fast", "deep"]
TimeRange = Literal["day", "week", "month", "year", "any"]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    objective: str | None = Field(
        default=None,
        max_length=1000,
        description="What the agent is trying to accomplish. Used to rank and excerpt results.",
    )
    mode: Mode = "fast"
    max_results: int = Field(default=5, ge=1, le=20)
    max_chars_per_result: int = Field(default=2000, ge=100, le=10000)
    time_range: TimeRange = "any"
    include_domains: list[str] = Field(default_factory=list, max_length=50)
    exclude_domains: list[str] = Field(default_factory=list, max_length=50)
    include_raw_content: bool = False


class SearchResult(BaseModel):
    title: str
    url: str
    domain: str
    published_at: str | None = None
    score: float
    excerpts: list[str] = Field(
        description="Passages most relevant to the query and objective. Total length <= max_chars_per_result."
    )
    content: str | None = Field(
        default=None,
        description="Cleaned page text as markdown, truncated to max_chars_per_result. "
        "Only in deep mode with include_raw_content=true.",
    )
    fetched_at: datetime | None = None
    fetch_status: str | None = Field(
        default=None,
        description="Deep mode only. 'ok', or why the page could not be read "
        "(e.g. timeout, http_403, robots_disallowed, blocked:non_public_address, empty_content). "
        "When not 'ok', excerpts fall back to the search snippet.",
    )


class Usage(BaseModel):
    credits: int


class SearchResponse(BaseModel):
    request_id: str
    query: str
    results: list[SearchResult]
    usage: Usage
    latency_ms: int
    cached: bool = False


class UsageSummary(BaseModel):
    key_name: str
    key_prefix: str
    period_start: datetime = Field(description="Start of the current billing month, UTC.")
    calls: int
    credits_used: int
    credit_limit: int


class AdminKey(BaseModel):
    id: int
    name: str
    key_prefix: str
    is_admin: bool
    active: bool
    monthly_credit_limit: int
    calls_this_month: int
    credits_this_month: int
    last_used_at: datetime | None
    created_at: datetime


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    monthly_credit_limit: int | None = Field(default=None, ge=1, le=100_000_000)
    is_admin: bool = False


class CreatedKey(BaseModel):
    key: AdminKey
    api_key: str = Field(description="The plaintext key. Returned once; only a hash is stored.")


class UpdateKeyRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    monthly_credit_limit: int | None = Field(default=None, ge=1, le=100_000_000)


class DailyUsage(BaseModel):
    date: str
    calls: int
    credits: int
    deep_credits: int


class AdminUsage(BaseModel):
    period_start: datetime
    calls: int
    credits: int
    cache_hits: int
    keys_used: int
    fast_calls: int
    deep_calls: int
    active_keys: int
    daily: list[DailyUsage]


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
