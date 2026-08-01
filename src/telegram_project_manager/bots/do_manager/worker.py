from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from telegram_project_manager.bots.do_manager.service import DoService
from telegram_project_manager.bots.goal_manager.service import GoalService


@dataclass(frozen=True)
class ActiveWork:
    kind: str
    work_id: str
    lane: str


class FullAccessWorker:
    def __init__(
        self,
        *,
        do_service: DoService,
        goal_service: GoalService | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        self.do_service = do_service
        self.goal_service = goal_service
        self.poll_interval = poll_interval
        self._tasks: dict[ActiveWork, asyncio.Task[None]] = {}
        self._active_lanes: set[str] = set()
        self._interrupting: set[str] = set()

    async def run(self) -> None:
        await self.do_service.recover()
        if self.goal_service is not None:
            await self.goal_service.recover()
        try:
            while True:
                self._reap_finished()
                await self._process_goal_controls()
                self._schedule_available()
                await asyncio.sleep(self.poll_interval)
        finally:
            await self._shutdown()

    def _schedule_available(self) -> None:
        available = self.do_service.max_concurrent - len(self._tasks)
        if available <= 0:
            return
        for job in self.do_service.queued_jobs():
            if available <= 0:
                return
            lane = self.do_service.lane(job)
            job_id = str(job["id"])
            if lane in self._active_lanes or not self.do_service.claim(job_id):
                continue
            self._start("do", job_id, lane, self.do_service.execute(job_id))
            available -= 1
        if self.goal_service is None:
            return
        for goal in self.goal_service.due_goals():
            if available <= 0:
                return
            lane = self.goal_service.lane(goal)
            goal_id = str(goal["id"])
            if lane in self._active_lanes or not self.goal_service.claim(goal_id):
                continue
            self._start("goal", goal_id, lane, self.goal_service.execute(goal_id))
            available -= 1

    def _start(
        self, kind: str, work_id: str, lane: str, operation: Coroutine[Any, Any, None]
    ) -> None:
        work = ActiveWork(kind, work_id, lane)
        self._active_lanes.add(lane)
        self._tasks[work] = asyncio.create_task(operation, name=f"{kind}-worker-{work_id}")

    async def _process_goal_controls(self) -> None:
        if self.goal_service is None:
            return
        for work in tuple(self._tasks):
            if work.kind != "goal" or work.work_id in self._interrupting:
                continue
            if self.goal_service.control_action(work.work_id):
                self._interrupting.add(work.work_id)
                await self.goal_service.interrupt(work.work_id)

    def _reap_finished(self) -> None:
        for work, task in tuple(self._tasks.items()):
            if not task.done():
                continue
            self._tasks.pop(work, None)
            self._active_lanes.discard(work.lane)
            self._interrupting.discard(work.work_id)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.exception("Unhandled %s worker task failure %s", work.kind, work.work_id)

    async def _shutdown(self) -> None:
        active = tuple(self._tasks.items())
        for work, _ in active:
            if work.kind == "goal" and self.goal_service is not None:
                await self.goal_service.interrupt(work.work_id)
                self.goal_service.mark_stopped(work.work_id)
            else:
                await self.do_service.interrupt(work.work_id)
                self.do_service.mark_stopped(work.work_id)
        for _, task in active:
            task.cancel()
        if active:
            await asyncio.gather(*(task for _, task in active), return_exceptions=True)
        self._tasks.clear()
        self._active_lanes.clear()
        self._interrupting.clear()
