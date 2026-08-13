"""Courses 模块测试。

不 import app.main (避免 lifespan); 自建内存 SQLite (StaticPool),
用 TestClient 挂载 courses router 并 override DB/CurrentUser 依赖。
"""

import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.deps import get_current_user, get_db, orchestrator
from app.models import CourseMember, Role, Submission, Template, User, Workspace
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


def new_workspace(db: Session, workspace_id: str, owner_id: str, template_id: str) -> Workspace:
    ws = Workspace(
        id=workspace_id,
        name=f"ws-{workspace_id}",
        template_id=template_id,
        provider="mock",
        user_id=owner_id,
    )
    db.add(ws)
    db.commit()
    return ws


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

    new_workspace(db_session, "ws-1", student.id, template.id)
    new_workspace(db_session, "ws-2", student.id, template.id)

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


def _make_submittable_assignment(
    db_session: Session,
    template: Template,
    teacher_client: TestClient,
    student_client: TestClient,
    slug_prefix: str,
) -> dict:
    course = create_course(teacher_client, f"{slug_prefix}-{uuid.uuid4().hex[:8]}")
    lab = teacher_client.post(
        f"/courses/{course['id']}/labs",
        json={"template_id": template.id, "name": "Lab"},
    ).json()
    assignment = teacher_client.post(
        f"/labs/{lab['id']}/assignments", json={"name": "A1"}
    ).json()
    assert student_client.post(f"/courses/{course['id']}/join").status_code == 200
    return assignment


