"""HTTP boundary for the simulated maintenance workflow."""

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from maintenance_agent.service import MaintenanceService

logger = logging.getLogger(__name__)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=2, max_length=4000)


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["provide_info", "confirm", "cancel"]
    text: str | None = Field(default=None, max_length=4000)
    draft_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_action_fields(self):
        if self.action == "provide_info" and (not self.text or len(self.text) < 2):
            raise ValueError("补充信息至少需要两个字符")
        if self.action == "confirm" and not self.draft_id:
            raise ValueError("确认必须携带服务端返回的当前 draft_id")
        if self.action != "provide_info" and self.text is not None:
            raise ValueError("确认/取消不能同时修改报修信息")
        return self


def create_app(service_factory: Callable[[], MaintenanceService] = MaintenanceService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service_factory()
        yield
        close = getattr(app.state.service, "close", None)
        if callable(close):
            close()

    app = FastAPI(
        title="制造设备报修与工单流转智能体（模拟数据）",
        version="0.1.0",
        description="仅用于报修分类和工单流转演示，所有设备、规则和工单均为模拟数据。",
        lifespan=lifespan,
    )

    @app.exception_handler(KeyError)
    async def missing_resource(_request: Request, _exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": "任务或工单不存在"})

    @app.exception_handler(ValueError)
    async def invalid_transition(_request: Request, exc: ValueError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unavailable(_request: Request, exc: Exception):
        # Provider exception messages can contain URLs or request metadata; keep them off the API.
        logger.error("Workflow request failed (%s)", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"detail": "服务暂时不可用，请检查后端环境配置和本地日志"},
        )

    @app.post("/tasks", status_code=201, tags=["tasks"])
    def create_task(body: TaskCreate, request: Request):
        return request.app.state.service.create_task(body.text)

    @app.get("/tasks/{task_id}", tags=["tasks"])
    def get_task(task_id: str, request: Request):
        return request.app.state.service.get_task(task_id)

    @app.post("/tasks/{task_id}/responses", tags=["tasks"])
    def respond(task_id: str, body: TaskResponse, request: Request):
        return request.app.state.service.respond(
            task_id, body.action, text=body.text, draft_id=body.draft_id
        )

    @app.get("/work-orders", tags=["work-orders"])
    def list_orders(request: Request):
        return request.app.state.service.list_orders()

    @app.get("/work-orders/{order_id}", tags=["work-orders"])
    def get_order(order_id: str, request: Request):
        return request.app.state.service.get_order(order_id)

    @app.get("/health", tags=["health"])
    def health(request: Request):
        return request.app.state.service.health()

    return app


app = create_app()
