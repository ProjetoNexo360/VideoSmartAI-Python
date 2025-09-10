from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from uuid import uuid4
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    evo_instance = Column(String, nullable=True)
