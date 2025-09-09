# models.py
from sqlalchemy import Column, Integer, String, DateTime, func
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)

    # Evolution API por usuário
    evo_instance = Column(String(255), nullable=True)  # ex.: "minha-instancia"

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
