"""Local Tavern — FastAPI 主程序

启动：uvicorn server:app --host 127.0.0.1 --port 8765
或双击 start.bat

v2: 项目+存档双层架构
  data/projects/<项目>/
    ├── characters/    角色卡（存档间共享）
    ├── worldbook/     世界设定（存档间共享）
    ├── user.yaml      用户档案
    └── saves/         存档（状态独立）
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.character_loader import needs_migration, migrate_old_data
from core.ollama_client import get_client
from routes import (
    models,
    projects,
    characters,
    user,
    worldbook,
    settings,
    sessions,
    chat,
    messages,
    prompts,
    static,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ===== lifespan：替代已弃用的 @app.on_event("startup"/"shutdown") =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    if needs_migration():
        logger.info("检测到旧数据，开始迁移……")
        name = migrate_old_data()
        logger.info("已迁移到 data/projects/%s/", name)
    yield
    # ---- shutdown ----
    await get_client().close()


app = FastAPI(title="Local Tavern", lifespan=lifespan)

# 静态文件（需在 include_router 之前 mount，避免被路由覆盖）
static.mount_static(app)

# 注册路由
app.include_router(models.router)
app.include_router(projects.router)
app.include_router(characters.router)
app.include_router(user.router)
app.include_router(worldbook.router)
app.include_router(settings.router)
app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(messages.router)
app.include_router(prompts.router)
app.include_router(static.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
