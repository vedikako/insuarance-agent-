import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
from fastapi.responses import FileResponse

from app.api.auth import router as auth_router
from app.api.claims import router as claims_router
from app.api.fraud import router as fraud_router
from app.api.policies import router as policies_router
from app.api.policy_chat import router as policy_chat_router
from app.api.reviews import router as reviews_router
from app.api.settlements import router as settlements_router

app = FastAPI(title="InsureFlow AI")
app.include_router(auth_router, prefix="/api")
app.include_router(claims_router, prefix="/api")
app.include_router(policies_router, prefix="/api")
app.include_router(policy_chat_router, prefix="/api")
app.include_router(reviews_router, prefix="/api")
app.include_router(settlements_router, prefix="/api")
app.include_router(fraud_router, prefix="/api")

_STATIC = Path(__file__).resolve().parent / "static"


@app.get("/")
async def home_page():
    return FileResponse(_STATIC / "home.html")


@app.get("/chat")
async def chat_page():
    return FileResponse(_STATIC / "chat.html")


@app.get("/review")
async def review_page():
    return FileResponse(_STATIC / "review.html")


@app.get("/file")
async def file_page():
    return FileResponse(_STATIC / "file.html")


@app.get("/status")
async def status_page():
    return FileResponse(_STATIC / "status.html")


app.mount("/static", StaticFiles(directory=_STATIC), name="static")