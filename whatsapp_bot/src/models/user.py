from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Session
from typing import Optional, List

from src.models import Base_sqlite

def digits_only(s: str) -> str:
    return ''.join(c for c in str(s) if c.isdigit())

def phones_match_fuzzy(a: str, b: str) -> bool:
    """
    Compara a y b por dígitos solamente y considera:
    - a contiene a b
    - b contiene a a
    - a termina con b (típico: '3467...867' vs '678...867')
    """
    da, db = digits_only(a), digits_only(b)
    if not da or not db:
        return False
    return (da in db) or (db in da) or da.endswith(db) or db.endswith(da)

class User(Base_sqlite):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    phone = Column(String, nullable=False, unique=True)
    email = Column(String, nullable=False, unique=True)
    name = Column(String)
    role = Column(String, nullable=False)  # 'user' o 'admin'

    @staticmethod
    def get_by_phone(session: Session, phone: str) -> Optional["User"]:
        return session.query(User).filter(User.phone == phone).first()

    @staticmethod
    def get_by_phone_fuzzy(session: Session, phone: str) -> Optional["User"]:
        """Busca por coincidencia de dígitos, permitiendo prefijos/sufijos."""
        target = digits_only(phone)
        # Pequeña optimización: si el exacto está, te lo ahorras
        u = session.query(User).filter(User.phone == phone).first()
        if u:
            return u

        # Fallback: escanear (normalmente pocos usuarios → OK)
        for u in session.query(User).all():
            if phones_match_fuzzy(u.phone, target):
                return u
        return None

    @staticmethod
    def user_exists(session: Session, phone: str) -> bool:
        return session.query(User.phone).filter(User.phone == phone).first() is not None

    @staticmethod
    def get_admins(session: Session) -> List["User"]:
        return session.query(User).filter(User.role == "admin").all()
