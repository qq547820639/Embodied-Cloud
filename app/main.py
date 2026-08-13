import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select

from . import __version__
from .deps import DB, CurrentUser, SessionFactory, provider, run_crash_recovery, settings
from .logging_setup import RequestIDMiddleware, configure_logging
from .metrics import GPU_ALLOCATED, WORKSPACE_RUNNING
from .models import Gpu, GpuStatus, Role, Workspace, WorkspaceStatus
from .routers import auth, courses, deployments, edge, gpus, streaming, templates, usage, workspaces
from .schemas import HealthOut

configure_logging(settings)
logger = logging.getLogger("embodiedcloud")


async def _gauge_loop():
    """周期刷新 Prometheus Gauge（workspace_running / gpu_allocated）。"""
    while True:
        try:
            with SessionFactory() as db:
                running = db.scalar(
                    select(func.count(Workspace.id)).where(
                        Workspace.status == WorkspaceStatus.RUNNING.value
                    )
                )
                allocated = db.scalar(
                    select(func.count(Gpu.id)).where(
                        Gpu.status == GpuStatus.ALLOCATED.value
                    )
                )
            WORKSPACE_RUNNING.set(int(running or 0))
            GPU_ALLOCATED.set(int(allocated or 0))
        except Exception as exc:  # 指标刷新失败不影响主服务
            logger.warning("gauge refresh failed: %s", exc)
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    from .deps import bootstrap_db, worker

    bootstrap_db()
    # 启动恢复：基于 runtime 事实的 reconciliation（幂等，不误杀存活 runtime）
    run_crash_recovery()
    # DB-backed operation worker：PENDING/RETRYING 任务跨重启不丢
    worker.start()
    task = asyncio.create_task(_gauge_loop())
    yield
    worker.stop()
    task.cancel()


app = FastAPI(
    title="EmbodiedCloud API",
    version=__version__,
    description="Browser-first Isaac Lab workspace control plane",
    lifespan=lifespan,
)
app.add_middleware(RequestIDMiddleware)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(auth.router, prefix="/api")
app.include_router(templates.router, prefix="/api")
app.include_router(workspaces.router, prefix="/api")
app.include_router(gpus.router, prefix="/api")
app.include_router(usage.router, prefix="/api")
app.include_router(streaming.router, prefix="/api")
app.include_router(courses.router, prefix="/api")
app.include_router(edge.router, prefix="/api")
app.include_router(deployments.router, prefix="/api")


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


@app.get("/metrics", include_in_schema=False)
def metrics():
    return HTMLResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/demo-workspace/{workspace_id}", response_class=HTMLResponse, include_in_schema=False)
def demo_workspace(workspace_id: str, db: DB, user: CurrentUser, view: str | None = None):
    from .models import Template
    from .models import Workspace as W

    workspace = db.get(W, workspace_id)
    if workspace is None:
        return HTMLResponse("workspace not found", status_code=404)
    # §6/SECURITY.md T1：owner/org 隔离，越权一律 404（不泄露资源存在性）
    if user.role != Role.ADMIN.value and workspace.user_id != user.id:
        return HTMLResponse("workspace not found", status_code=404)
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
