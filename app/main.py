import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db.database import init_db
from app.scheduler import start_scheduler, stop_scheduler
from app.web.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="AI Gold Trader", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory="app/web/static", html=True), name="static")
