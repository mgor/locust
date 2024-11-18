from __future__ import annotations

import atexit
import logging
import logging.config
import re
import socket
from collections import deque
from logging.config import ConvertingDict, ConvertingList, valid_ident
from logging.handlers import QueueHandler, QueueListener
from time import time
from typing import Callable, cast

from gevent import Greenlet
from gevent.queue import Full, Queue

HOSTNAME = re.sub(r"\..*", "", socket.gethostname())

# Global flag that we set to True if any unhandled exception occurs in a greenlet
# Used by main.py to set the process return code to non-zero
unhandled_greenlet_exception = False

logger = logging.getLogger(__name__)


class LogReader(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.logs = deque(maxlen=500)

    def emit(self, record) -> None:
        self.logs.append(self.format(record))
        
        
class QueueListenerHandler(QueueHandler):
    def __init__(self, handlers: list[logging.Handler] | ConvertingList, respect_handler_level: bool = False, auto_run: bool = True, queue: Queue | ConvertingDict=Queue(-1)) -> None:
        queue = self.resolve_queue(queue)
        super().__init__(queue)
        
        handlers = self.resolve_handlers(handlers)
        self._listener = QueueListener(
            self.queue,
            *handlers,
            respect_handler_level=respect_handler_level,
        )

        self._discarding_started: float | None = None
        self._discarding_count: int = 0
        
        if auto_run:
            self.start()
            atexit.register(self.stop)
            
    def start(self) -> None:
        self._listener.start()
        
    def stop(self) -> None:
        self._listener.stop()
        
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        
    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            super().enqueue(record)

            if self._discarding_started is not None:
                delta = time() - self._discarding_started
                logger.warning("Discarded %d log messages during %.2f seconds due to excessive logging resulting in log queue being full", self._discarding_count, delta)
                
            self._discarding_started = None
            self._discarding_count = 0
        except Full:
            if self._discarding_started is None:
                self._discarding_started = time()

            self._discarding_count += 1

    @classmethod
    def resolve_queue(cls, queue: Queue | ConvertingDict) -> Queue:
        if isinstance(queue, Queue):
            return queue
            
        if '__resolved_value__' in queue:
            return cast(Queue, queue['__resolved_value__'])
            
        class_name = queue.pop('class')
        class_type = queue.configurator.resolve(class_name) # type: ignore
        properties = queue.pop('.', None)
        kwargs = {key: queue[key] for key in queue if isinstance(key, str) and valid_ident(key)}
        instance = class_type(**kwargs)
        
        if properties:
            for name, value in properties.items():
                setattr(instance, name, value)
                
        queue['__resolved_value__'] = instance
        
        return instance
        
    @classmethod
    def resolve_handlers(cls, handlers: list[logging.Handler] | ConvertingList) -> list[logging.Handler]:
        if not isinstance(handlers, ConvertingList):
            return handlers
            
        return [handlers[index] for index in range(len(handlers))]


def setup_logging(loglevel, logfile: str | None = None, maxsize: int = 10000) -> None:
    loglevel = loglevel.upper()

    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "objects": {
            "queue": {
                "class": "gevent.queue.Queue",
                "maxsize": maxsize,
            },
        },
        "formatters": {
            "default": {
                "format": f"[%(asctime)s] {HOSTNAME}/%(levelname)s/%(name)s: %(message)s",
            },
            "plain": {
                "format": "%(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            },
            "console_plain": {
                "class": "logging.StreamHandler",
                "formatter": "plain",
            },
            "log_reader": {"class": "locust.log.LogReader", "formatter": "default"},
            "queue_listener": {
                "class": "locust.log.QueueListenerHandler",
                "handlers": ["cfg://handlers.console", "cfg://handlers.console_plain", "cfg://handlers.log_reader"],
                "queue": "cfg://objects.queue",
            }
        },
        "loggers": {
            "locust": {
                "handlers": ["queue_listener"],
                "level": loglevel,
                "propagate": False,
            },
            "locust.stats_logger": {
                "handlers": ["queue_listener"],
                "level": "INFO",
                "propagate": False,
            },
        },
        "root": {
            "handlers": ["queue_listener"],
            "level": loglevel,
        },
    }
    if logfile:
        # if a file has been specified add a file logging handler and set
        # the locust and root loggers to use it
        LOGGING_CONFIG["handlers"]["file"] = {
            "class": "logging.FileHandler",
            "filename": logfile,
            "formatter": "default",
        }
        LOGGING_CONFIG["loggers"]["locust"]["handlers"] = ["file", "log_reader"]
        LOGGING_CONFIG["root"]["handlers"] = ["file", "log_reader"]

    logging.config.dictConfig(LOGGING_CONFIG)


def get_logs() -> list[str]:
    log_reader_handler = [handler for handler in logging.getLogger("root").handlers if handler.name == "log_reader"]

    if log_reader_handler:
        return list(cast(LogReader, log_reader_handler[0]).logs)

    return []


def greenlet_exception_logger(logger: logging.Logger, level=logging.CRITICAL) -> Callable[[Greenlet], None]:
    """
    Return a function that can be used as argument to Greenlet.link_exception() that will log the
    unhandled exception to the given logger.
    """

    def exception_handler(greenlet: Greenlet) -> None:
        if greenlet.exc_info is not None and greenlet.exc_info[0] == SystemExit:
            logger.log(
                min(logging.INFO, level),  # dont use higher than INFO for this, because it sounds way to urgent
                "sys.exit(%s) called (use log level DEBUG for callstack)" % greenlet.exc_info[1],
            )
            logger.log(logging.DEBUG, "Unhandled exception in greenlet: %s", greenlet, exc_info=greenlet.exc_info)
        else:
            logger.log(level, "Unhandled exception in greenlet: %s", greenlet, exc_info=greenlet.exc_info)
        global unhandled_greenlet_exception
        unhandled_greenlet_exception = True

    return exception_handler
