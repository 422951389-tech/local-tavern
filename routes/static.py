"""静态文件与首页路由。"""
from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.config import WEB_DIR

router = APIRouter()


@router.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


class NoCacheStaticFiles(StaticFiles):
    """覆盖 StaticFiles，强制禁用浏览器缓存，避免前端更新后用户必须手动清缓存。"""
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def mount_static(app):
    app.mount("/static", NoCacheStaticFiles(directory=str(WEB_DIR)), name="static")
