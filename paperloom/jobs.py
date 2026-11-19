from __future__ import annotations

import json
import os
import threading
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .llm import Cancelled
from .models import Document, LLMConfig
from .pipeline import prepare


def now():
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="paperloom")
        self.events: dict[str, threading.Event] = {}
        self.states = {}
        for file in root.glob("*/status.json"):
            try:
                state = json.loads(file.read_text(encoding="utf-8"))
                if state["status"] in ("queued", "running", "cancelling"):
                    state.update(status="interrupted", message="服务已重启，可使用已有缓存重试")
                elif state['status']=='ready' and '校对' in state.get('message',''):
                    state['message']='文献已准备完成，可以阅读'
                self.states[state["id"]] = state
            except (OSError, ValueError, KeyError):
                continue

    def directory(self, job_id: str) -> Path:
        if job_id not in self.states:
            raise KeyError(job_id)
        return self.root / job_id

    def snapshot(self, job_id=None):
        with self.lock:
            value = self.states[job_id] if job_id else sorted(self.states.values(), key=lambda v: v["created"], reverse=True)
            return json.loads(json.dumps(value))

    def save(self, state):
        temp = (self.root / state["id"] / "status.json").with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(temp.with_suffix(".json"))

    def new(self, data: bytes, filename: str, config: LLMConfig) -> str:
        with self.lock:
            active = sum(s["status"] in ("queued", "running", "cancelling") for s in self.states.values())
            if active >= 6:
                raise ValueError("最多允许 6 个进行中的任务，请稍后再提交")
            job_id = str(uuid.uuid4())
            directory = self.root / job_id
            directory.mkdir()
            (directory / "source.pdf").write_bytes(data)
            state = {"id": job_id, "filename": filename, "created": now(), "status": "queued", "stage": "queued",
                     "progress": 0, "message": "等待处理", "events": [], "config": config.safe_dump(), "revision": 0}
            self.states[job_id] = state
            self.save(state)
            self.start(job_id, config)
            return job_id

    def start(self, job_id: str, config: LLMConfig):
        with self.lock:
            state = self.states[job_id]
            if job_id in self.events and not self.events[job_id].is_set():
                raise ValueError("任务已经在运行")
            state.update(status="queued", stage="queued", progress=0, message="等待处理", error=None,
                         config=config.safe_dump())
            self.save(state)
            cancel = threading.Event()
            self.events[job_id] = cancel
            self.executor.submit(self.run, job_id, config, cancel)

    def update(self, job_id, stage, progress, message):
        with self.lock:
            state = self.states[job_id]
            changed = state["stage"] != stage
            state.update(stage=stage, progress=progress, message=message,
                         status="cancelling" if self.events[job_id].is_set() else "running")
            if changed:
                state["events"].append({"at": now(), "stage": stage, "message": message})
            self.save(state)

    def run(self, job_id, config, cancel):
        directory = self.directory(job_id)
        def audit(entry):
            with self.lock, (directory / "audit.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": now(), **entry}, ensure_ascii=False) + "\n")
        try:
            doc = prepare(directory / "source.pdf", directory, job_id, self.states[job_id]["filename"], config, cancel,
                          lambda *args: self.update(job_id, *args), audit)
            with self.lock:
                state = self.states[job_id]
                state.update(status="ready", stage="ready", progress=100, message="文献准备与自动校验已完成，可以阅读",
                             title=doc.metadata.title, issues=len(doc.issues), revision=state["revision"] + 1)
                self.save(state)
        except Cancelled:
            with self.lock:
                self.states[job_id].update(status="cancelled", message="任务已取消，缓存保留，可重试")
                self.save(self.states[job_id])
        except Exception as exc:
            with self.lock:
                # Do not expose request bodies, response bodies, tracebacks or credentials.
                message = str(exc) if isinstance(exc, (ValueError,)) or exc.__class__.__name__ == "LLMError" else "处理失败，请检查 PDF 是否有效并重试"
                self.states[job_id].update(status="failed", message=message[:1200], error=message[:1200])
                self.save(self.states[job_id])
        finally:
            with self.lock:
                self.events.pop(job_id, None)

    def cancel(self, job_id):
        with self.lock:
            event = self.events.get(job_id)
            if event:
                event.set()
                self.states[job_id].update(status="cancelling", message="正在取消；当前模型请求结束后停止")
                self.save(self.states[job_id])

    def close(self):
        for event in list(self.events.values()):
            event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def delete(self,job_id):
        with self.lock:
            state=self.states[job_id]
            if state['status'] not in ('cancelled','failed','interrupted') or job_id in self.events:
                raise ValueError('只能删除已经停止的取消、失败或中断任务；运行中的任务需要先取消并等待结束')
            directory=self.directory(job_id);root=self.root.resolve()
            if directory.is_symlink() or directory.resolve().parent!=root or str(uuid.UUID(job_id))!=job_id:
                raise ValueError('任务目录无效，拒绝删除')
            if directory.exists():shutil.rmtree(directory)
            self.states.pop(job_id)
