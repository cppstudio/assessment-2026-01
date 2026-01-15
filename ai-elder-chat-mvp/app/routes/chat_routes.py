# app/routes/chat_routes.py
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ChatRequest, ChatResponse
from app.auth import get_user_id_from_authorization
from app import models

from app.llm import llm_reply

router = APIRouter()


def stub_llm_reply(user_message: str) -> str:
    return llm_reply(user_message)


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime

    class Config:
        from_attributes = True  # Pydantic v2


@router.post("", response_model=ChatResponse)  # main.py 里 prefix="/chat"，所以这里就是 POST /chat
def chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    user_id = get_user_id_from_authorization(db, authorization)

    user_text = (payload.message or "").strip()
    if not user_text:
        # 极简：空消息直接当作 422（你也可以改成返回提示）
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="message cannot be empty")

    # 1) 生成回复（stub）
    reply_text = stub_llm_reply(user_text)

    # 2) 落库两条记录：user + assistant
    db.add(models.Message(user_id=user_id, role="user", content=user_text))
    db.add(models.Message(user_id=user_id, role="assistant", content=reply_text))
    db.commit()

    return {"reply": reply_text}


@router.get("/history", response_model=List[MessageOut])  # GET /chat/history
def history(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    limit: int = Query(20, ge=1, le=200),
):
    user_id = get_user_id_from_authorization(db, authorization)

    rows = (
        db.query(models.Message)
        .filter(models.Message.user_id == user_id)
        .order_by(models.Message.created_at.desc())
        .limit(limit)
        .all()
    )

    # 返回按时间正序（更像聊天记录）
    return list(reversed(rows))
