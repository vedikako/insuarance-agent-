from fastapi import FastAPI

from app.api.policy_chat import router as policy_chat_router

app = FastAPI(title="InsureFlow AI")
app.include_router(policy_chat_router, prefix="/api")
