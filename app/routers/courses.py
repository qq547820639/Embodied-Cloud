"""Courses - Course/Lab/Assignment/Submission 高校教学模块 (薄壳)。

路由显式写全路径 (不设 router prefix), 以便同一模块同时承载
/courses/*, /labs/*, /assignments/* 三组端点; main.py 以 prefix="/api" 挂载。
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..deps import DB, CurrentUser, billing, orchestrator
from ..models import Role
from ..schemas import (
    AssignmentCreate,
    AssignmentOut,
    CourseCreate,
    CourseMemberOut,
    CourseOut,
    LabCreate,
    LabOut,
    SubmissionOut,
    WorkspaceOut,
)
from ..services import course as course_service
from ..services.ledger import mark_usage_segment

router = APIRouter(tags=["courses"])


class MemberAdd(BaseModel):
    user_id: str
    role: str = Role.STUDENT.value


class SubmitIn(BaseModel):
    workspace_id: str | None = None


class JoinBySlugIn(BaseModel):
    slug: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# 课程（教师 + 学生）
# ---------------------------------------------------------------------------


@router.post("/courses", response_model=CourseOut, status_code=201)
def create_course(payload: CourseCreate, db: DB, user: CurrentUser):
    return course_service.create_course(db, payload, user)


@router.get("/courses", response_model=list[CourseOut])
def list_courses(db: DB, user: CurrentUser):
    return course_service.list_courses(db, user)


@router.get("/courses/{course_id}", response_model=CourseOut)
def get_course(course_id: str, db: DB, user: CurrentUser):
    return course_service.get_course_or_404(db, course_id, user)


@router.post("/courses/{course_id}/join", response_model=CourseMemberOut)
def join_course(course_id: str, db: DB, user: CurrentUser):
    course = course_service.get_course_or_404(db, course_id, user, require_member=False)
    return course_service.join_course(db, course, user)


@router.post("/courses/join-by-slug", response_model=CourseMemberOut)
def join_course_by_slug(payload: JoinBySlugIn, db: DB, user: CurrentUser):
    """按 slug（邀请码）加入课程：老师分享 slug，学生输入即可加入。"""
    return course_service.join_course_by_slug(db, payload.slug, user)


@router.post("/courses/{course_id}/members", response_model=CourseMemberOut, status_code=201)
def add_member(course_id: str, payload: MemberAdd, db: DB, user: CurrentUser):
    course = course_service.get_course_for_teacher(db, course_id, user)
    return course_service.add_member(db, course, payload.user_id, payload.role)


@router.get("/courses/{course_id}/members", response_model=list[CourseMemberOut])
def list_members(course_id: str, db: DB, user: CurrentUser):
    course = course_service.get_course_for_teacher(db, course_id, user)
    return course_service.list_members(db, course)


@router.get("/courses/{course_id}/completions")
def get_completions(course_id: str, db: DB, user: CurrentUser):
    course = course_service.get_course_for_teacher(db, course_id, user)
    return course_service.course_completions(db, course)


@router.post("/courses/{course_id}/labs", response_model=LabOut, status_code=201)
def create_lab(course_id: str, payload: LabCreate, db: DB, user: CurrentUser):
    course = course_service.get_course_for_teacher(db, course_id, user)
    return course_service.create_lab(db, course, payload)


@router.get("/courses/{course_id}/labs", response_model=list[LabOut])
def list_labs(course_id: str, db: DB, user: CurrentUser):
    """labs 列表对 member 可见（学生要看到实验才能 launch）；创建仍限 teacher。"""
    course = course_service.get_course_or_404(db, course_id, user)
    return course_service.list_labs(db, course)


@router.get("/courses/{course_id}/my-progress")
def get_my_progress(course_id: str, db: DB, user: CurrentUser):
    """当前用户在课程中的作业进度（member 可见；教师另用 /completions 看全班）。"""
    course = course_service.get_course_or_404(db, course_id, user)
    return course_service.my_progress(db, course, user)


# ---------------------------------------------------------------------------
# Lab / Assignment
# ---------------------------------------------------------------------------


@router.post("/labs/{lab_id}/assignments", response_model=AssignmentOut, status_code=201)
def create_assignment(lab_id: str, payload: AssignmentCreate, db: DB, user: CurrentUser):
    lab = course_service.get_lab_for_teacher(db, lab_id, user)
    return course_service.create_assignment(db, lab, payload)


@router.get("/labs/{lab_id}/assignments", response_model=list[AssignmentOut])
def list_assignments(lab_id: str, db: DB, user: CurrentUser):
    """assignments 列表对 member 可见（学生要看到作业才能提交）；创建仍限 teacher。"""
    lab = course_service.get_lab_or_404(db, lab_id, user)
    return course_service.list_assignments(db, lab)


@router.post("/labs/{lab_id}/launch", response_model=WorkspaceOut, status_code=201)
def launch_lab(lab_id: str, db: DB, user: CurrentUser):
    lab = course_service.get_lab_or_404(db, lab_id, user)
    return mark_usage_segment(
        db, course_service.launch_lab(db, orchestrator, lab, user, billing=billing)
    )


@router.post("/assignments/{assignment_id}/submit", response_model=SubmissionOut)
def submit_assignment(assignment_id: str, payload: SubmitIn, db: DB, user: CurrentUser):
    assignment = course_service.get_assignment_or_404(db, assignment_id, user)
    return course_service.upsert_submission(db, assignment, user, payload.workspace_id)
