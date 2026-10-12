import logging
from pathlib import Path

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
from fastapi.responses import FileResponse

from app.api.auth import router as auth_router
from app.api.policies import router as policies_router
from app.api.policy_chat import router as policy_chat_router
from app.api.reviews import router as reviews_router

app = FastAPI(title="InsureFlow AI")
app.include_router(auth_router, prefix="/api")
app.include_router(policies_router, prefix="/api")
app.include_router(policy_chat_router, prefix="/api")
app.include_router(reviews_router, prefix="/api")

_CHAT_PAGE = Path(__file__).resolve().parent / "static" / "chat.html"


@app.get("/")
async def chat_page():
    return FileResponse(_CHAT_PAGE)
