"""Loguru-based logging with rotation."""

import sys
from pathlib import Path

from loguru import logger


def setup_logger(log_dir: str | Path = "./logs") -> None:
    """Configure loguru with file rotation and console output."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Remove default handler
    logger.remove()

    # Console handler — colorized, structured
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        level="DEBUG",
        colorize=True,
        backtrace=True,
        diagnose=False,
    )

    # File handler — rotation at 10 MB, keep 7 days
    logger.add(
        log_path / "mohobot_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
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