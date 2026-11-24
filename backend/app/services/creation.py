import hashlib

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import CreationRequest, new_id


def reserve_creation(db: Session, kind: str, key: str | None, payload: BaseModel) -> tuple[str, bool]:
    """Reserve before other writes. The reservation and new record commit together."""
    resource_id = new_id()
    if key is None:
        return resource_id, False
    digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    db.add(CreationRequest(kind=kind, request_key=key, payload_hash=digest, resource_id=resource_id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.get(CreationRequest, (kind, key))
        if existing is None:
            raise
        if existing.payload_hash != digest:
            raise ApiError(409, "idempotency_conflict", "该创建请求编号已用于另一组资料。") from None
        return existing.resource_id, True
    return resource_id, False
