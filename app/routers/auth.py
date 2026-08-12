"""认证：register / login / logout / me。OIDC-ready（AuthProvider 抽象可替换）。"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..deps import DB, CurrentUser, settings
from ..models import Organization, Role, User, UserSession
from ..schemas import AuthOut, LoginIn, RegisterIn, UserOut
from ..security import generate_token, hash_password, hash_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _create_session(db, user_id: str) -> tuple[str, str]:
    token = generate_token()
    session = UserSession(
        id=str(uuid.uuid4()),
        user_id=user_id,
        token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(session)
    db.commit()
    return token, session.id


@router.post("/register", response_model=AuthOut, status_code=201)
def register(payload: RegisterIn, db: DB):
    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise HTTPException(409, "email already registered")

    organization_id = None
    if payload.organization_name:
        org = Organization(id=str(uuid.uuid4()), name=payload.organization_name)
        db.add(org)
        db.flush()
        organization_id = org.id

    user = User(
        id=str(uuid.uuid4()),
        email=payload.email,
        username=payload.username,
        password_hash=hash_password(payload.password, settings.password_pepper),
        role=Role.USER.value,
        organization_id=organization_id,
    )
    db.add(user)
    db.flush()
    if organization_id:
        org_row = db.get(Organization, organization_id)
        if org_row is not None:
            org_row.owner_id = user.id
    db.commit()
    db.refresh(user)

    token, _ = _create_session(db, user.id)
    return AuthOut(token=token, user=UserOut.model_validate(user))


@router.post("/login", response_model=AuthOut)
def login(payload: LoginIn, db: DB):
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash, settings.password_pepper):
        raise HTTPException(401, "invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "account disabled")
    token, _ = _create_session(db, user.id)
    return AuthOut(token=token, user=UserOut.model_validate(user))


@router.post("/logout", status_code=204)
def logout(db: DB, user: CurrentUser):
    # 简单实现：注销当前用户全部会话（demo 语义）。
    from ..models import UserSession as US

    sessions = db.scalars(select(US).where(US.user_id == user.id, US.revoked.is_(False))).all()
    for s in sessions:
        s.revoked = True
    db.commit()


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user
