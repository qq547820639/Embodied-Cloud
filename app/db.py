from collections.abc import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


def make_engine(settings: Settings):
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    return create_engine(settings.database_url, connect_args=connect_args, future=True)


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
