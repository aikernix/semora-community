"""Bound concurrent scans and cancel native scans across their connection phases."""
from contextlib import contextmanager
import threading
from fastapi import HTTPException

LOCAL=threading.local()
LOCK=threading.RLock()
ACTIVE={}
SLOTS=threading.BoundedSemaphore(2)

def cancelled():
    state=getattr(LOCAL,"state",None)
    return bool(state and state["cancelled"])

@contextmanager
def operation(identifier):
    if not SLOTS.acquire(blocking=False):raise HTTPException(429,"同时最多运行两个结构扫描")
    state={"cancelled":False,"connection":None}
    try:
        LOCAL.state=state
        with LOCK:
            if identifier:ACTIVE[identifier]=state
        yield
        if state["cancelled"]:raise HTTPException(409,"结构扫描已取消")
    finally:
        with LOCK:
            if identifier:ACTIVE.pop(identifier,None)
        LOCAL.state=None
        SLOTS.release()

@contextmanager
def track(connection):
    state=getattr(LOCAL,"state",None)
    try:
        with LOCK:
            if state:
                if state["cancelled"]:raise HTTPException(409,"结构扫描已取消")
                state["connection"]=connection
        with connection:yield connection
    finally:
        with LOCK:
            if state:state["connection"]=None
        try:connection.close()
        except Exception:pass

def cancel(identifier):
    with LOCK:
        state=ACTIVE.get(identifier)
        if not state:return False
        state["cancelled"]=True
        connection=state.get("connection")
    if connection:
        try:
            if hasattr(connection,"cancel"):connection.cancel()
            else:connection.close()
        except Exception:pass
    return True
