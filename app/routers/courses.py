"""Courses - Course/Lab/Assignment/Submission 高校教学模块 (薄壳)。

路由显式写全路径 (不设 router prefix), 以便同一模块同时承载
/courses/*, /labs/*, /assignments/* 三组端点; main.py 以 prefix="/api" 挂载。
"""

from fastapi import APIRouter
from pydantic import BaseModel

from ..deps import DB, CurrentUser, orchestrator
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

router = APIRouter(tags=["courses"])


class MemberAdd(BaseModel):
    user_id: str
    role: str = Role.STUDENT.value


class SubmitIn(BaseModel):
    workspace_id: str | None = None


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
    course = course_service.get_course_for_teacher(db, course_id, user)
    return course_service.list_labs(db, course)


# ---------------------------------------------------------------------------
# Lab / Assignment
# ---------------------------------------------------------------------------


@router.post("/labs/{lab_id}/assignments", response_model=AssignmentOut, status_code=201)
def create_assignment(lab_id: str, payload: AssignmentCreate, db: DB, user: CurrentUser):
    lab = course_service.get_lab_for_teacher(db, lab_id, user)
    return course_service.create_assignment(db, lab, payload)


@router.get("/labs/{lab_id}/assignments", response_model=list[AssignmentOut])
def list_assignments(lab_id: str, db: DB, user: CurrentUser):
    lab = course_service.get_lab_for_teacher(db, lab_id, user)
    return course_service.list_assignments(db, lab)


@router.post("/labs/{lab_id}/launch", response_model=WorkspaceOut, status_code=201)
def launch_lab(lab_id: str, db: DB, user: CurrentUser):
    lab = course_service.get_lab_or_404(db, lab_id, user)
    return course_service.launch_lab(db, orchestrator, lab, user)


@router.post("/assignments/{assignment_id}/submit", response_model=SubmissionOut)
def submit_assignment(assignment_id: str, payload: SubmitIn, db: DB, user: CurrentUser):
    assignment = course_service.get_assignment_or_404(db, assignment_id, user)
    return course_service.upsert_submission(db, assignment, user, payload.workspace_id)
