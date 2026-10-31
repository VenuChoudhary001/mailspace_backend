from pydantic import BaseModel, EmailStr


class EmailSchema(BaseModel):
    to: list[EmailStr]
    reply_to: list[EmailStr] | None = None
    subject: str
    html: str


class EmailResponse(BaseModel):
    message: str
    id: str | None = None
