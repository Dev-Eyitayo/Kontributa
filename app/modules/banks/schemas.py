from pydantic import BaseModel


class BankOut(BaseModel):
    bank_code: str
    bank_name: str
