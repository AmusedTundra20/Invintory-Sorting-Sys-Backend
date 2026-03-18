from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

# -----------------------------
# Database setup
# -----------------------------
DATABASE_URL = "sqlite:///./inventory.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# -----------------------------
# Database models
# -----------------------------
class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    barcode = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    quantity = Column(Integer, default=0)
    default_bin = Column(String, nullable=False)

    scans = relationship("ScanLog", back_populates="item")


class ScanLog(Base):
    __tablename__ = "scan_logs"

    id = Column(Integer, primary_key=True, index=True)
    barcode = Column(String, nullable=False)
    action = Column(String, nullable=False)
    quantity = Column(Integer, default=1)
    source = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    item_id = Column(Integer, ForeignKey("items.id"), nullable=True)
    sorted_to = Column(String, nullable=True)

    item = relationship("Item", back_populates="scans")


Base.metadata.create_all(bind=engine)


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="Inventory Sorting System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Dependency
# -----------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -----------------------------
# Pydantic schemas
# -----------------------------
class ItemCreate(BaseModel):
    barcode: str = Field(..., min_length=3)
    name: str
    category: str
    quantity: int = 0
    default_bin: str


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    quantity: Optional[int] = None
    default_bin: Optional[str] = None


class ItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    barcode: str
    name: str
    category: str
    quantity: int
    default_bin: str


class ScanRequest(BaseModel):
    barcode: str
    action: str = Field(..., pattern="^(IN|OUT|SORT)$")
    quantity: int = 1
    source: Optional[str] = "web-scanner"
    location_hint: Optional[str] = None


class ScanResponse(BaseModel):
    success: bool
    barcode: str
    item_name: Optional[str] = None
    category: Optional[str] = None
    new_quantity: Optional[int] = None
    assigned_bin: Optional[str] = None
    message: str


class SortDecisionResponse(BaseModel):
    barcode: str
    item_name: str
    category: str
    assigned_bin: str
    reason: str


class ScanLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    barcode: str
    action: str
    quantity: int
    source: Optional[str]
    timestamp: datetime
    sorted_to: Optional[str]


# -----------------------------
# Sorting logic
# -----------------------------
def choose_sort_bin(item: Item, location_hint: Optional[str] = None) -> tuple[str, str]:
    category = item.category.strip().lower()
    location = location_hint.strip().upper() if location_hint else None

    category_map = {
        "electronics": "E1",
        "clothing": "C1",
        "books": "B1",
        "fragile": "F1",
        "food": "FD1",
        "tools": "T1",
    }

    # If both category and location are given, combine them
    if location and category in category_map:
        return f"{location}-{category_map[category]}", f"Sorted by location {location} and category {category}"

    # If only category is known
    if category in category_map:
        return f"BIN-{category_map[category]}", f"Sorted by category {category}"

    # If only location is known
    if location:
        return f"ZONE-{location}", f"Sorted by location {location}"

    # Fallback
    return item.default_bin, "Used item's default bin"


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def root():
    return {"message": "Inventory Sorting System API is running"}


@app.post("/items", response_model=ItemResponse)
def create_item(item: ItemCreate, db: Session = Depends(get_db)):
    existing = db.query(Item).filter(Item.barcode == item.barcode).first()
    if existing:
        raise HTTPException(status_code=400, detail="Barcode already exists")

    db_item = Item(
        barcode=item.barcode,
        name=item.name,
        category=item.category,
        quantity=item.quantity,
        default_bin=item.default_bin,
    )
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item


@app.get("/items", response_model=List[ItemResponse])
def list_items(db: Session = Depends(get_db)):
    return db.query(Item).order_by(Item.name.asc()).all()


@app.get("/items/{barcode}", response_model=ItemResponse)
def get_item(barcode: str, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.barcode == barcode).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.put("/items/{barcode}", response_model=ItemResponse)
def update_item(barcode: str, updates: ItemUpdate, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.barcode == barcode).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    if updates.name is not None:
        item.name = updates.name
    if updates.category is not None:
        item.category = updates.category
    if updates.quantity is not None:
        item.quantity = updates.quantity
    if updates.default_bin is not None:
        item.default_bin = updates.default_bin

    db.commit()
    db.refresh(item)
    return item


@app.delete("/items/{barcode}")
def delete_item(barcode: str, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.barcode == barcode).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    db.delete(item)
    db.commit()
    return {"success": True, "message": f"Deleted item {barcode}"}


@app.get("/sort/{barcode}", response_model=SortDecisionResponse)
def sort_decision(barcode: str, location_hint: Optional[str] = None, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.barcode == barcode).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    assigned_bin, reason = choose_sort_bin(item, location_hint)

    return SortDecisionResponse(
        barcode=item.barcode,
        item_name=item.name,
        category=item.category,
        assigned_bin=assigned_bin,
        reason=reason,
    )


@app.post("/scan", response_model=ScanResponse)
def process_scan(scan: ScanRequest, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.barcode == scan.barcode).first()
    if not item:
        raise HTTPException(status_code=404, detail="Unknown barcode")

    assigned_bin = None

    if scan.action == "IN":
        item.quantity += scan.quantity
        message = f"Added {scan.quantity} unit(s) to inventory"

    elif scan.action == "OUT":
        if item.quantity < scan.quantity:
            raise HTTPException(status_code=400, detail="Not enough stock")
        item.quantity -= scan.quantity
        message = f"Removed {scan.quantity} unit(s) from inventory"

    elif scan.action == "SORT":
        assigned_bin, reason = choose_sort_bin(item, scan.location_hint)
        message = f"Send item to {assigned_bin} ({reason})"

    else:
        raise HTTPException(status_code=400, detail="Invalid action")

    log = ScanLog(
        barcode=scan.barcode,
        action=scan.action,
        quantity=scan.quantity,
        source=scan.source,
        item_id=item.id,
        sorted_to=assigned_bin,
    )
    db.add(log)
    db.commit()
    db.refresh(item)

    return ScanResponse(
        success=True,
        barcode=item.barcode,
        item_name=item.name,
        category=item.category,
        new_quantity=item.quantity,
        assigned_bin=assigned_bin,
        message=message,
    )


@app.get("/logs", response_model=List[ScanLogResponse])
def get_logs(db: Session = Depends(get_db)):
    return db.query(ScanLog).order_by(ScanLog.timestamp.desc()).limit(100).all()