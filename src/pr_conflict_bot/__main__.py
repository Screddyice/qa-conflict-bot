"""Entry point: load config, start webhook server + N workers."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog

from .config import Config, ConfigError, load_from_env
from .orchestrator import worker
from .server import GitHubClient, PRJob, make_app, serve, with_session


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )


async def _run(cfg: Config, num_workers: int = 2) -> None:
    queue: asyncio.Queue[PRJob] = asyncio.Queue(maxsize=128)
    app = make_app(cfg, queue)
    session = await with_session()
    gh = GitHubClient(cfg.github, session)

    workers = [
        asyncio.create_task(worker(queue, cfg, gh), name=f"worker-{i}")
        for i in range(num_workers)
    ]
    await serve(cfg, app)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await session.close()


def main() -> int:
    try:
        cfg = load_from_env()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    _configure_logging(cfg.log_level)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_run(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