def test_submit_owned_workspace_ok(
    db_session, template, teacher_client, student_client, student
):
    assignment = _make_submittable_assignment(
        db_session, template, teacher_client, student_client, "own"
    )
    new_workspace(db_session, "ws-own", student.id, template.id)
    resp = student_client.post(
        f"/assignments/{assignment['id']}/submit", json={"workspace_id": "ws-own"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace_id"] == "ws-own"


def test_submit_cross_tenant_workspace_404(
    db_session, template, teacher_client, student_client, student, stranger
):
    assignment = _make_submittable_assignment(
        db_session, template, teacher_client, student_client, "cross"
    )
    new_workspace(db_session, "ws-stranger", stranger.id, template.id)
    resp = student_client.post(
        f"/assignments/{assignment['id']}/submit", json={"workspace_id": "ws-stranger"}
    )
    assert resp.status_code == 404


def test_submit_missing_workspace_404(
    db_session, template, teacher_client, student_client
):
    assignment = _make_submittable_assignment(
        db_session, template, teacher_client, student_client, "missing"
    )
    resp = student_client.post(
        f"/assignments/{assignment['id']}/submit", json={"workspace_id": "no-such-ws"}
    )
    assert resp.status_code == 404


def test_submit_admin_can_link_any_workspace(
    db_session, template, teacher_client, student_client, stranger
):
    """admin 豁免归属校验（与项目其它函数一致）。"""
    assignment = _make_submittable_assignment(
        db_session, template, teacher_client, student_client, "admin"
    )
    new_workspace(db_session, "ws-admin-link", stranger.id, template.id)
    admin = new_user(db_session, "u-admin", "admin")
    admin.role = Role.ADMIN.value
    db_session.commit()
    admin_client = make_client(db_session, admin)
    resp = admin_client.post(
        f"/assignments/{assignment['id']}/submit",
        json={"workspace_id": "ws-admin-link"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace_id"] == "ws-admin-link"


def test_create_course_duplicate_slug_409(teacher_client):
    slug = f"dup-{uuid.uuid4().hex[:8]}"
    create_course(teacher_client, slug)  # 首次成功
    resp = teacher_client.post(
        "/courses", json={"name": "Course 2", "description": "x", "slug": slug}
    )
    assert resp.status_code == 409


def test_create_course_db_conflict_maps_to_409(db_session, teacher, monkeypatch):
    """绕过先查（select 未命中）后插入触发 DB 唯一约束 → 409（§S-5）。"""
    from sqlalchemy.exc import IntegrityError

    from app.schemas import CourseCreate
    from app.services import course as course_service

    def _raise_integrity():
        raise IntegrityError(
            "INSERT INTO courses ...", {}, Exception("UNIQUE constraint failed: courses.slug")
        )

    monkeypatch.setattr(db_session, "commit", _raise_integrity)
    with pytest.raises(HTTPException) as exc_info:
        course_service.create_course(
            db_session, CourseCreate(name="C", description="d", slug="dup"), teacher
        )
    assert exc_info.value.status_code == 409


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


def test_course_completions_ordering_and_dedup(db_session, template, teacher):
    """course_completions 批量加载与旧实现等价：lab/assignment 按创建序、学生按用户名、无提交占位。"""
    from datetime import UTC, datetime, timedelta

    from app.models import Assignment, Course, Lab
    from app.services import course as course_service

    course = Course(id="c-comp", owner_id=teacher.id, name="C", slug="comp")
    db_session.add(course)
    db_session.commit()

    # 学生按 username 升序：amy < bob < zoe
    for uid, uname in (("s-bob", "bob"), ("s-amy", "amy"), ("s-zoe", "zoe")):
        db_session.add(
            User(
                id=uid, email=f"{uid}@x.com", username=uname,
                password_hash="x",  # noqa: S106 测试桩用户，非真实密码
                role=Role.USER.value,
            )
        )
        db_session.add(
            CourseMember(id=f"cm-{uid}", course_id="c-comp", user_id=uid, role=Role.STUDENT.value)
        )
    db_session.commit()

    # 显式 created_at 保证遍历顺序确定性
    base = datetime(2026, 8, 12, 10, 0, 0, tzinfo=UTC)
    lab1 = Lab(id="lab-1", course_id="c-comp", template_id=template.id, name="Lab1", created_at=base)
    lab2 = Lab(id="lab-2", course_id="c-comp", template_id=template.id, name="Lab2",
               created_at=base + timedelta(seconds=10))
    db_session.add_all([lab1, lab2])
    db_session.commit()
    a1 = Assignment(id="a1", lab_id="lab-1", name="A1", created_at=base)
    a2 = Assignment(id="a2", lab_id="lab-1", name="A2", created_at=base + timedelta(seconds=1))
    a3 = Assignment(id="a3", lab_id="lab-2", name="A3", created_at=base)
    db_session.add_all([a1, a2, a3])
    db_session.commit()
    db_session.add(Submission(id="sub-1", assignment_id="a1", user_id="s-bob", status="completed"))
    db_session.commit()

    data = course_service.course_completions(db_session, course)

    # 遍历顺序：lab1 的两个 assignment 在前，lab2 在后；assignment 按创建序
    assert [e["lab_id"] for e in data] == ["lab-1", "lab-1", "lab-2"]
    assert [e["assignment_id"] for e in data] == ["a1", "a2", "a3"]
    assert [e["lab_name"] for e in data] == ["Lab1", "Lab1", "Lab2"]
    assert [e["assignment_name"] for e in data] == ["A1", "A2", "A3"]
    # 每个 assignment 下学生按 username 升序，且每学生仅一行（去重）
    for entry in data:
        assert [r["username"] for r in entry["submissions"]] == ["amy", "bob", "zoe"]
        assert len(entry["submissions"]) == 3
    # 提交状态与占位字段
    a1_rows = {r["user_id"]: r for r in data[0]["submissions"]}
    assert a1_rows["s-bob"]["status"] == "completed"
    assert a1_rows["s-amy"]["status"] == "not_submitted"
    assert a1_rows["s-zoe"]["status"] == "not_submitted"
    assert a1_rows["s-bob"]["completed_at"] is None
    assert all(r["status"] == "not_submitted" for r in data[1]["submissions"])


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
