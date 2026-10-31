from fastapi import APIRouter, Request, Response  

router = APIRouter()

@router.post("/forward-email")
async def forward_email(request: Request, response: Response):
    """
    Endpoint to forward an email.
    """
    try:
        payload = await request.json()
        print(f"Received payload: {payload}")  # Debugging line
        return {"message": "Email forwarded successfully", "payload": payload}
    except Exception as e:
        response.status_code = 500
        return {"error": str(e)}