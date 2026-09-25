from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


def _enable_sqlite_foreign_keys(dbapi_connection) -> None:
    """SQLite 默认不校验外键（连接级 PRAGMA），PostgreSQL 默认校验。

    不打开时开发库可以静默写进孤儿行（实测：同批 parent/child 插入顺序颠倒
    在 SQLite 下无声通过、在 PG 下 IntegrityError），这类脏数据一旦带到
    生产就再也插不进去，所以应用在 sqlite 连接上强制开启。
    Alembic 的 batch_alter_table 走它自己的连接，不受这里影响。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def make_engine(settings: Settings):
    is_sqlite = settings.database_url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
    if is_sqlite:
        event.listen(engine, "connect", lambda dbapi_conn, _record: _enable_sqlite_foreign_keys(dbapi_conn))
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def session_dependency(factory):
    def _get_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        finally:
            db.close()
    return _get_db
