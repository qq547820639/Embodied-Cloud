"""Courses 模块核心业务逻辑 (router 薄壳之外的全部逻辑)。

权限模型 (与 SECURITY.md T1 一致): 非 member 一律 404, 不泄露资源存在性;
teacher/admin 才有管理权限, 越权管理操作返回 403。
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import (
    Assignment,
    Course,
    CourseMember,
    Lab,
    Role,
    Submission,
    Template,
    User,
    Workspace,
)
from ..schemas import AssignmentCreate, CourseCreate, LabCreate
from ..utils import utcnow
from .billing import BillingError, BillingPolicy
from .orchestrator import WorkspaceOrchestrator

TEACHER_MEMBER_ROLES = (Role.INSTRUCTOR.value, Role.ORG_ADMIN.value)
ALLOWED_MEMBER_ROLES = (Role.STUDENT.value, Role.INSTRUCTOR.value, Role.ORG_ADMIN.value)


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------


def _member(db: Session, course: Course, user: User) -> CourseMember | None:
    return db.scalar(
        select(CourseMember).where(
            CourseMember.course_id == course.id, CourseMember.user_id == user.id
        )
    )


def is_teacher(db: Session, course: Course, user: User) -> bool:
    """teacher/admin: admin, course owner, 或 member role 为 instructor/org_admin。"""
    if user.role == Role.ADMIN.value or course.owner_id == user.id:
        return True
    member = _member(db, course, user)
    return member is not None and member.role in TEACHER_MEMBER_ROLES


def is_member(db: Session, course: Course, user: User) -> bool:
    """member: 存在任意 CourseMember, 或 teacher/admin 判定通过。"""
    if _member(db, course, user) is not None:
        return True
    return user.role == Role.ADMIN.value or course.owner_id == user.id


# ---------------------------------------------------------------------------
# 资源定位（带权限）
# ---------------------------------------------------------------------------


def get_course_or_404(
    db: Session, course_id: str, user: User, *, require_member: bool = True
) -> Course:
    course = db.get(Course, course_id)
    if course is None or (require_member and not is_member(db, course, user)):
        raise HTTPException(404, "course not found")
    return course


def get_course_for_teacher(db: Session, course_id: str, user: User) -> Course:
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "course not found")
    if not is_teacher(db, course, user):
        raise HTTPException(403, "teacher role required")
    return course


def get_lab_or_404(db: Session, lab_id: str, user: User) -> Lab:
    lab = db.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    course = db.get(Course, lab.course_id)
    if course is None or not is_member(db, course, user):
        raise HTTPException(404, "lab not found")
    return lab


def get_lab_for_teacher(db: Session, lab_id: str, user: User) -> Lab:
    lab = db.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    course = db.get(Course, lab.course_id)
    if course is None:
        raise HTTPException(404, "lab not found")
    if not is_teacher(db, course, user):
        raise HTTPException(403, "teacher role required")
    return lab


def get_assignment_or_404(db: Session, assignment_id: str, user: User) -> Assignment:
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise HTTPException(404, "assignment not found")
    lab = db.get(Lab, assignment.lab_id)
    course = db.get(Course, lab.course_id) if lab is not None else None
    if lab is None or course is None or not is_member(db, course, user):
        raise HTTPException(404, "assignment not found")
    return assignment


# ---------------------------------------------------------------------------
# 教师流程
# ---------------------------------------------------------------------------


def create_course(db: Session, payload: CourseCreate, user: User) -> Course:
    if db.scalar(select(Course).where(Course.slug == payload.slug)) is not None:
        raise HTTPException(409, "course slug already exists")
    course = Course(
        id=str(uuid.uuid4()),
        organization_id=user.organization_id,
        owner_id=user.id,
        name=payload.name,
        description=payload.description,
        slug=payload.slug,
    )
    db.add(course)
    # 创建者自动成为 instructor member
    db.add(
        CourseMember(
            id=str(uuid.uuid4()),
            course_id=course.id,
            user_id=user.id,
            role=Role.INSTRUCTOR.value,
        )
    )
    db.commit()
    db.refresh(course)
    return course


def list_courses(db: Session, user: User) -> list[Course]:
    member_course_ids = select(CourseMember.course_id).where(CourseMember.user_id == user.id)
    stmt = (
        select(Course)
        .where(or_(Course.owner_id == user.id, Course.id.in_(member_course_ids)))
        .order_by(Course.created_at.desc())
    )
    return list(db.scalars(stmt))


def add_member(db: Session, course: Course, user_id: str, role: str) -> CourseMember:
    if db.get(User, user_id) is None:
        raise HTTPException(404, "user not found")
    if role not in ALLOWED_MEMBER_ROLES:
        raise HTTPException(400, "invalid member role")
    member = db.scalar(
        select(CourseMember).where(
            CourseMember.course_id == course.id, CourseMember.user_id == user_id
        )
    )
    if member is None:
        member = CourseMember(
            id=str(uuid.uuid4()), course_id=course.id, user_id=user_id, role=role
        )
        db.add(member)
    else:
        member.role = role
    db.commit()
    db.refresh(member)
    return member


def list_members(db: Session, course: Course) -> list[CourseMember]:
    stmt = (
        select(CourseMember)
        .where(CourseMember.course_id == course.id)
        .order_by(CourseMember.created_at)
    )
    return list(db.scalars(stmt))


def create_lab(db: Session, course: Course, payload: LabCreate) -> Lab:
    template = db.get(Template, payload.template_id)
    if template is None or not template.enabled:
        raise HTTPException(404, "template not found")
    lab = Lab(
        id=str(uuid.uuid4()),
        course_id=course.id,
        template_id=template.id,
        name=payload.name,
        description=payload.description,
        quota_seconds=payload.quota_seconds,
    )
    db.add(lab)
    db.commit()
    db.refresh(lab)
    return lab


def list_labs(db: Session, course: Course) -> list[Lab]:
    stmt = select(Lab).where(Lab.course_id == course.id).order_by(Lab.created_at)
    return list(db.scalars(stmt))


def create_assignment(db: Session, lab: Lab, payload: AssignmentCreate) -> Assignment:
    assignment = Assignment(
        id=str(uuid.uuid4()),
        lab_id=lab.id,
        name=payload.name,
        description=payload.description,
        due_at=payload.due_at,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return assignment


def list_assignments(db: Session, lab: Lab) -> list[Assignment]:
    stmt = select(Assignment).where(Assignment.lab_id == lab.id).order_by(Assignment.created_at)
    return list(db.scalars(stmt))


def course_completions(db: Session, course: Course) -> list[dict]:
    """每个 assignment x 每个学生的 submission 状态汇总 (teacher/admin)。

    批量加载该 course 的 labs/assignments/submissions（消除逐 lab/assignment/student
    的 N+1 查询），再在内存中按原遍历顺序组装；返回结构与旧实现完全一致。
    """
    students = list(
        db.scalars(
            select(User)
            .join(CourseMember, CourseMember.user_id == User.id)
            .where(
                CourseMember.course_id == course.id,
                CourseMember.role == Role.STUDENT.value,
            )
            .order_by(User.username)
        )
    )
    labs = list_labs(db, course)
    lab_ids = [lab.id for lab in labs]
    assignments_by_lab: dict[str, list[Assignment]] = {}
    if lab_ids:
        for assignment in db.scalars(
            select(Assignment)
            .where(Assignment.lab_id.in_(lab_ids))
            .order_by(Assignment.created_at)
        ):
            assignments_by_lab.setdefault(assignment.lab_id, []).append(assignment)
    assignment_ids = [
        assignment.id
        for assignments in assignments_by_lab.values()
        for assignment in assignments
    ]
    submissions: dict[tuple[str, str], Submission] = {}
    if assignment_ids:
        for sub in db.scalars(
            select(Submission).where(Submission.assignment_id.in_(assignment_ids))
        ):
            submissions[(sub.assignment_id, sub.user_id)] = sub

    result: list[dict] = []
    for lab in labs:
        for assignment in assignments_by_lab.get(lab.id, []):
            rows = []
            for student in students:
                submission = submissions.get((assignment.id, student.id))
                rows.append(
                    {
                        "user_id": student.id,
                        "username": student.username,
                        "status": submission.status if submission is not None else "not_submitted",
                        "completed_at": submission.completed_at if submission is not None else None,
                    }
                )
            result.append(
                {
                    "lab_id": lab.id,
                    "lab_name": lab.name,
                    "assignment_id": assignment.id,
                    "assignment_name": assignment.name,
                    "submissions": rows,
                }
            )
    return result


# ---------------------------------------------------------------------------
# 学生流程
# ---------------------------------------------------------------------------


def join_course(db: Session, course: Course, user: User) -> CourseMember:
    member = _member(db, course, user)
    if member is not None:
        return member
    member = CourseMember(
        id=str(uuid.uuid4()), course_id=course.id, user_id=user.id, role=Role.STUDENT.value
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


def launch_lab(
    db: Session,
    orchestrator: WorkspaceOrchestrator,
    lab: Lab,
    user: User,
    billing: BillingPolicy | None = None,
) -> Workspace:
    template = db.get(Template, lab.template_id)
    if template is None or not template.enabled:
        raise HTTPException(404, "template not found")
    # BillingPolicy：launch 前额度/配额门禁（course quota_seconds 真实执行）
    if billing is not None:
        try:
            billing.check_launch_eligible(db, user, template, lab=lab)
        except BillingError as exc:
            raise HTTPException(402, str(exc)) from exc
    workspace = orchestrator.create(
        db,
        template,
        user_id=user.id,
        organization_id=user.organization_id,
    )
    orchestrator.start_async(workspace.id)
    return workspace


def upsert_submission(
    db: Session, assignment: Assignment, user: User, workspace_id: str | None
) -> Submission:
    sub = db.scalar(
        select(Submission).where(
            Submission.assignment_id == assignment.id, Submission.user_id == user.id
        )
    )
    if sub is None:
        sub = Submission(
            id=str(uuid.uuid4()),
            assignment_id=assignment.id,
            user_id=user.id,
            workspace_id=workspace_id,
            status="completed",
            completed_at=utcnow(),
        )
        db.add(sub)
    else:
        sub.status = "completed"
        sub.completed_at = utcnow()
        if workspace_id is not None:
            sub.workspace_id = workspace_id
    db.commit()
    db.refresh(sub)
    return sub
