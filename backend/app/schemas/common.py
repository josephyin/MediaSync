from pydantic import BaseModel


class Page[T](BaseModel):
    items: list[T]
    page: int
    page_size: int
    total: int


class MessageResponse(BaseModel):
    message: str
