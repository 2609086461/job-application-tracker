"""SSE 日志总线：后台任务通过 bus.log(msg, channel) 广播，前端 EventSource 订阅 /api/stream。"""
import json
import queue
import threading
import time
from datetime import datetime

# 每个前端连接一个 Queue，broadcast 时逐个 put。
_subscribers: list[queue.Queue] = []
_lock = threading.Lock()
# 保留最近日志，方便新连接补看历史。
_history: list[dict] = []
_HISTORY_MAX = 200


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=500)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def log(message: str, channel: str = "system", level: str = "info") -> None:
    """广播一条日志到所有订阅者。channel 用于前端分区（system/command/scan/discover）。"""
    evt = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "channel": channel,
        "level": level,
        "message": str(message),
    }
    with _lock:
        _history.append(evt)
        if len(_history) > _HISTORY_MAX:
            _history.pop(0)
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(evt)
        except queue.Full:
            pass


def history() -> list[dict]:
    with _lock:
        return list(_history)


def event_stream(q: queue.Queue):
    """生成 SSE 数据流。首帧补发历史，之后阻塞等待新事件，定期发心跳防超时。"""
    for evt in history():
        yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
    while True:
        try:
            evt = q.get(timeout=15)
            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        except queue.Empty:
            yield f": keepalive {int(time.time())}\n\n"
