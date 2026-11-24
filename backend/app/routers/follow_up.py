from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import ApiError
from app.follow_up_schemas import FollowUpSummaryOut, ItemCreate, ItemOut, ItemsOut, ItemUpdate, PatientOut, PatientsOut
from app.models import Patient
from app.schemas import PatientCreate, PatientLink, SessionDetailOut
from app.serializers import session_detail_dict
from app.services import follow_up
from app.routers.assistant import IdempotencyKey
from app.services.creation import reserve_creation
from app.services.advice_state import lock_session
from app.services.session_service import get_session_or_404

router = APIRouter(tags=["患者与复诊"])


@router.post("/patients", response_model=PatientOut, status_code=201)
def add_patient(payload: PatientCreate, idempotency_key: IdempotencyKey = None, db: Session = Depends(get_db)):
    patient_id, replay = reserve_creation(db, "patient", idempotency_key, payload)
    if replay:
        return db.get(Patient, patient_id)
    patient = Patient(id=patient_id, **payload.model_dump())
    db.add(patient)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if payload.patient_ref and db.scalar(select(Patient.id).where(Patient.patient_ref == payload.patient_ref)):
            raise ApiError(409, "patient_ref_conflict", "该档案编号已存在，请核对后选用已有患者。") from None
        raise
    return patient


@router.get("/patients", response_model=PatientsOut)
def patients(query: str = Query(default="", max_length=120), limit: int = Query(default=30, ge=1, le=100),
             offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    statement = select(Patient)
    term = query.strip()
    if term:
        statement = statement.where(or_(Patient.id == term, Patient.patient_name.contains(term, autoescape=True),
                                        Patient.patient_ref.contains(term, autoescape=True)))
    return {"items": db.scalars(statement.order_by(Patient.created_at.desc(), Patient.id).offset(offset).limit(limit)).all()}


@router.put("/sessions/{session_id}/patient", response_model=SessionDetailOut)
def link_patient(session_id: str, payload: PatientLink, db: Session = Depends(get_db)):
    session = lock_session(db, session_id)
    if session.patient_id == payload.patient_id:
        return session_detail_dict(session)
    if session.patient_id:
        raise ApiError(409, "patient_already_linked", "会话已关联其他档案。")
    if session.transcript and not session.confirmed_version:
        raise ApiError(409, "session_in_progress", "请在转录前或病历确认后关联档案。")
    patient = db.get(Patient, payload.patient_id)
    if patient is None:
        raise ApiError(404, "patient_not_found", "患者档案不存在。")
    session.patient_id = patient.id
    db.commit()
    return session_detail_dict(session)


@router.get("/sessions/{session_id}/follow-up-summary", response_model=FollowUpSummaryOut)
def summary(session_id: str, db: Session = Depends(get_db)):
    return follow_up.follow_up_summary(db, get_session_or_404(db, session_id))


@router.get("/sessions/{session_id}/items", response_model=ItemsOut)
def items(session_id: str, request: Request, db: Session = Depends(get_db)):
    return {"items": follow_up.list_items(db, get_session_or_404(db, session_id),
                                          advisor_enabled=request.app.state.settings.live_assistant_enabled)}


@router.post("/sessions/{session_id}/items", response_model=ItemOut, status_code=201)
def add_item(session_id: str, payload: ItemCreate, db: Session = Depends(get_db)):
    return follow_up.create_item(db, session_id, payload)


@router.patch("/sessions/{session_id}/items/{item_id}", response_model=ItemOut)
def edit_item(session_id: str, item_id: str, payload: ItemUpdate, db: Session = Depends(get_db)):
    return follow_up.update_item(db, session_id, item_id, payload)
