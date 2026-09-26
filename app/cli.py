"""管理 CLI：bootstrap-admin / list-gpus / show-usage / record-image-digest。

用法：
  .venv/bin/python -m app.cli bootstrap-admin --email admin@example.com --password 'xxxx'
  .venv/bin/python -m app.cli list-gpus
  .venv/bin/python -m app.cli show-usage --email admin@example.com
  .venv/bin/python -m app.cli record-image-digest --template-id cartpole --version 0.1.0 \
      --digest sha256:<64 hex>
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


def record_image_digest(template_id: str, version: str, digest: str) -> int:
    """把构建/registry 报出的镜像 digest 回填进 `TemplateVersion.image_digest`。

    这一列自 v0.4 起就建模、却始终 0 处写入也 0 处读取（本轮普查读数）。现在它有
    读者了（`services/image_ref.py` 在 workspace 快照时钉引用），缺的就是这个写入入口。

    退码约定：0 写入或幂等；2 前提不成立（版本不存在 / 没有 image）；
    3 该行已钉在**另一个** digest 上——released 版本不可变，同 tag 换了内容
    意味着要发布新版本，而不是就地改写这条记录。
    """
    from sqlalchemy import select

    from .models import TemplateVersion
    from .services.image_ref import DIGEST_RE, ImageDigestError

    if not DIGEST_RE.match(digest):
        raise ImageDigestError(f"digest 形制不对（要 sha256: + 64 位十六进制）：{digest!r}")
    with SessionFactory() as db:
        tv = db.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version == version,
            )
        )
        if tv is None:
            print(f"未找到模板版本 {template_id}@{version}", file=sys.stderr)
            return 2
        if not tv.image:
            print(f"{template_id}@{version} 没有 image，钉无可钉", file=sys.stderr)
            return 2
        if tv.image_digest and tv.image_digest != digest:
            print(
                f"拒改：{template_id}@{version} 已钉在 {tv.image_digest}，"
                "released 版本不可变，请发布新版本",
                file=sys.stderr,
            )
            return 3
        if tv.image_digest == digest:
            print(f"unchanged {template_id}@{version} image_digest={digest}")
            return 0
        tv.image_digest = digest
        db.commit()
        print(f"recorded {template_id}@{version} image_digest={digest}")
        return 0


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

    p_digest = sub.add_parser("record-image-digest")
    p_digest.add_argument("--template-id", required=True)
    p_digest.add_argument("--version", required=True)
    p_digest.add_argument("--digest", required=True, help="镜像 manifest 的 sha256:… 摘要")

    args = parser.parse_args()
    if args.cmd == "bootstrap-admin":
        bootstrap_admin(args.email, args.password, args.username)
    elif args.cmd == "list-gpus":
        list_gpus()
    elif args.cmd == "show-usage":
        show_usage(args.email)
    elif args.cmd == "make-session":
        make_session(args.email)
    elif args.cmd == "record-image-digest":
        raise SystemExit(
            record_image_digest(args.template_id, args.version, args.digest)
        )


if __name__ == "__main__":
    main()
