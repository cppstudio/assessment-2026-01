# app/config.py
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data.db")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
MODEL_NAME = os.getenv("MODEL_NAME", "")