import asyncio
import threading

from PySide6.QtCore import QObject, Signal

_loop = None
_loop_lock = threading.Lock()

_dispatcher = None


class _Dispatcher(QObject):
    invoke = Signal(object)

    def __init__(self):
        super().__init__()
        self.invoke.connect(self._dispatch)

    def _dispatch(self, callback):
        callback()


def init_dispatcher():
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _Dispatcher()
    return _dispatcher


def get_dispatcher():
    global _dispatcher
    return _dispatcher


def _get_dispatcher():
    return get_dispatcher()


def _start_loop():
    global _loop
    loop = asyncio.new_event_loop()
    _loop = loop
    loop.run_forever()


def _ensure_loop():
    global _loop
    if _loop is None:
        with _loop_lock:
            if _loop is None:
                t = threading.Thread(target=_start_loop, daemon=True)
                t.start()
                while _loop is None:
                    threading.Event().wait(0.01)
    return _loop


def run_async(coro):
    if asyncio.iscoroutine(coro):
        loop = _ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()
    return coro


def run_in_background(func, on_success, on_error=None):
    def _execute():
        try:
            if asyncio.iscoroutinefunction(func):
                coro = func()
                loop = _ensure_loop()
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                result = future.result()
            elif asyncio.iscoroutine(func):
                loop = _ensure_loop()
                future = asyncio.run_coroutine_threadsafe(func, loop)
                result = future.result()
            else:
                result = func()
            dispatcher = _get_dispatcher()
            if dispatcher is not None:
                dispatcher.invoke.emit(lambda: on_success(result))
        except Exception as exc:
            if on_error is not None:
                dispatcher = _get_dispatcher()
                if dispatcher is not None:
                    dispatcher.invoke.emit(lambda exc=exc: on_error(exc))

    threading.Thread(target=_execute, daemon=True).start()