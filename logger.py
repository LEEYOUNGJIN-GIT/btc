"""콘솔과 trading.log 에 동시에 남기는 로거."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "scalper"

logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


def setup_logger(log_file: str, level: str = "INFO") -> logging.Logger:
    """핸들러를 한 번만 붙입니다. 주문·체결·순손익·오류가 같은 형식으로 남습니다."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if any(not isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
