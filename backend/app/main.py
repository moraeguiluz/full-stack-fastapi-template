from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from .snippets import hello

app = FastAPI(title="API Bonube", openapi_url="/api/v1/openapi.json", docs_url="/docs", redoc_url="/redoc")

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")

@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

# monta el snippet bajo /api/v1
app.include_router(hello.router, prefix="/api/v1")
