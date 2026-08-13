"""CLI 命令单元测试：bootstrap-admin / list-gpus / show-usage / make-session。

纯单测：monkeypatch `app.cli.SessionFactory` / scheduler / CreditLedgerService，
不依赖真实 DB。
"""

import sys
from types import SimpleNamespace

import pytest

import app.cli as cli
from app.models import Role
from app.security import hash_token


class _FakeDB:
    """最小 DB session 替身：只实现 cli 命令用到的 scalar/add/flush/commit。"""

    def __init__(self, scalar_result=None):
        self.scalar_result = scalar_result
        self.added = []
        self.committed = 0
        self.flushed = 0

    def scalar(self, stmt):
        # 忽略真实 SQLAlchemy Select，返回预设结果（user / None）
        return self.scalar_result

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed += 1

    def commit(self):
        self.committed += 1


class _FakeSessionFactory:
    """SessionFactory 替身：`SessionFactory()` 作为上下文管理器返回预设 db。"""

    def __init__(self, db):
        self._db = db

    def __call__(self):
        return self

    def __enter__(self):
        return self._db

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeScheduler:
    def __init__(self, gpus):
        self._gpus = gpus

    def list_gpus(self, db):
        return self._gpus


def _patch_ledger(monkeypatch, balance_value):
    """替换 app.services.ledger.CreditLedgerService（show_usage 内按需导入）。"""

    class _FakeLedger:
        def __init__(self, session_factory):
            pass

        def balance(self, db, user_id):
            return balance_value

    monkeypatch.setattr("app.services.ledger.CreditLedgerService", _FakeLedger)


def _user(email="u@example.com", user_id="uid-1", role=Role.USER.value):
    return SimpleNamespace(id=user_id, email=email, role=role)


# ---------------------------------------------------------------------------
# bootstrap-admin
# ---------------------------------------------------------------------------


def test_bootstrap_admin_creates_new_admin(monkeypatch, capsys):
    db = _FakeDB(scalar_result=None)
    monkeypatch.setattr(cli, "SessionFactory", _FakeSessionFactory(db))
    monkeypatch.setattr(cli, "hash_password", lambda pw, pepper: f"hashed:{pw}")

    cli.bootstrap_admin("admin@example.com", "s3cret")

    out = capsys.readouterr().out
    assert "admin 已创建" in out
    assert db.committed == 1
    assert db.flushed == 2
    assert len(db.added) == 2  # org + user
    org, user = db.added
    assert org.name == "EmbodiedCloud"
    assert user.email == "admin@example.com"
    assert user.role == Role.ADMIN.value
    assert user.password_hash == "hashed:s3cret"
    assert org.owner_id == user.id


def test_bootstrap_admin_promotes_existing_non_admin(monkeypatch, capsys):
    existing = _user(role=Role.USER.value)
    db = _FakeDB(scalar_result=existing)
    monkeypatch.setattr(cli, "SessionFactory", _FakeSessionFactory(db))

    cli.bootstrap_admin("u@example.com", "pw")

    assert "已提升为 admin" in capsys.readouterr().out
    assert existing.role == Role.ADMIN.value
    assert db.committed == 1


def test_bootstrap_admin_already_admin_is_noop(monkeypatch, capsys):
    existing = _user(role=Role.ADMIN.value)
    db = _FakeDB(scalar_result=existing)
    monkeypatch.setattr(cli, "SessionFactory", _FakeSessionFactory(db))

    cli.bootstrap_admin("u@example.com", "pw")

    assert "已是 admin" in capsys.readouterr().out
    assert db.committed == 0  # 无写操作


# ---------------------------------------------------------------------------
# list-gpus
# ---------------------------------------------------------------------------


def test_list_gpus_empty_inventory(monkeypatch, capsys):
    monkeypatch.setattr(cli, "SessionFactory", _FakeSessionFactory(_FakeDB()))
    monkeypatch.setattr(cli, "scheduler", _FakeScheduler([]))

    cli.list_gpus()

    assert "(empty inventory)" in capsys.readouterr().out


