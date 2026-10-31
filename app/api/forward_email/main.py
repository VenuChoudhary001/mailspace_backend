from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException    


router = APIRouter()

@router.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@router.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": exc.body},
    )

@router.post("/forward-email")
async def forward_email(request: Request, response: Response):
    """
    Endpoint to forward an email.
    """
    try:
        payload = await request.json()
        return {"message": "Email forwarded successfully", "payload": payload}
    except Exception as e:
        response.status_code = 500
        return {"error": str(e)}