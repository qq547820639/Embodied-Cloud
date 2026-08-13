"""课程学生端 onboarding（UX 迭代）：slug 邀请码加入、member 可见 labs/assignments、
个人进度 my-progress。与 test_courses.py 同一内存 SQLite 模式。"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.deps import get_current_user, get_db
from app.models import CourseMember, Role, User
from app.routers.courses import router
from tests.test_courses import create_course, new_user


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
def teacher(db_session: Session) -> User:
    return new_user(db_session, "u-teacher", "teacher")


@pytest.fixture
def student(db_session: Session) -> User:
    return new_user(db_session, "u-student", "student")


@pytest.fixture
def stranger(db_session: Session) -> User:
    return new_user(db_session, "u-stranger", "stranger")


def make_client(db: Session, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


@pytest.fixture
def teacher_client(db_session: Session, teacher: User) -> TestClient:
    return make_client(db_session, teacher)


@pytest.fixture
def student_client(db_session: Session, student: User) -> TestClient:
    return make_client(db_session, student)


@pytest.fixture
def stranger_client(db_session: Session, stranger: User) -> TestClient:
    return make_client(db_session, stranger)


@pytest.fixture
def template(db_session: Session):
    from app.models import Template

    tpl = Template(
        id="tpl-onboarding-test",
        slug="onboarding-test-template",
        name="Onboarding Test Template",
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


def _lab(teacher_client, template, course_id):
    resp = teacher_client.post(
        f"/courses/{course_id}/labs",
        json={"template_id": template.id, "name": "实验一", "quota_seconds": 3600},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_join_by_slug_creates_student_member(db_session, teacher_client, student_client):
    course = create_course(teacher_client, f"joinme-{uuid.uuid4().hex[:8]}")
    resp = student_client.post("/courses/join-by-slug", json={"slug": course["slug"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["course_id"] == course["id"]
    member = db_session.scalar(
        select(CourseMember).where(
            CourseMember.course_id == course["id"],
            CourseMember.user_id == resp.json()["user_id"],
        )
    )
    assert member is not None
    assert member.role == Role.STUDENT.value
    # 加入后可见课程详情
    assert student_client.get(f"/courses/{course['id']}").status_code == 200


def test_join_by_slug_unknown_404(student_client):
    resp = student_client.post("/courses/join-by-slug", json={"slug": "no-such-slug"})
    assert resp.status_code == 404


def test_join_by_slug_idempotent(db_session, teacher_client, student_client):
    course = create_course(teacher_client, f"idem-{uuid.uuid4().hex[:8]}")
    first = student_client.post("/courses/join-by-slug", json={"slug": course["slug"]})
    second = student_client.post("/courses/join-by-slug", json={"slug": course["slug"]})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_student_can_list_labs_and_assignments(
    db_session, template, teacher_client, student_client
):
    course = create_course(teacher_client, f"labs-{uuid.uuid4().hex[:8]}")
    student_client.post("/courses/join-by-slug", json={"slug": course["slug"]})
    lab = _lab(teacher_client, template, course["id"])

    # member（学生）可读 labs / assignments（此前仅 teacher）
    labs = student_client.get(f"/courses/{course['id']}/labs")
    assert labs.status_code == 200, labs.text
    assert any(row["id"] == lab["id"] for row in labs.json())

    assignment = teacher_client.post(
        f"/labs/{lab['id']}/assignments", json={"name": "作业一", "description": "d"}
    )
    assert assignment.status_code == 201
    assignments = student_client.get(f"/labs/{lab['id']}/assignments")
    assert assignments.status_code == 200, assignments.text
    assert any(row["id"] == assignment.json()["id"] for row in assignments.json())

    # 学生仍不能创建（teacher-only 语义不变）
    assert (
        student_client.post(
            f"/courses/{course['id']}/labs",
            json={"template_id": template.id, "name": "x", "quota_seconds": 1},
        ).status_code
        == 403
    )


def test_non_member_cannot_list_labs(teacher_client, stranger_client):
    course = create_course(teacher_client, f"priv-{uuid.uuid4().hex[:8]}")
    assert stranger_client.get(f"/courses/{course['id']}/labs").status_code == 404


def test_launch_lab_billing_error_maps_to_402(
    db_session, template, teacher_client, student_client, monkeypatch
):
    """POST /labs/{id}/launch 的 BillingError → 402 映射（此前零覆盖）。"""
    import app.routers.courses as courses_router
    from app.services.billing import BillingError

    course = create_course(teacher_client, f"bill-{uuid.uuid4().hex[:8]}")
    student_client.post("/courses/join-by-slug", json={"slug": course["slug"]})
    lab = _lab(teacher_client, template, course["id"])

    class DenyBilling:
        def check_launch_eligible(self, db, user, tpl, lab=None):
            raise BillingError("credits exhausted")

    monkeypatch.setattr(courses_router, "billing", DenyBilling())
    resp = student_client.post(f"/labs/{lab['id']}/launch")
    assert resp.status_code == 402
    assert "credits exhausted" in resp.json()["detail"]


def test_my_progress_tracks_submission(
    db_session, template, teacher_client, student_client, stranger_client
):
    course = create_course(teacher_client, f"prog-{uuid.uuid4().hex[:8]}")
    join = student_client.post("/courses/join-by-slug", json={"slug": course["slug"]})
    assert join.status_code == 200
    lab = _lab(teacher_client, template, course["id"])
    assignment = teacher_client.post(
        f"/labs/{lab['id']}/assignments", json={"name": "作业一"}
    ).json()

    # 未提交前：not_submitted
    progress = student_client.get(f"/courses/{course['id']}/my-progress")
    assert progress.status_code == 200, progress.text
    rows = {row["assignment_id"]: row for row in progress.json()}
    assert rows[assignment["id"]]["status"] == "not_submitted"

    # 提交后：completed + completed_at
    submit = student_client.post(
        f"/assignments/{assignment['id']}/submit", json={"workspace_id": None}
    )
    assert submit.status_code == 200, submit.text
    progress = student_client.get(f"/courses/{course['id']}/my-progress")
    rows = {row["assignment_id"]: row for row in progress.json()}
    assert rows[assignment["id"]]["status"] == "completed"
    assert rows[assignment["id"]]["completed_at"] is not None
    assert rows[assignment["id"]]["lab_name"] == lab["name"]

    # 非 member → 404
    assert stranger_client.get(f"/courses/{course['id']}/my-progress").status_code == 404
