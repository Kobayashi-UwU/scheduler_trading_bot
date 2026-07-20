import datetime as dt
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.cycle import run_analysis_cycle, run_price_check

log = logging.getLogger("scheduler")

scheduler = BackgroundScheduler()


def start_scheduler() -> None:
    scheduler.add_job(
        run_analysis_cycle,
        "interval",
        minutes=settings.analysis_interval_min,
        id="analysis_cycle",
        next_run_time=dt.datetime.now(),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_price_check,
        "interval",
        minutes=settings.price_check_interval_min,
        id="price_check",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info(
        "scheduler started: analysis every %dmin, price check every %dmin",
        settings.analysis_interval_min,
        settings.price_check_interval_min,
    )


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
