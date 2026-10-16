"""
Request and response bodies for /api/curation.

OASIS's versions of these were lost from the copy this was ported from; they
are rebuilt from how its router, page script and tests use them, and those
tests (ported to tests/test_curation.py) are the contract.

``curator_name`` and ``reviewer`` are still accepted on the way in, so older
clients and scripts do not break, but they are ignored: who made a change is
whoever is signed in.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

ReviewState = Literal["unreviewed", "reviewed", "flagged"]
QualityDecision = Literal["accept", "reject", "manual_review"]
MAX_BULK = 5000


class DatasetMode(str, Enum):
    T3 = "T3"
    T1 = "T1"


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Projects ──────────────────────────────────────────────────────────────────

class CurationProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    dataset_mode: DatasetMode = DatasetMode.T3
    source_config: dict[str, Any] = Field(default_factory=dict)
    curator_name: Optional[str] = None  # ignored; the owner is the signed-in user


class CurationProjectResponse(_ORM):
    id: str
    name: str
    description: Optional[str] = None
    source_kind: str
    source_config: dict[str, Any] = Field(default_factory=dict)
    dataset_mode: str
    curator_name: Optional[str] = None
    owner_user_id: Optional[int] = None
    status: str
    item_count: int
    created_at: datetime
    updated_at: datetime
    my_role: Optional[str] = None


class TaxonomyNodeResponse(BaseModel):
    key: str
    tier: str
    code: Optional[str] = None
    name: str
    label: str
    codes: list[str]
    depth: int


# ── Imports ───────────────────────────────────────────────────────────────────

class CurationImportSource(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    root_uri: str = Field(min_length=1, max_length=1000)
    csv_uri: str = Field(min_length=1, max_length=1000)


class CurationImportCreate(BaseModel):
    sources: list[CurationImportSource] = Field(min_length=1, max_length=20)
    batch_size: int = Field(default=2000, ge=100, le=10000)


class CurationImportResponse(_ORM):
    id: str
    project_id: str
    status: str
    sources: list[dict[str, Any]]
    batch_size: int
    processed_rows: int
    imported_rows: int
    skipped_rows: int
    rejected_rows: int
    current_source: Optional[str] = None
    error_message: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ── Items ─────────────────────────────────────────────────────────────────────

class CurationItemCreate(BaseModel):
    source_key: str = Field(min_length=1, max_length=400)
    source_image_uri: str = Field(min_length=1, max_length=1000)
    crop_uri: str = Field(min_length=1, max_length=1000)
    file_name: str = Field(min_length=1, max_length=300)
    source_label: str = Field(min_length=1, max_length=100)
    row_px: Optional[int] = Field(default=None, ge=0)
    column_px: Optional[int] = Field(default=None, ge=0)
    crop_size: int = Field(default=224, ge=8, le=4096)
    region: Optional[str] = Field(default=None, max_length=120)
    site: Optional[str] = Field(default=None, max_length=120)
    survey_year: Optional[int] = None
    split: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("split")
    @classmethod
    def _original_split(cls, value):
        from curation.splits import normalize_split
        return normalize_split(value)


class CurationItemResponse(_ORM):
    id: int
    project_id: str
    source_key: str
    source_image_uri: str
    crop_uri: str
    file_name: str
    source_label: str
    current_label: str
    included: bool
    removed: bool = False
    source_missing: bool = False
    review_state: str
    row_px: Optional[int] = None
    column_px: Optional[int] = None
    crop_size: int
    region: Optional[str] = None
    island: Optional[str] = None
    site: Optional[str] = None
    survey_year: Optional[int] = None
    split: Optional[str] = None
    annotator: Optional[str] = None
    model_label: Optional[str] = None
    model_confidence: Optional[float] = None
    top_predictions: list[dict[str, Any]] = Field(default_factory=list)
    quality_score: Optional[int] = None
    quality_flags: list[str] = Field(default_factory=list)
    quality_decision: Optional[str] = None
    quality_reviewed_by: Optional[str] = None
    quality_reviewed_at: Optional[datetime] = None
    # Stored as metadata_json (``metadata`` is reserved on SQLAlchemy models);
    # the page script reads it as ``metadata``.
    metadata: dict[str, Any] = Field(
        default_factory=dict, validation_alias=AliasChoices("metadata_json", "metadata"),
    )
    revision: int
    updated_by_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("top_predictions", "quality_flags", "metadata", mode="before")
    @classmethod
    def _none_to_empty(cls, value, info):
        if value is None:
            return {} if info.field_name == "metadata" else []
        return value


class CurationItemPage(BaseModel):
    items: list[CurationItemResponse]
    next_cursor: Optional[int] = None
    returned: int


class CurationItemUpdate(BaseModel):
    expected_revision: int
    current_label: Optional[str] = Field(default=None, max_length=100)
    included: Optional[bool] = None
    review_state: Optional[ReviewState] = None
    row_px: Optional[int] = Field(default=None, ge=0)
    column_px: Optional[int] = Field(default=None, ge=0)
    crop_size: Optional[int] = Field(default=None, ge=32, le=1024)
    removed: Optional[bool] = None
    reason: Optional[str] = Field(default=None, max_length=4000)
    curator_name: Optional[str] = None  # ignored


class CurationItemRef(BaseModel):
    id: int
    expected_revision: Optional[int] = None


class CurationBulkUpdate(BaseModel):
    """Either bare ``item_ids`` or ``items`` carrying the revision the editor saw.

    With revisions, an item someone else changed in the meantime is left alone
    and reported back as a conflict; the rest are applied.
    """

    item_ids: list[int] = Field(default_factory=list, max_length=MAX_BULK)
    items: list[CurationItemRef] = Field(default_factory=list, max_length=MAX_BULK)
    included: Optional[bool] = None
    review_state: Optional[ReviewState] = None
    current_label: Optional[str] = Field(default=None, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=4000)
    curator_name: Optional[str] = None  # ignored

    @model_validator(mode="after")
    def _some_items(self):
        if not self.item_ids and not self.items:
            raise ValueError("Choose at least one item")
        return self

    def refs(self) -> dict[int, Optional[int]]:
        refs: dict[int, Optional[int]] = {item_id: None for item_id in self.item_ids}
        refs.update({ref.id: ref.expected_revision for ref in self.items})
        return refs


class CurationConflict(BaseModel):
    id: int
    revision: int
    updated_by_name: Optional[str] = None


class CurationBulkUpdateResponse(BaseModel):
    operation_id: str
    updated: int
    conflicts: list[CurationConflict] = Field(default_factory=list)


class CurationPointCreate(BaseModel):
    row_px: int = Field(ge=0)
    column_px: int = Field(ge=0)
    label: str = Field(min_length=1, max_length=100)
    crop_size: int = Field(default=224, ge=32, le=1024)
    reason: Optional[str] = Field(default=None, max_length=4000)
    curator_name: Optional[str] = None  # ignored


class CurationQualityReviewRequest(BaseModel):
    decision: QualityDecision
    score: Optional[int] = Field(default=None, ge=0, le=100)
    flags: list[str] = Field(default_factory=list, max_length=20)
    expected_revision: Optional[int] = None
    reviewer: Optional[str] = None  # ignored


class CurationQualityDecisionIn(BaseModel):
    item_id: int
    decision: QualityDecision
    score: Optional[int] = Field(default=None, ge=0, le=100)
    flags: list[str] = Field(default_factory=list, max_length=20)
    expected_revision: Optional[int] = None


class CurationQualityBulkRequest(BaseModel):
    """Many quality decisions at once, e.g. "Include recommended" in quality review."""

    scope: Literal["patch", "source"] = "patch"
    decisions: list[CurationQualityDecisionIn] = Field(min_length=1, max_length=500)


class CurationQualityBulkResponse(BaseModel):
    operation_id: str
    applied: int
    conflicts: list[CurationConflict] = Field(default_factory=list)


class CurationClassifyRequest(BaseModel):
    row_px: int = Field(ge=0)
    column_px: int = Field(ge=0)
    model_name: str = Field(min_length=1, max_length=300)
    crop_size: int = Field(default=224, ge=32, le=1024)
    radius: int = Field(default=0, ge=0, le=512)
    step: int = Field(default=112, ge=16, le=512)


class CurationClassifyResponse(BaseModel):
    model_name: str
    points: list[dict[str, Any]]


# ── Source images ─────────────────────────────────────────────────────────────

class CurationSourceImageSummary(BaseModel):
    source_image_uri: str
    image_name: str
    region: Optional[str] = None
    site: Optional[str] = None
    survey_year: Optional[int] = None
    annotation_count: int
    reviewed_count: int
    included_count: int
    labels: list[str]
    first_item_id: int


class CurationSourceImagePage(BaseModel):
    images: list[CurationSourceImageSummary]
    next_cursor: Optional[int] = None
    next_missing: int = 0
    returned: int


class CurationSourceImageDetail(BaseModel):
    first_item_id: int
    source_image_uri: str
    image_name: str
    region: Optional[str] = None
    site: Optional[str] = None
    survey_year: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    source_missing: bool = False
    items: list[CurationItemResponse]


# ── Facets, status, stats ─────────────────────────────────────────────────────

class FacetValue(BaseModel):
    value: str
    count: int


class CurationFilterFacets(BaseModel):
    regions: list[FacetValue]
    islands: list[FacetValue]
    years: list[FacetValue]
    sites: list[FacetValue]
    labels: list[FacetValue]
    annotators: list[FacetValue]


class StatusLabel(BaseModel):
    label: str
    total: int
    included: int
    reviewed: int
    source_images: int


class CuratorContribution(BaseModel):
    user_id: Optional[int] = None
    name: str
    changes: int
    items: int
    last_change_at: Optional[datetime] = None


class CurationDatasetStatus(BaseModel):
    total_items: int
    included_items: int
    excluded_items: int
    source_images: int
    reviewed_items: int
    classifier_predictions: int
    labels: list[StatusLabel]
    regions: list[FacetValue]
    curators: list[CuratorContribution] = Field(default_factory=list)


class CurationStatsResponse(BaseModel):
    total: int
    included: int
    excluded: int
    reviewed: int
    unreviewed: int
    flagged: int
    class_counts: dict[str, int]


class CurationEventResponse(_ORM):
    id: int
    item_id: Optional[int] = None
    operation_id: Optional[str] = None
    event_type: str
    user_id: Optional[int] = None
    curator_name: str
    legacy_actor: Optional[str] = None
    reason: Optional[str] = None
    previous_value: Optional[dict[str, Any]] = None
    new_value: Optional[dict[str, Any]] = None
    created_at: datetime


class CurationActivity(BaseModel):
    events: list[CurationEventResponse]
    curators: list[CuratorContribution]


# ── Confusion matrix ──────────────────────────────────────────────────────────

class MatrixCell(BaseModel):
    actual: str
    predicted: str
    count: int
    value: float


class MatrixAxis(BaseModel):
    label: str
    total: int
    correct: int
    metric: float


class CurationConfusionMatrix(BaseModel):
    matrix_source: str
    evaluation_id: Optional[str] = None
    split: Optional[str] = None
    available_splits: list[str] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    drilldown_kind: str
    actual_axis: str
    normalization: str
    scored_count: int
    unscored_count: int
    correct_count: int
    accuracy: float
    model: dict[str, Any]
    metrics: dict[str, Any] = Field(default_factory=dict)
    rows: list[MatrixAxis]
    columns: list[MatrixAxis]
    cells: list[MatrixCell]


class EvaluationPredictionPage(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: Optional[int] = None
    returned: int


class MatrixAssetPage(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: Optional[str] = None
    returned: int


# ── Batch edits on one source image ───────────────────────────────────────────

class GeometryEdit(BaseModel):
    op: Literal["move", "resize", "remove", "restore"]
    item_id: int
    expected_revision: int
    row_px: Optional[int] = Field(default=None, ge=0)
    column_px: Optional[int] = Field(default=None, ge=0)
    crop_size: Optional[int] = Field(default=None, ge=32, le=1024)


class NewPoint(BaseModel):
    row_px: int = Field(ge=0)
    column_px: int = Field(ge=0)
    crop_size: int = Field(default=224, ge=32, le=1024)
    label: str = Field(min_length=1, max_length=100)


class SourceEditBatch(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=4000)
    edits: list[GeometryEdit] = Field(default_factory=list, max_length=200)
    added: list[NewPoint] = Field(default_factory=list, max_length=200)


class SuggestRequest(BaseModel):
    item_id: int
    grid: Literal[3, 5] = 3
    spacing_px: int = Field(default=224, ge=32, le=512)
    crop_size: Optional[int] = Field(default=None, ge=32, le=1024)
    # The point's centre as the editor shows it, an unsaved move included; the saved one when absent.
    row_px: Optional[int] = Field(default=None, ge=0)
    column_px: Optional[int] = Field(default=None, ge=0)
    model_name: Optional[str] = Field(default=None, max_length=300)
