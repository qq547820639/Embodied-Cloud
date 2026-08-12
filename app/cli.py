"""管理 CLI：bootstrap-admin / list-gpus / show-usage。

用法：
  .venv/bin/python -m app.cli bootstrap-admin --email admin@example.com --password 'xxxx'
  .venv/bin/python -m app.cli list-gpus
  .venv/bin/python -m app.cli show-usage --email admin@example.com
"""

import argparse
import sys
import uuid
from datetime import UTC, datetime, timedelta

from .deps import SessionFactory, engine, scheduler, settings
from .models import Organization, Role, User, UserSession
from .security import generate_token, hash_password, hash_token


def bootstrap_admin(email: str, password: str, username: str | None = None) -> None:
    with SessionFactory() as db:
        from sqlalchemy import select

        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            if existing.role != Role.ADMIN.value:
                existing.role = Role.ADMIN.value
                db.commit()
                print(f"已提升为 admin: {email}")
            else:
                print(f"已是 admin: {email}")
            return
        org = Organization(id=str(uuid.uuid4()), name="EmbodiedCloud")
        db.add(org)
        db.flush()
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            username=username or email.split("@")[0],
            password_hash=hash_password(password, settings.password_pepper),
            role=Role.ADMIN.value,
            organization_id=org.id,
        )
        db.add(user)
        db.flush()
        org.owner_id = user.id
        db.commit()
        print(f"admin 已创建: {email} (role=admin)")


def list_gpus() -> None:
    with SessionFactory() as db:
        gpus = scheduler.list_gpus(db)
        if not gpus:
            print("(empty inventory)")
            return
        for g in gpus:
            print(f"{g.gpu_uuid}  {g.model:<28} {g.status:<12} mem={g.memory_total} ws={g.workspace_id or '-'}")


def show_usage(email: str) -> None:
    with SessionFactory() as db:
        from sqlalchemy import select

        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"user not found: {email}", file=sys.stderr)
            sys.exit(1)
        from .services.ledger import CreditLedgerService

        ledger = CreditLedgerService(SessionFactory)
        balance = ledger.balance(db, user.id)
        print(f"user={user.email} role={user.role} balance={balance}")


def make_session(email: str) -> None:
    """为已存在用户签发一次性 session token（运维/测试用）。"""
    with SessionFactory() as db:
        from sqlalchemy import select

        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"user not found: {email}", file=sys.stderr)
            sys.exit(1)
        token = generate_token()
        session = UserSession(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours),
        )
        db.add(session)
        db.commit()
        print(token)


def _ensure_tables() -> None:
    """开发便利：表不存在时自动建表；生产用 alembic 迁移。"""
    from .db import Base

    Base.metadata.create_all(engine)


def main() -> None:
    _ensure_tables()
    parser = argparse.ArgumentParser(prog="embodiedcloud-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_admin = sub.add_parser("bootstrap-admin")
    p_admin.add_argument("--email", required=True)
    p_admin.add_argument("--password", required=True)
    p_admin.add_argument("--username")

    sub.add_parser("list-gpus")

    p_usage = sub.add_parser("show-usage")
    p_usage.add_argument("--email", required=True)

    p_session = sub.add_parser("make-session")
    p_session.add_argument("--email", required=True)

    args = parser.parse_args()
    if args.cmd == "bootstrap-admin":
        bootstrap_admin(args.email, args.password, args.username)
    elif args.cmd == "list-gpus":
        list_gpus()
    elif args.cmd == "show-usage":
        show_usage(args.email)
    elif args.cmd == "make-session":
        make_session(args.email)


if __name__ == "__main__":
    main()
