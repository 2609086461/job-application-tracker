"""_state 全局状态：记录后台任务运行情况和数据缓存，前端轮询 /api/status 读取。"""
import threading
import time

_lock = threading.Lock()

_state = {
    "scanning": False,       # 是否有扫描任务在跑
    "last_scan": None,       # 最近一次扫描结果摘要
    "cache": None,           # 看板数据缓存
    "cache_at": 0,           # 缓存时间戳
    "started_at": time.time(),
}


def get() -> dict:
    with _lock:
        return dict(_state)


def update(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def set_cache(data: dict) -> None:
    with _lock:
        _state["cache"] = data
        _state["cache_at"] = time.time()


def get_cache(max_age: float = 30.0):
    with _lock:
        if _state["cache"] and (time.time() - _state["cache_at"]) < max_age:
            return _state["cache"]
    return None