def test_list_gpus_prints_inventory(monkeypatch, capsys):
    gpus = [
        SimpleNamespace(
            gpu_uuid="gpu-1", model="RTX 4090", status="available",
            memory_total=24564, workspace_id=None,
        ),
        SimpleNamespace(
            gpu_uuid="gpu-2", model="A100", status="allocated",
            memory_total=81920, workspace_id="ws-1",
        ),
    ]
    monkeypatch.setattr(cli, "SessionFactory", _FakeSessionFactory(_FakeDB()))
    monkeypatch.setattr(cli, "scheduler", _FakeScheduler(gpus))

    cli.list_gpus()

    out = capsys.readouterr().out
    assert "gpu-1" in out and "RTX 4090" in out and "available" in out
    assert "mem=24564" in out and "ws=-" in out
    assert "gpu-2" in out and "mem=81920" in out and "ws=ws-1" in out


# ---------------------------------------------------------------------------
# show-usage
# ---------------------------------------------------------------------------


def test_show_usage_prints_balance(monkeypatch, capsys):
    user = _user(email="u@example.com", user_id="uid-1", role=Role.USER.value)
    db = _FakeDB(scalar_result=user)
    monkeypatch.setattr(cli, "SessionFactory", _FakeSessionFactory(db))
    _patch_ledger(monkeypatch, 42)

    cli.show_usage("u@example.com")

    out = capsys.readouterr().out
    assert "user=u@example.com" in out
    assert "role=user" in out
    assert "balance=42" in out


def test_show_usage_unknown_user_exits(monkeypatch, capsys):
    db = _FakeDB(scalar_result=None)
    monkeypatch.setattr(cli, "SessionFactory", _FakeSessionFactory(db))

    with pytest.raises(SystemExit) as excinfo:
        cli.show_usage("missing@example.com")

    assert excinfo.value.code == 1
    assert "user not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# make-session
# ---------------------------------------------------------------------------


def test_make_session_prints_token_and_persists_session(monkeypatch, capsys):
    user = _user(email="u@example.com", user_id="uid-1", role=Role.USER.value)
    db = _FakeDB(scalar_result=user)
    monkeypatch.setattr(cli, "SessionFactory", _FakeSessionFactory(db))

    cli.make_session("u@example.com")

    token = capsys.readouterr().out.strip()
    assert token
    assert db.committed == 1
    assert len(db.added) == 1
    session = db.added[0]
    assert session.user_id == "uid-1"
    assert session.token_hash == hash_token(token)
    assert session.expires_at is not None


def test_make_session_unknown_user_exits(monkeypatch, capsys):
    db = _FakeDB(scalar_result=None)
    monkeypatch.setattr(cli, "SessionFactory", _FakeSessionFactory(db))

    with pytest.raises(SystemExit) as excinfo:
        cli.make_session("missing@example.com")

    assert excinfo.value.code == 1
    assert "user not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() argparse 分发
# ---------------------------------------------------------------------------


def _patch_dispatch_targets(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_ensure_tables", lambda: None)
    monkeypatch.setattr(cli, "bootstrap_admin", lambda *a, **kw: calls.append(("bootstrap-admin", a, kw)))
    monkeypatch.setattr(cli, "list_gpus", lambda: calls.append(("list-gpus",)))
    monkeypatch.setattr(cli, "show_usage", lambda *a: calls.append(("show-usage", a)))
    monkeypatch.setattr(cli, "make_session", lambda *a: calls.append(("make-session", a)))
    return calls


def test_main_bootstrap_admin_dispatch(monkeypatch):
    calls = _patch_dispatch_targets(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cli", "bootstrap-admin", "--email", "a@b.c", "--password", "pw"])

    cli.main()

    assert calls == [("bootstrap-admin", ("a@b.c", "pw", None), {})]


def test_main_list_gpus_dispatch(monkeypatch):
    calls = _patch_dispatch_targets(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cli", "list-gpus"])

    cli.main()

    assert calls == [("list-gpus",)]


def test_main_show_usage_dispatch(monkeypatch):
    calls = _patch_dispatch_targets(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cli", "show-usage", "--email", "a@b.c"])

    cli.main()

    assert calls == [("show-usage", ("a@b.c",))]


def test_main_make_session_dispatch(monkeypatch):
    calls = _patch_dispatch_targets(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cli", "make-session", "--email", "a@b.c"])

    cli.main()

    assert calls == [("make-session", ("a@b.c",))]
