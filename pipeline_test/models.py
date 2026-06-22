# models.py
from pydantic import BaseModel

from typing import Optional

class RFIDData(BaseModel):
    rfid_code: str 
    gate: Optional[str] = None  # "in", "out", hoặc None (cho chế độ tương thích ngược tự đoán)