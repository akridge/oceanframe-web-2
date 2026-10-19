"""
Curation tables.

Ported from OASIS's ``models/classification_curation.py`` with these changes:

* Sized strings throughout, JSON in :class:`JSONText` — so they compile on Oracle.
* ``island`` and ``annotator`` are real columns. OASIS kept them in the JSON
  metadata and filtered with ``json_extract``, which SQLite has and Oracle does
  not spell the same way; they are filtered and faceted on constantly, so they
  belong in indexed columns anyway.
* Who did it: ``created_by_user_id`` / ``updated_by_user_id`` on items,
  ``user_id`` on events, ``owner_user_id`` on projects. ``curator_name`` is kept
  as the display name at the time, and ``legacy_actor`` holds names carried
  over from an OASIS database where the person had no account.
* An event's ``item_id`` is SET NULL rather than CASCADE on delete, so the
  audit trail outlives the item it describes. Deleting a project still takes
  its whole history with it, by design.
* OASIS declared a ``curation_dataset_versions`` table but never used it; the
  versions here (below) are a working version of that idea, with a change log.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Identity, Index, Integer, Text, UniqueConstraint, false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from appdb.base import Base, JSONText, Str, utcnow


def new_id() -> str:
    return str(uuid.uuid4())


# Column sizes, shared with the import paths that must fit values into them.
URI_LEN = 1000
LABEL_LEN = 100
NAME_LEN = 200
PLACE_LEN = 120


class CurationProject(Base):
    __tablename__ = "curation_projects"

    id: Mapped[str] = mapped_column(Str(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Str(NAME_LEN), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(Str(40), nullable=False, default="regional_csv")
    source_config: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    curator_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Str(20), nullable=False, default="draft", index=True)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    items: Mapped[list["CurationItem"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True,
    )
    events: Mapped[list["CurationEvent"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True,
    )
    imports: Mapped[list["CurationImportOperation"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True,
    )

    @property
    def dataset_mode(self) -> str:
        return str((self.source_config or {}).get("dataset_mode") or "T3").upper()


class CurationItem(Base):
    __tablename__ = "curation_items"
    __table_args__ = (
        UniqueConstraint("project_id", "source_key", name="uq_curation_item_source"),
        Index("ix_cur_items_project_id", "project_id", "id"),
        Index("ix_cur_items_label", "project_id", "current_label"),
        Index("ix_cur_items_state", "project_id", "review_state"),
        Index("ix_cur_items_region", "project_id", "region"),
        Index("ix_cur_items_island", "project_id", "island"),
        Index("ix_cur_items_site", "project_id", "site"),
        Index("ix_cur_items_year", "project_id", "survey_year"),
        Index("ix_cur_items_annotator", "project_id", "annotator"),
        Index("ix_cur_items_included", "project_id", "included"),
        Index("ix_cur_items_included_label", "project_id", "included", "current_label"),
        Index("ix_cur_items_source_image", "project_id", "source_image_uri", "id"),
        Index("ix_cur_items_current_model", "project_id", "current_label", "model_label"),
        Index("ix_cur_items_source_model", "project_id", "source_label", "model_label"),
        Index("ix_cur_items_source_file", "project_id", "source_label", "file_name"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("curation_projects.id", ondelete="CASCADE"), nullable=False,
    )
    source_key: Mapped[str] = mapped_column(Str(400), nullable=False)
    source_image_uri: Mapped[str] = mapped_column(Str(URI_LEN), nullable=False)
    crop_uri: Mapped[str] = mapped_column(Str(URI_LEN), nullable=False)
    file_name: Mapped[str] = mapped_column(Str(300), nullable=False)
    source_label: Mapped[str] = mapped_column(Str(LABEL_LEN), nullable=False)
    current_label: Mapped[str] = mapped_column(Str(LABEL_LEN), nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Taken out of the dataset on the source image; restorable, never deleted.
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # The source image is not in its bucket (curation/availability.py).
    source_missing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    review_state: Mapped[str] = mapped_column(Str(20), nullable=False, default="unreviewed")
    row_px: Mapped[int | None] = mapped_column(Integer)
    column_px: Mapped[int | None] = mapped_column(Integer)
    crop_size: Mapped[int] = mapped_column(Integer, nullable=False, default=224)
    region: Mapped[str | None] = mapped_column(Str(PLACE_LEN))
    island: Mapped[str | None] = mapped_column(Str(PLACE_LEN))
    site: Mapped[str | None] = mapped_column(Str(PLACE_LEN))
    survey_year: Mapped[int | None] = mapped_column(Integer)
    split: Mapped[str | None] = mapped_column(Str(10))
    annotator: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    model_label: Mapped[str | None] = mapped_column(Str(LABEL_LEN))
    model_confidence: Mapped[float | None] = mapped_column(Float)
    top_predictions: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    quality_score: Mapped[int | None] = mapped_column(Integer)
    quality_flags: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    quality_decision: Mapped[str | None] = mapped_column(Str(20))
    quality_reviewed_by: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    quality_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    metadata_json: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    updated_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    project: Mapped[CurationProject] = relationship(back_populates="items")


class CurationEvent(Base):
    """Append-only audit trail: one row per change, with before and after."""

    __tablename__ = "curation_events"
    __table_args__ = (
        Index("ix_cur_events_project_id", "project_id", "id"),
        Index("ix_cur_events_item_id", "item_id", "id"),
        Index("ix_cur_events_user", "project_id", "user_id"),
        Index("ix_cur_events_commit", "project_id", "user_id", "change_set_id"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("curation_projects.id", ondelete="CASCADE"), nullable=False,
    )
    item_id: Mapped[int | None] = mapped_column(ForeignKey("curation_items.id", ondelete="SET NULL"))
    operation_id: Mapped[str | None] = mapped_column(Str(36), index=True)
    event_type: Mapped[str] = mapped_column(Str(40), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    curator_name: Mapped[str] = mapped_column(Str(NAME_LEN), nullable=False)
    legacy_actor: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    reason: Mapped[str | None] = mapped_column(Text)
    previous_value: Mapped[dict | None] = mapped_column(JSONText)
    new_value: Mapped[dict | None] = mapped_column(JSONText)
    # Set when the author commits; NULL while the change is still "uncommitted".
    # No database FK: the column is added to existing tables by a migration.
    change_set_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    project: Mapped[CurationProject] = relationship(back_populates="events")


class CurationProjectMember(Base):
    """Who works on a project, and as what. App-wide admins need no row."""

    __tablename__ = "curation_project_members"

    project_id: Mapped[str] = mapped_column(ForeignKey("curation_projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("of_users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(Str(10), nullable=False, default="curator")
    added_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class CurationClaim(Base):
    """Items a curator has open right now (curation/claims.py), so the others' queues skip them.

    One row per item, held by one browser tab (``holder``) until it lets go or ``expires_at`` passes."""

    __tablename__ = "curation_claims"
    __table_args__ = (Index("ix_cur_claims_holder", "project_id", "holder"),)

    project_id: Mapped[str] = mapped_column(ForeignKey("curation_projects.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder: Mapped[str] = mapped_column(Str(64), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("of_users.id", ondelete="CASCADE"), nullable=False)
    user_name: Mapped[str] = mapped_column(Str(NAME_LEN), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CurationQualityMetric(Base):
    """A patch's image-quality score, worked out once on the server (curation/quality.py) so Quality
    review can sort and filter every patch, not only those the browser has scored. ``signature`` is
    the geometry it was scored for: a moved or resized point is scored again."""

    __tablename__ = "curation_quality_metrics"
    __table_args__ = (Index("ix_cur_quality_score", "project_id", "score"),)

    item_id: Mapped[int] = mapped_column(ForeignKey("curation_items.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("curation_projects.id", ondelete="CASCADE"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    suggestion: Mapped[str] = mapped_column(Str(20), nullable=False)  # accept | manual_review | reject
    flags: Mapped[str] = mapped_column(Str(200), nullable=False)  # ",blurry,too_dark," (delimited, for LIKE)
    sharpness: Mapped[int | None] = mapped_column(Integer)
    brightness: Mapped[int | None] = mapped_column(Integer)
    contrast: Mapped[int | None] = mapped_column(Integer)
    color_cast: Mapped[float | None] = mapped_column(Float)
    clipped: Mapped[int | None] = mapped_column(Integer)
    signature: Mapped[str] = mapped_column(Str(400), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class CurationPresence(Base):
    """One open curation tab: who, in which project, looking at what, last heard from when (curation/presence.py)."""

    __tablename__ = "curation_presence"
    __table_args__ = (Index("ix_cur_presence_seen", "last_seen"),)

    holder: Mapped[str] = mapped_column(Str(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("curation_projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("of_users.id", ondelete="CASCADE"), nullable=False)
    user_name: Mapped[str] = mapped_column(Str(NAME_LEN), nullable=False)
    view: Mapped[str] = mapped_column(Str(20), nullable=False)
    detail: Mapped[str | None] = mapped_column(Str(300))
    last_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CurationChangeSet(Base):
    """One person's edits, committed together under a message."""

    __tablename__ = "curation_change_sets"
    __table_args__ = (Index("ix_cur_change_sets_project", "project_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("curation_projects.id", ondelete="CASCADE"), nullable=False)
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    author_name: Mapped[str] = mapped_column(Str(NAME_LEN), nullable=False)
    committed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    committed_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    message: Mapped[str] = mapped_column(Str(500), nullable=False)
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class CurationRelease(Base):
    """One dataset built from published versions of several projects."""

    __tablename__ = "curation_releases"
    __table_args__ = (UniqueConstraint("name", "number", name="uq_cur_release_number"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Str(200), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_mode: Mapped[str] = mapped_column(Str(10), nullable=False)
    status: Mapped[str] = mapped_column(Str(20), nullable=False, default="building")
    notes: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    statistics: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_uri: Mapped[str | None] = mapped_column(Str(URI_LEN))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    created_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)


class CurationImportOperation(Base):
    __tablename__ = "curation_import_operations"

    id: Mapped[str] = mapped_column(Str(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("curation_projects.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    status: Mapped[str] = mapped_column(Str(20), nullable=False, default="queued", index=True)
    sources: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_source: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    created_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    project: Mapped[CurationProject] = relationship(back_populates="imports")


class CurationImportMap(Base):
    """Rows brought over from an OASIS database, so re-running the import is safe."""

    __tablename__ = "curation_import_map"
    __table_args__ = (
        UniqueConstraint("source_db", "kind", "legacy_id", name="uq_curation_import_map"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    source_db: Mapped[str] = mapped_column(Str(64), nullable=False)  # fingerprint of the OASIS file
    kind: Mapped[str] = mapped_column(Str(20), nullable=False)       # project | item | event | import
    legacy_id: Mapped[str] = mapped_column(Str(64), nullable=False)
    new_id: Mapped[str] = mapped_column(Str(64), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class CurationDatasetVersion(Base):
    """A published, immutable snapshot of a project's included items.

    Versions are numbered per project (v1, v2, …). Each records what changed
    since the previous one — the in/out/relabel counts per class — and who
    contributed, so a dataset's history can be read without diffing files.
    """

    __tablename__ = "curation_dataset_versions"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_curation_project_version"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("curation_projects.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Str(20), nullable=False, default="building")  # building|published|failed|interrupted
    notes: Mapped[str | None] = mapped_column(Text)
    # Highest curation_events.id covered: the next version's contributors are
    # the events after this one.
    event_watermark: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    split_seed: Mapped[int] = mapped_column(Integer, nullable=False, default=42)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    statistics: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)   # counts by class/split
    changes: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)      # diff vs previous
    contributors: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    output_uri: Mapped[str | None] = mapped_column(Str(URI_LEN))
    artifacts: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)    # file names, image export state
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    created_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)


class CurationVersionItem(Base):
    """Membership of one version: which item, under which label, in which split.

    Kept narrow on purpose — one row per included item per version is the
    bulk of the database as versions accumulate.
    """

    __tablename__ = "curation_version_items"

    version_id: Mapped[int] = mapped_column(
        ForeignKey("curation_dataset_versions.id", ondelete="CASCADE"), primary_key=True,
    )
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(Str(LABEL_LEN), nullable=False)
    split: Mapped[str] = mapped_column(Str(10), nullable=False)


class CurationVersionInput(Base):
    __tablename__ = "curation_version_inputs"

    version_id: Mapped[int] = mapped_column(ForeignKey("curation_dataset_versions.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(Str(LABEL_LEN), nullable=False)
    split: Mapped[str] = mapped_column(Str(10), nullable=False)
    source_key: Mapped[str] = mapped_column(Str(400), nullable=False)
    source_label: Mapped[str] = mapped_column(Str(LABEL_LEN), nullable=False)
    crop_uri: Mapped[str] = mapped_column(Str(URI_LEN), nullable=False)
    source_image_uri: Mapped[str] = mapped_column(Str(URI_LEN), nullable=False)
    file_name: Mapped[str] = mapped_column(Str(300), nullable=False)
    row_px: Mapped[int | None] = mapped_column(Integer)
    column_px: Mapped[int | None] = mapped_column(Integer)
    crop_size: Mapped[int] = mapped_column(Integer, nullable=False)
    region: Mapped[str | None] = mapped_column(Str(PLACE_LEN))
    island: Mapped[str | None] = mapped_column(Str(PLACE_LEN))
    site: Mapped[str | None] = mapped_column(Str(PLACE_LEN))
    survey_year: Mapped[int | None] = mapped_column(Integer)
    annotator: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    updated_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    metadata_json: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)

    @property
    def id(self) -> int:
        return self.item_id


class CurationClassifier(Base):
    __tablename__ = "curation_classifiers"

    id: Mapped[str] = mapped_column(Str(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Str(NAME_LEN), nullable=False)
    sha256: Mapped[str] = mapped_column(Str(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Str(20), nullable=False, default="staged")
    class_names: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class CurationEvaluationRun(Base):
    __tablename__ = "curation_evaluation_runs"
    __table_args__ = (Index("ix_cur_eval_project_created", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(Str(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("curation_projects.id", ondelete="CASCADE"), nullable=False)
    version_id: Mapped[int] = mapped_column(ForeignKey("curation_dataset_versions.id", ondelete="CASCADE"), nullable=False)
    classifier_id: Mapped[str] = mapped_column(ForeignKey("curation_classifiers.id"), nullable=False)
    status: Mapped[str] = mapped_column(Str(20), nullable=False, default="queued", index=True)
    specification: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    provenance: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    results: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scored_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class CurationEvaluationPrediction(Base):
    __tablename__ = "curation_eval_predictions"
    __table_args__ = (
        UniqueConstraint("run_id", "item_id", name="uq_cur_eval_prediction_item"),
        Index("ix_cur_eval_prediction_cell", "run_id", "split", "actual_label", "predicted_label"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("curation_evaluation_runs.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_key: Mapped[str] = mapped_column(Str(400), nullable=False)
    split: Mapped[str] = mapped_column(Str(10), nullable=False)
    actual_label: Mapped[str] = mapped_column(Str(LABEL_LEN), nullable=False)
    predicted_label: Mapped[str] = mapped_column(Str(LABEL_LEN), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    top_predictions: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    image_sha256: Mapped[str] = mapped_column(Str(64), nullable=False)
    file_name: Mapped[str] = mapped_column(Str(300), nullable=False)


class CurationHubEntry(Base):
    """A Hugging Face dataset or model, pinned to the commit it was registered at."""

    __tablename__ = "curation_hub_entries"
    __table_args__ = (
        UniqueConstraint("kind", "repo_id", "revision", name="uq_cur_hub_entry"),
        Index("ix_cur_hub_kind", "kind", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Str(10), nullable=False)
    repo_id: Mapped[str] = mapped_column(Str(200), nullable=False)
    revision: Mapped[str] = mapped_column(Str(40), nullable=False)
    requested_ref: Mapped[str] = mapped_column(Str(100), nullable=False, default="main")
    task: Mapped[str | None] = mapped_column(Str(40))
    runnable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    card: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    files: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    options: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    classifier_id: Mapped[str | None] = mapped_column(ForeignKey("curation_classifiers.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Str(20), nullable=False, default="registered")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    created_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
