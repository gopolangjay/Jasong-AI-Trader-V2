from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///./jasong_trader.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    symbol = Column(String, index=True)
    direction = Column(String)
    confidence = Column(Float)
    entry_price = Column(Float)
    stake = Column(Float)
    mode = Column(String, default="paper")
    result = Column(String, nullable=True)
    pnl = Column(Float, nullable=True)
    closed = Column(Boolean, default=False)

def init_db():
    Base.metadata.create_all(bind=engine)
