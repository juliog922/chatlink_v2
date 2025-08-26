# src/ai/schemas.py
from typing import List, Optional
from typing_extensions import Annotated
from pydantic import BaseModel, Field, StringConstraints
from pydantic import field_validator

ProductCode = Annotated[str, StringConstraints(min_length=2, max_length=32)]

class MentionedItem(BaseModel):
    code: ProductCode
    qty: int = Field(..., ge=1)

    @field_validator("code")
    @classmethod
    def only_alnum_dash(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[A-Za-z0-9-]{2,32}", v):
            raise ValueError("code must match [A-Za-z0-9-]{2,32}")
        return v

class MentionedItems(BaseModel):
    items: List[MentionedItem] = Field(default_factory=list)

class OrderYesNo(BaseModel):
    order: bool

class ChatDecision(BaseModel):
    responder: bool
    respuesta: Optional[str] = None
