"""Templates（Registry）—— 公开可读，enabled 过滤。"""

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..deps import DB
from ..models import Template
from ..schemas import TemplateOut

router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("", response_model=list[TemplateOut])
def list_templates(db: DB):
    stmt = (
        select(Template)
        .where(Template.enabled.is_(True))
        .order_by(Template.category, Template.name)
    )
    return list(db.scalars(stmt))


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(template_id: str, db: DB):
    template = db.get(Template, template_id)
    if template is None or not template.enabled:
        raise HTTPException(404, "template not found")
    return template
