"""Courses 模块测试。

不 import app.main (避免 lifespan); 自建内存 SQLite (StaticPool),
用 TestClient 挂载 courses router 并 override DB/CurrentUser 依赖。
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.deps import get_current_user, get_db, orchestrator
from app.models import CourseMember, Role, Submission, Template, User
from app.routers.courses import router


def new_user(db: Session, user_id: str, username: str) -> User:
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        username=username,
        password_hash="x",  # noqa: S106 测试桩用户, 非真实密码
        role=Role.USER.value,
    )
    db.add(user)
    db.commit()
    return user


def make_client(db: Session, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def create_course(client: TestClient, slug: str) -> dict:
    resp = client.post(
        "/courses",
        json={"name": f"Course {slug}", "description": "test course", "slug": slug},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    yield session
    session.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def template(db_session: Session) -> Template:
    tpl = Template(
        id="tpl-courses-test",
        slug="courses-test-template",
        name="Courses Test Template",
        version="0.1.0",
        description="test template",
        category="test",
        runtime="mock",
        image=None,
        gpu_requirement_gb=1,
        repo_asset=None,
        entrypoint="",
        outputs=[],
        requires_streaming=False,
        healthcheck=None,
        metadata_json={},
        launch_command="",
        recommended_vram_gb=1,
        estimated_hourly_cost_cny=1.0,
        enabled=True,
    )
    db_session.add(tpl)
    db_session.commit()
    return tpl


@pytest.fixture
def teacher(db_session: Session) -> User:
    return new_user(db_session, "u-teacher", "teacher")


@pytest.fixture
def student(db_session: Session) -> User:
    return new_user(db_session, "u-student", "student")


@pytest.fixture
def stranger(db_session: Session) -> User:
    return new_user(db_session, "u-stranger", "stranger")


@pytest.fixture
def teacher_client(db_session: Session, teacher: User) -> TestClient:
    return make_client(db_session, teacher)


@pytest.fixture
def student_client(db_session: Session, student: User) -> TestClient:
    return make_client(db_session, student)


@pytest.fixture
def stranger_client(db_session: Session, stranger: User) -> TestClient:
    return make_client(db_session, stranger)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_create_course_owner_becomes_instructor_member(
    db_session, teacher, teacher_client
):
    course = create_course(teacher_client, f"intro-{uuid.uuid4().hex[:8]}")
    assert course["owner_id"] == teacher.id
    member = db_session.scalar(
        select(CourseMember).where(CourseMember.course_id == course["id"])
    )
    assert member is not None
    assert member.user_id == teacher.id
    assert member.role == Role.INSTRUCTOR.value


def test_student_join_is_idempotent(db_session, teacher_client, student_client):
    course = create_course(teacher_client, f"join-{uuid.uuid4().hex[:8]}")
    resp = student_client.post(f"/courses/{course['id']}/join")
    assert resp.status_code == 200
    first = resp.json()
    assert first["role"] == Role.STUDENT.value

    resp2 = student_client.post(f"/courses/{course['id']}/join")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == first["id"]

    count = db_session.scalar(
        select(func.count(CourseMember.id)).where(CourseMember.course_id == course["id"])
    )
    assert count == 2  # teacher(instructor) + student


def test_create_lab_requires_existing_template(teacher_client):
    course = create_course(teacher_client, f"tpl-{uuid.uuid4().hex[:8]}")
    resp = teacher_client.post(
        f"/courses/{course['id']}/labs",
        json={"template_id": "no-such-template", "name": "Lab 1"},
    )
    assert resp.status_code == 404


def test_student_launch_lab_creates_owned_workspace(
    db_session, template, teacher_client, student_client, student, monkeypatch
):
    monkeypatch.setattr(orchestrator, "start_async", lambda workspace_id: None)
    course = create_course(teacher_client, f"launch-{uuid.uuid4().hex[:8]}")
    lab_resp = teacher_client.post(
        f"/courses/{course['id']}/labs",
        json={"template_id": template.id, "name": "Lab 1"},
    )
    assert lab_resp.status_code == 201, lab_resp.text
    lab = lab_resp.json()
    assert student_client.post(f"/courses/{course['id']}/join").status_code == 200

    resp = student_client.post(f"/labs/{lab['id']}/launch")
    assert resp.status_code == 201, resp.text
    workspace = resp.json()
    assert workspace["user_id"] == student.id
    assert workspace["template_id"] == template.id


def test_submit_is_idempotent(
    db_session, template, teacher_client, student_client, student
):
    course = create_course(teacher_client, f"submit-{uuid.uuid4().hex[:8]}")
    lab = teacher_client.post(
        f"/courses/{course['id']}/labs",
        json={"template_id": template.id, "name": "Lab"},
    ).json()
    assignment = teacher_client.post(
        f"/labs/{lab['id']}/assignments", json={"name": "A1"}
    ).json()
    assert student_client.post(f"/courses/{course['id']}/join").status_code == 200

    resp = student_client.post(
        f"/assignments/{assignment['id']}/submit", json={"workspace_id": "ws-1"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"

    resp2 = student_client.post(
        f"/assignments/{assignment['id']}/submit", json={"workspace_id": "ws-2"}
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "completed"

    subs = list(
        db_session.scalars(
            select(Submission).where(Submission.assignment_id == assignment["id"])
        )
    )
    assert len(subs) == 1
    assert subs[0].user_id == student.id
    assert subs[0].status == "completed"
    assert subs[0].completed_at is not None
    assert subs[0].workspace_id == "ws-2"


def test_completions_include_student_submission(
    template, teacher_client, student_client, student
):
    course = create_course(teacher_client, f"comp-{uuid.uuid4().hex[:8]}")
    lab = teacher_client.post(
        f"/courses/{course['id']}/labs",
        json={"template_id": template.id, "name": "Lab"},
    ).json()
    assignment = teacher_client.post(
        f"/labs/{lab['id']}/assignments", json={"name": "A1"}
    ).json()
    assert student_client.post(f"/courses/{course['id']}/join").status_code == 200
    resp = student_client.post(f"/assignments/{assignment['id']}/submit", json={})
    assert resp.status_code == 200

    resp = teacher_client.get(f"/courses/{course['id']}/completions")
    assert resp.status_code == 200
    data = resp.json()
    entry = next(e for e in data if e["assignment_id"] == assignment["id"])
    row = next(r for r in entry["submissions"] if r["user_id"] == student.id)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None


def test_non_member_cannot_read_course(teacher_client, stranger_client):
    course = create_course(teacher_client, f"private-{uuid.uuid4().hex[:8]}")
    resp = stranger_client.get(f"/courses/{course['id']}")
    assert resp.status_code == 404


def test_student_cannot_create_lab(template, teacher_client, student_client):
    course = create_course(teacher_client, f"deny-{uuid.uuid4().hex[:8]}")
    assert student_client.post(f"/courses/{course['id']}/join").status_code == 200
    resp = student_client.post(
        f"/courses/{course['id']}/labs",
        json={"template_id": template.id, "name": "Lab"},
    )
    assert resp.status_code == 403


def test_list_courses_includes_participated(teacher_client, student_client):
    course = create_course(teacher_client, f"list-{uuid.uuid4().hex[:8]}")
    assert student_client.post(f"/courses/{course['id']}/join").status_code == 200

    resp = student_client.get("/courses")
    assert resp.status_code == 200
    assert any(c["id"] == course["id"] for c in resp.json())

    resp_teacher = teacher_client.get("/courses")
    assert any(c["id"] == course["id"] for c in resp_teacher.json())
