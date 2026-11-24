from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas import ConfirmedFieldOut, FieldKey, SegmentOut, TranscriptOut


Actor = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
ItemStatus = Literal["open", "asked", "resolved", "dismissed"]


class PatientOut(BaseModel):
    id: str
    patient_name: str
    patient_ref: str | None


class PatientsOut(BaseModel):
    items: list[PatientOut]


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    actor: Actor
    carry_forward: bool = False


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    actor: Actor
    status: ItemStatus
    result: str | None = Field(default=None, max_length=1000)
    carry_forward: bool | None = None


class ActionOut(BaseModel):
    session_id: str
    actor: str
    revision: int
    status: ItemStatus
    carry_forward: bool
    result: str
    created_at: str


class ItemOut(BaseModel):
    id: str
    origin_session_id: str
    title: str
    origin: Literal["doctor", "assistant"]
    status: ItemStatus
    revision: int
    carry_forward: bool
    result: str
    created_at: str
    actions: list[ActionOut]


class ItemsOut(BaseModel):
    items: list[ItemOut]


class PriorRecordOut(BaseModel):
    session_id: str
    record_id: str
    version: int
    visit_started_at: str
    confirmed_at: str
    fields: list[ConfirmedFieldOut]


class FieldComparisonOut(BaseModel):
    field_key: str
    previous_value: str
    current_value: str | None
    state: Literal["awaiting_draft", "not_mentioned", "same_text", "different_text"]
    current_sources: list[SegmentOut]


class FollowUpSummaryOut(BaseModel):
    session_id: str
    patient_id: str | None
    previous_record: PriorRecordOut | None
    pending_items: list[ItemOut]
    comparisons: list[FieldComparisonOut]


class HistorySourceOut(BaseModel):
    session_id: str
    record_id: str
    version: int
    confirmed_at: str
    field_key: FieldKey
    label: str
    value: str


class AdviceSuggestionOut(BaseModel):
    id: str
    item_id: str | None = None
    category: Literal["follow_up", "clarification", "diagnostic_consideration", "examination", "treatment", "red_flag"]
    title: str
    suggestion: str
    rationale: str
    source_segment_ids: list[str]
    sources: list[SegmentOut]
    history_sources: list[HistorySourceOut] = Field(default_factory=list)
    history_field_keys: list[FieldKey] = Field(default_factory=list)
    existing_item_id: str | None = None
    topic_key: str | None = None
    missing_information: list[str]
    basis_kind: str
    requires_clinician_review: bool


class AdviceErrorOut(BaseModel):
    code: str
    message: str


class AdviceRunOut(BaseModel):
    id: str
    session_id: str
    transcript_id: str
    transcript_revision: int
    transcript_state: str
    provider: str
    status: str
    suggestions: list[AdviceSuggestionOut]
    metrics: dict
    started_at: str
    finished_at: str | None
    error: AdviceErrorOut | None
    requires_clinician_review: bool
    medical_references_retrieved: bool


class AdviceStatusOut(BaseModel):
    enabled: bool
    available: bool
    worker_enabled: bool
    transcript_revision: int | None
    transcript_state: str | None
    pending: bool
    active_run_id: str | None
    run_count: int
    current: AdviceRunOut | None
    previous: AdviceRunOut | None
    error: AdviceErrorOut | None
    notice: str


class AdviceHistoryOut(BaseModel):
    items: list[AdviceRunOut]


class LiveSnapshotOut(BaseModel):
    transcript: TranscriptOut | None
    assistant: AdviceStatusOut
    items: list[ItemOut]
