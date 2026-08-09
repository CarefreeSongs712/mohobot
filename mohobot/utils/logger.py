"""Loguru-based logging with rotation."""

import sys
from pathlib import Path

from loguru import logger

from mohobot.utils.time_utils import TZ_UTC8


def _utc8_time(record: dict) -> str:
    """Render log timestamp in UTC+8 regardless of system timezone."""
    return record["time"].astimezone(TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _console_format(record: dict) -> str:
    """Console template with the timestamp pre-rendered in UTC+8."""
    return (
        f"<green>{_utc8_time(record)}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )


def _file_format(record: dict) -> str:
    """File template with the timestamp pre-rendered in UTC+8."""
    return (
        f"{_utc8_time(record)} | "
        "{level: <8} | {name}:{function}:{line} | {message}"
    )


def setup_logger(log_dir: str | Path = "./logs") -> None:
    """Configure loguru with file rotation and console output."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Remove default handler
    logger.remove()

    # Console handler — colorized, structured
    logger.add(
        sys.stderr,
        format=_console_format,
        level="DEBUG",
        colorize=True,
        backtrace=True,
        diagnose=False,
    )

    # File handler — rotation at 10 MB, keep 7 days
    logger.add(
        log_path / "mohobot_{time:YYYY-MM-DD}.log",
        format=_file_format,
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        compression="gz",
        backtrace=True,
        diagnose=True,
    )

    logger.info("Logger initialized — file rotation at 10 MB, 7 day retention")


# Re-export for convenience
__all__ = ["logger", "setup_logger"]
