# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app import models  # noqa: F401 关键：确保 ORM 模型被加载
from app.db import Base, engine
from app.routes.auth_routes import router as auth_router
from app.routes.chat_routes import router as chat_router

# =========================
# App 基本信息
# =========================
app = FastAPI(
    title="AI Elder Chat MVP",
    version="0.1.0",
    description="AI 对话产品 MVP（老年人沟通辅助）",
)

# =========================
# 中间件
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # MVP 阶段放开
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# 启动时建表
# =========================
@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)

# =========================
# Health Check
# =========================
@app.get("/health", tags=["default"])
def health():
    return {"status": "ok"}

# =========================
# 路由注册
# =========================
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(chat_router, prefix="/chat", tags=["chat"])

# =========================
# Swagger Authorize（Bearer Token）
# =========================
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # 定义 Bearer Token 鉴权
    openapi_schema.setdefault("components", {})
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
    }

    # 全局应用（Swagger 右上角会出现 Authorize）
    openapi_schema["security"] = [{"BearerAuth": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
