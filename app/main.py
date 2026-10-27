import hmac
import re

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import FRONTEND_DIR, GENERATED_DIR, settings
from app.services.dataset_store import atomic_write_json, load_json
from app.services.excel_processor import WorkbookValidationError, parse_contracts, parse_credentials

app = FastAPI(title=settings.app_name, version="0.1.0")
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
STUDENT_ID_RE = re.compile(r"^[A-Z]{2}\\d{7}$")

def require_admin(x_admin_token: str | None) -> None:
    configured = settings.admin_upload_token
    if not configured or configured == "CHANGE_ME_BEFORE_RUNNING":
        raise HTTPException(status_code=503, detail="Set ADMIN_UPLOAD_TOKEN in .env before using admin upload endpoints.")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, configured):
        raise HTTPException(status_code=403, detail="Not authorized to upload datasets.")

async def read_upload(file: UploadFile) -> bytes:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx file.")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb} MB limit.")
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    return content

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "datasets": {
            "hemis_credentials": (GENERATED_DIR / "login.json").exists(),
            "contracts": (GENERATED_DIR / "contract.json").exists(),
        },
    }

@app.post("/api/admin/upload/hemis")
async def upload_hemis(file: UploadFile = File(...), x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    content = await read_upload(file)
    try:
        dataset = parse_credentials(content)
    except WorkbookValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    atomic_write_json(GENERATED_DIR / "login.json", dataset)
    return {"ok": True, "message": "HEMIS credentials dataset updated.", "records": len(dataset)}

@app.post("/api/admin/upload/contracts")
async def upload_contracts(file: UploadFile = File(...), x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    content = await read_upload(file)
    try:
        dataset = parse_contracts(content)
    except WorkbookValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    atomic_write_json(GENERATED_DIR / "contract.json", dataset)
    return {"ok": True, "message": "Contract dataset updated.", "records": len(dataset)}

@app.get("/api/hemis/credentials/{student_id}")
def get_credentials(student_id: str):
    student_id = student_id.upper()
    if not STUDENT_ID_RE.fullmatch(student_id):
        raise HTTPException(status_code=422, detail="Student ID must contain two English letters followed by seven digits.")
    dataset = load_json(GENERATED_DIR / "login.json")
    if dataset is None:
        raise HTTPException(status_code=503, detail="HEMIS credentials have not been uploaded yet.")
    record = dataset.get(student_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No HEMIS credentials were found for this student ID.")
    return {"student_id": student_id, **record}

@app.get("/api/contracts/{student_id}")
def get_contract(student_id: str):
    student_id = student_id.upper()
    if not STUDENT_ID_RE.fullmatch(student_id):
        raise HTTPException(status_code=422, detail="Student ID must contain two English letters followed by seven digits.")
    dataset = load_json(GENERATED_DIR / "contract.json")
    if dataset is None:
        raise HTTPException(status_code=503, detail="Contract payment data has not been uploaded yet.")
    record = dataset.get(student_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No contract payment record was found for this student ID.")
    return {"student_id": student_id, **record}

@app.get("/", include_in_schema=False)
def frontend():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return {"message": "Ranchbot API is running. Open /docs to inspect the API."}
    return FileResponse(index_file)
