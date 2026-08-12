from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .config import Settings
from .db import Base, make_engine, make_session_factory, session_dependency
from .models import Template, Workspace, WorkspaceStatus
from .schemas import (
    HealthOut,
    TemplateOut,
    UsageOut,
    WorkspaceAccessOut,
    WorkspaceCreate,
    WorkspaceOut,
)
from .seed import seed_templates
from .services.orchestrator import WorkspaceOrchestrator
from .services.providers.docker import DockerProvider
from .services.providers.mock import MockProvider

settings = Settings()
settings.ensure_dirs()
engine = make_engine(settings)
SessionFactory = make_session_factory(engine)
get_db = session_dependency(SessionFactory)
DB = Annotated[Session, Depends(get_db)]


def make_provider():
    if settings.provider.lower() == "docker":
        return DockerProvider(settings)
    return MockProvider(settings.public_base_url)


provider = make_provider()
orchestrator = WorkspaceOrchestrator(SessionFactory, provider, settings.workspace_root)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    with SessionFactory() as db:
        seed_templates(db)
    yield


app = FastAPI(
    title="EmbodiedCloud API",
    version=__version__,
    description="Browser-first Isaac Lab workspace control plane",
    lifespan=lifespan,
)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(static_dir / "index.html")


@app.get("/api/health", response_model=HealthOut)
def health():
    ready, detail = provider.health()
    return HealthOut(
        status="ok",
        provider=provider.name,
        provider_ready=ready,
        provider_detail=detail,
        version=__version__,
    )


@app.get("/api/templates", response_model=list[TemplateOut])
def list_templates(db: DB):
    stmt = (
        select(Template)
        .where(Template.enabled.is_(True))
        .order_by(Template.category, Template.name)
    )
    return list(db.scalars(stmt))


@app.get("/api/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(db: DB):
    return list(db.scalars(select(Workspace).order_by(Workspace.created_at.desc())))


@app.get("/api/workspaces/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(workspace_id: str, db: DB):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(404, "workspace not found")
    return workspace


@app.get("/api/workspaces/{workspace_id}/access", response_model=WorkspaceAccessOut)
def get_workspace_access(workspace_id: str, db: DB):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(404, "workspace not found")
    return WorkspaceAccessOut(
        workspace_id=workspace.id,
        status=workspace.status,
        ide_url=workspace.ide_url,
        ide_password=workspace.password,
        stream_hint=workspace.stream_hint,
        signal_port=workspace.signal_port,
        media_port=workspace.media_port,
    )


@app.post("/api/workspaces", response_model=WorkspaceOut, status_code=201)
def create_workspace(payload: WorkspaceCreate, db: DB):
    template = db.get(Template, payload.template_id)
    if template is None or not template.enabled:
        raise HTTPException(404, "template not found")
    workspace = orchestrator.create(db, template, payload.name)
    if payload.auto_start:
        orchestrator.start_async(workspace.id)
    return workspace


@app.post("/api/workspaces/{workspace_id}/start", response_model=WorkspaceOut)
def start_workspace(workspace_id: str, db: DB):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(404, "workspace not found")
    if workspace.status in {WorkspaceStatus.PROVISIONING.value, WorkspaceStatus.RUNNING.value}:
        return workspace
    workspace.status = WorkspaceStatus.QUEUED.value
    workspace.error_message = None
    db.commit()
    orchestrator.start_async(workspace.id)
    db.refresh(workspace)
    return workspace


@app.post("/api/workspaces/{workspace_id}/stop", response_model=WorkspaceOut)
def stop_workspace(workspace_id: str, db: DB):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(404, "workspace not found")
    return orchestrator.stop(db, workspace)


@app.delete("/api/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str, db: DB):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(404, "workspace not found")
    orchestrator.destroy(db, workspace)


@app.get("/api/usage", response_model=UsageOut)
def usage(db: DB):
    workspaces = list(db.scalars(select(Workspace)))
    running = [w for w in workspaces if w.status == WorkspaceStatus.RUNNING.value]
    now = datetime.now(UTC)
    seconds_by_workspace: dict[str, int] = {}
    for w in workspaces:
        live_seconds = 0
        if w.status == WorkspaceStatus.RUNNING.value and w.started_at:
            started = w.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            live_seconds = max(0, int((now - started).total_seconds()))
        seconds_by_workspace[w.id] = w.accumulated_seconds + live_seconds
    seconds = sum(seconds_by_workspace.values())
    template_rates = {t.id: t.estimated_hourly_cost_cny for t in db.scalars(select(Template))}
    estimated = sum((seconds_by_workspace[w.id] / 3600.0) * template_rates.get(w.template_id, 0.0) for w in workspaces)
    return UsageOut(
        running_workspaces=len(running),
        total_workspaces=len(workspaces),
        accumulated_gpu_seconds=seconds,
        estimated_cost_cny=round(estimated, 2),
    )


@app.get("/demo-workspace/{workspace_id}", response_class=HTMLResponse, include_in_schema=False)
def demo_workspace(workspace_id: str, db: DB, view: str | None = None):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(404, "workspace not found")
    template = db.get(Template, workspace.template_id)
    mode = "仿真流演示" if view == "stream" else "浏览器 IDE 演示"
    command = template.launch_command if template else ""
    return f"""
    <!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>
    <title>{mode}</title><style>
    body{{margin:0;background:#090e1a;color:#e8edf8;font-family:ui-monospace,Menlo,monospace}}
    header{{padding:14px 20px;background:#111a2e;border-bottom:1px solid #273455}}
    .grid{{display:grid;grid-template-columns:240px 1fr;height:calc(100vh - 55px)}}
    aside{{padding:16px;border-right:1px solid #273455;color:#93a4c7}}
    main{{padding:24px}}
    code,pre{{background:#050913;border:1px solid #273455;border-radius:10px;padding:16px;display:block}}
    code,pre{{white-space:pre-wrap}}
    .ok{{color:#5be49b}} .muted{{color:#7e90b8}}
    </style></head><body><header>EmbodiedCloud / {mode} · <span class='ok'>RUNNING</span></header>
    <div class='grid'><aside>Explorer<br><br>project/<br>├── README.md<br>└── experiments/</aside><main>
    <h2>{workspace.name}</h2><p class='muted'>这是 mock provider 的可交互产品演示页，不消耗真实 GPU。</p>
    <p>真实 GPU 模式会在同一位置打开 code-server；Streaming 模板会返回 Isaac Sim WebRTC 端点。</p>
    <pre>$ cd /workspace/IsaacLab\n$ {command}\n\n[EmbodiedCloud] workspace {workspace.id[:8]} ready.</pre>
    </main></div></body></html>
    """


def run():
    import uvicorn
    uvicorn.run("app.main:app", host=settings.bind_host, port=settings.bind_port, reload=False)


if __name__ == "__main__":
    run()
