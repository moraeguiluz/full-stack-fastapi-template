# backend/app/main.py
from fastapi import FastAPI

from app.snippet_loader import load_snippets


app = FastAPI(
    title="API Bonube",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

@app.get("/", include_in_schema=False)
def root():
    return {"ok": True}

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

load_snippets(app)
