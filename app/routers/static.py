from fastapi import APIRouter
from fastapi.responses import FileResponse, PlainTextResponse

from app.config import DATA_DIR

router = APIRouter()


# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------


@router.get("/")
async def serve_spa():
    html = DATA_DIR / "templates" / "index.html"
    if html.exists():
        return FileResponse(html)
    return PlainTextResponse("Frontend not found.", status_code=404)
