"""FastAPI 入口：注册路由与 CORS。"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.utils.tile_queue import configured_renderer
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.routers import admin, algorithms, detect, export, overlay, poi, thumbnail, wayback

@asynccontextmanager
async def lifespan(app):
    renderer = configured_renderer()
    app.state.tile_renderer = renderer
    try:
        await renderer.start()
        yield
    finally:
        from app.utils.model_process import stop_all
        stop_all()
        await app.state.tile_renderer.close()


app = FastAPI(title="Change Detection Backend (Python)", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(algorithms.router)
app.include_router(wayback.router)
app.include_router(detect.router)
app.include_router(thumbnail.router)
app.include_router(overlay.router)
app.include_router(export.router)
app.include_router(poi.router)


@app.get("/")
def root():
    return {"service": "change-detection-backend-py", "version": app.version}


app.include_router(admin.router)

async def private_compute_routes(request, call_next):
    import secrets
    from app.settings import DATA_ROOT
    from fastapi.responses import JSONResponse
    path = request.url.path
    # All file paths exposed to a browser are confined to managed runtime data.
    if path.startswith('/py/overlay/'):
        from app.settings import managed_path
        try:
            if request.method == 'GET':
                managed_path(request.query_params.get('path', ''))
            else:
                payload = await request.json()
                managed_path(payload.get('mask_path') or payload.get('image_path') or payload.get('path') or '')
        except (ValueError, TypeError):
            return JSONResponse({'error':'预览路径必须位于运行数据目录内'}, status_code=400)
    if path.startswith('/py/admin/') or path in ('/py/detect','/py/local/fetch','/py/wayback/fetch','/py/thumbnail'):
        try:
            token = (DATA_ROOT / 'control.token').read_text().strip()
        except OSError:
            token = ''
        if not token or not secrets.compare_digest(request.headers.get('X-CDP-Control',''),token):
            return JSONResponse({'error':'private computation endpoint'},status_code=403)
    if path in ('/py/detect', '/py/local/fetch', '/py/wayback/fetch'):
        from app.settings import managed_path
        try:
            payload = await request.json()
            managed_path(payload.get('output_dir', ''), output=True)
            if path == '/py/detect':
                managed_path(payload.get('before_path', ''))
                managed_path(payload.get('after_path', ''))
        except (ValueError, TypeError):
            return JSONResponse({'error':'计算路径必须位于运行数据目录内'},status_code=400)
    return await call_next(request)


class PrivateComputeMiddleware(BaseHTTPMiddleware):
    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        protected = path.startswith(('/py/admin/', '/py/overlay/')) or path in (
            '/py/detect', '/py/local/fetch', '/py/wayback/fetch', '/py/thumbnail')
        if scope['type'] == 'http' and protected:
            await super().__call__(scope, receive, send)
        else:
            # BaseHTTPMiddleware wraps receive in a task group. Its cancellation
            # checkpoint hides http.disconnect from Request.is_disconnected().
            # Public tiles need the original ASGI receive to drop obsolete work.
            await self.app(scope, receive, send)


app.add_middleware(PrivateComputeMiddleware, dispatch=private_compute_routes)
