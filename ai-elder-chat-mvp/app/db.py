# app/db.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./data.db"

# SQLite 多线程访问需要 check_same_thread=False（FastAPI 常见）
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """
    FastAPI 依赖：yield 一个 db session
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
