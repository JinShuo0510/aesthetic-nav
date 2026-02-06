"""
JinResearch Dashboard - FastAPI Backend
A navigation portal with persistent storage using SQLite.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
import sqlite3
import httpx
import asyncio
from pathlib import Path
from typing import Optional
import os
import hashlib
import secrets
import json

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt

# --- Configuration ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)  # Ensure data directory exists
DB_PATH = DATA_DIR / "dashboard.db"
TEMPLATES_DIR = BASE_DIR / "templates"

# Auth Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key-in-production-to-a-random-string")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24
PASSWORD_SALT = "dashboard-salt-2024"  # Should be random in production

# Security
security = HTTPBearer()

# --- FastAPI App ---
app = FastAPI(title="JinResearch Dashboard")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# --- Pydantic Models ---
class LinkCreate(BaseModel):
    title: str
    url: str
    icon: Optional[str] = None  # SVG string or icon class
    icon_url: Optional[str] = None  # Custom icon URL
    description: Optional[str] = None
    category: str = "Uncategorized"
    is_favorite: bool = False


class LinkUpdate(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    icon: Optional[str] = None
    icon_url: Optional[str] = None  # Custom icon URL
    description: Optional[str] = None
    category: Optional[str] = None
    is_favorite: Optional[bool] = None


class LinkResponse(BaseModel):
    id: int
    title: str
    url: str
    icon: Optional[str]
    icon_url: Optional[str]  # Custom icon URL
    description: Optional[str]
    category: str
    is_favorite: bool
    sort_index: int
    usage_count: int
    created_at: str


# --- Authentication Models ---
class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class Settings(BaseModel):
    site_title: str
    site_logo: str
    hidden_categories: list[str]
    category_order: list[str] = []


class CategoryOrderUpdate(BaseModel):
    order: list[str]


class LinkReorderItem(BaseModel):
    id: int
    category: str
    sort_index: int


class LinkReorderRequest(BaseModel):
    items: list[LinkReorderItem]


# --- Database Setup ---
@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initialize the database with required tables."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                icon TEXT,
                category TEXT DEFAULT 'Uncategorized',
                is_favorite INTEGER DEFAULT 0,
                usage_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        
        # Migration: Add description column if not exists
        cursor = conn.execute("PRAGMA table_info(links)")
        columns = [info[1] for info in cursor.fetchall()]
        if "description" not in columns:
            conn.execute("ALTER TABLE links ADD COLUMN description TEXT")
            conn.commit()
        
        # Migration: Add icon_url column if not exists
        if "icon_url" not in columns:
            conn.execute("ALTER TABLE links ADD COLUMN icon_url TEXT")
            conn.commit()

        # Migration: Add sort_index column if not exists
        if "sort_index" not in columns:
            conn.execute("ALTER TABLE links ADD COLUMN sort_index INTEGER DEFAULT 0")
            conn.commit()

            # Backfill sort_index per category by created_at then id
            cursor = conn.execute("SELECT DISTINCT category FROM links")
            categories = [row[0] for row in cursor.fetchall()]
            for category in categories:
                cursor = conn.execute(
                    "SELECT id FROM links WHERE category = ? ORDER BY created_at ASC, id ASC",
                    (category,)
                )
                ids = [row[0] for row in cursor.fetchall()]
                for idx, link_id in enumerate(ids, start=1):
                    conn.execute(
                        "UPDATE links SET sort_index = ? WHERE id = ?",
                        (idx, link_id)
                    )
            conn.commit()
        
        # Create admin table for authentication
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        # Create settings table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        
        # Seed default settings
        default_settings = {
            "site_title": "Aesthetic Nav",
            "site_logo": "https://www.gstatic.com/images/branding/product/1x/keep_2020q4_48dp.png",  # Default logo
            "hidden_categories": "[]",  # JSON list of hidden categories
            "category_order": "[]"  # JSON list of category order
        }
        
        for key, value in default_settings.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        
        # Initialize default admin user if doesn't exist
        cursor = conn.execute("SELECT COUNT(*) FROM admin WHERE username = ?", ("admin",))
        if cursor.fetchone()[0] == 0:
            default_password_hash = hash_password("admin123")
            conn.execute(
                "INSERT INTO admin (username, password_hash) VALUES (?, ?)",
               ("admin", default_password_hash)
            )
            conn.commit()
            print("Default admin user created (username: admin, password: admin123)")
        
        # Seed default data if table is empty
        cursor = conn.execute("SELECT COUNT(*) FROM links")
        if cursor.fetchone()[0] == 0:
            seed_default_links(conn)


def seed_default_links(conn: sqlite3.Connection):
    """Populate database with initial links from the original design."""
    default_links = [
        # Social Media
        ("Instagram", "https://instagram.com", "instagram", "Social Media", 0, "Visual inspiration and photo sharing platform.", 1),
        ("Twitter (X)", "https://twitter.com", "twitter", "Social Media", 0, "Real-time news and public conversation.", 2),
        ("LinkedIn", "https://linkedin.com", "linkedin", "Social Media", 0, "Professional networking and career development.", 3),
        # Design Tools
        ("Figma", "https://figma.com", "figma", "Design Tools", 0, "Collaborative interface design tool.", 1),
        ("Dribbble", "https://dribbble.com", "dribbble", "Design Tools", 0, "World's leading destination for design inspiration.", 2),
        ("Behance", "https://behance.net", "behance", "Design Tools", 0, "Showcase and discover creative work.", 3),
        # News & Media
        ("The Verge", "https://theverge.com", "theverge", "News & Media", 0, "Tech news, reviews, and futuristic features.", 1),
        ("Medium", "https://medium.com", "medium", "News & Media", 0, "A place to read, write, and deepen understanding.", 2),
        ("YouTube", "https://youtube.com", "youtube", "News & Media", 0, "World's most popular video hosting service.", 3),
    ]
    conn.executemany(
        "INSERT INTO links (title, url, icon, category, is_favorite, description, sort_index) VALUES (?, ?, ?, ?, ?, ?, ?)",
        default_links
    )
    conn.commit()


# --- Password Hashing Helpers ---
def hash_password(password: str) -> str:
    """Hash a password using SHA256 with salt."""
    salted = (password + PASSWORD_SALT).encode('utf-8')
    return hashlib.sha256(salted).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    return hash_password(password) == password_hash


# --- Lifecycle Events ---
@app.on_event("startup")
def on_startup():
    init_db()



# --- Health Check Endpoint ---
@app.get("/api/check_status")
async def check_status(url: str):
    if not url:
        return {"status": "error", "message": "URL is required"}
    
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            start_time = asyncio.get_event_loop().time()
            try:
                response = await client.head(url)
            except httpx.RequestError:
                 # If HEAD fails, try GET immediately
                 response = await client.get(url)

            # If HEAD returned error (e.g. 405), try GET
            if response.status_code >= 400:
                response = await client.get(url)
            
            end_time = asyncio.get_event_loop().time()
            latency = int((end_time - start_time) * 1000)
            
            if response.status_code < 400:
                return {"status": "online", "latency": latency}
            else:
                return {"status": "offline", "code": response.status_code}
    except Exception as e:
        return {"status": "offline", "error": str(e)}


# --- Authentication Helpers ---
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify JWT token and return username if valid."""
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username != "admin":
            raise HTTPException(status_code=403, detail="Invalid token")
        return username
    except JWTError:
        raise HTTPException(status_code=403, detail="Invalid or expired token")


def get_current_user_optional(authorization: Optional[str] = Header(None)):
    """Check for optional authentication token."""
    if not authorization:
        return None
    
    try:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return None
            
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        return username if username == "admin" else None
    except JWTError:
        return None


def create_access_token(data: dict):
    """Create a new JWT access token."""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# --- Authentication Endpoints ---
@app.post("/api/auth/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    """Authenticate admin user and return JWT token."""
    with get_db() as conn:
        cursor = conn.execute("SELECT password_hash FROM admin WHERE username = ?", ("admin",))
        row = cursor.fetchone()
        
        if not row or not verify_password(request.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid password")
        
        # Generate JWT token
        access_token = create_access_token(data={"sub": "admin"})
        return TokenResponse(access_token=access_token)


@app.post("/api/auth/change-password")
async def change_password(request: ChangePasswordRequest, username: str = Depends(verify_token)):
    """Change admin password (requires authentication)."""
    with get_db() as conn:
        cursor = conn.execute("SELECT password_hash FROM admin WHERE username = ?", ("admin",))
        row = cursor.fetchone()
        
        if not verify_password(request.old_password, row["password_hash"]):
            raise HTTPException(status_code=400, detail="Incorrect old password")
        
        new_hash = hash_password(request.new_password)
        conn.execute("UPDATE admin SET password_hash = ? WHERE username = ?", (new_hash, "admin"))
        conn.commit()
        
        return {"message": "Password changed successfully"}


# --- Settings Endpoints ---
@app.get("/api/settings", response_model=Settings)
async def get_settings():
    """Get public settings."""
    with get_db() as conn:
        cursor = conn.execute("SELECT key, value FROM settings")
        rows = cursor.fetchall()
        settings_dict = {row["key"]: row["value"] for row in rows}
        
        return Settings(
            site_title=settings_dict.get("site_title", "JinResearch"),
            site_logo=settings_dict.get("site_logo", ""),
            hidden_categories=json.loads(settings_dict.get("hidden_categories", "[]")),
            category_order=json.loads(settings_dict.get("category_order", "[]"))
        )


@app.put("/api/settings", response_model=Settings)
async def update_settings(settings: Settings, username: str = Depends(verify_token)):
    """Update settings (requires authentication)."""
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("site_title", settings.site_title))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("site_logo", settings.site_logo))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("hidden_categories", json.dumps(settings.hidden_categories)))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("category_order", json.dumps(settings.category_order)))
        conn.commit()
        return settings


# --- API Routes ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the main dashboard page."""
    return templates.TemplateResponse("index.html", {"request": request})





@app.get("/api/links", response_model=list[LinkResponse])
async def get_links(
    category: Optional[str] = None, 
    favorite: Optional[bool] = None,
    current_user: Optional[str] = Depends(get_current_user_optional)
):
    """Get all links, optionally filtered by category or favorite status."""
    with get_db() as conn:
        # Get hidden categories
        cursor = conn.execute("SELECT value FROM settings WHERE key = 'hidden_categories'")
        row = cursor.fetchone()
        hidden_categories = json.loads(row["value"]) if row else []
        
        query = "SELECT * FROM links WHERE 1=1"
        params = []
        
        # Filter hidden categories if not admin
        if not current_user and hidden_categories:
            placeholders = ','.join('?' * len(hidden_categories))
            query += f" AND category NOT IN ({placeholders})"
            params.extend(hidden_categories)
        
        if category:
            query += " AND category = ?"
            params.append(category)
        if favorite is not None:
            query += " AND is_favorite = ?"
            params.append(1 if favorite else 0)
            
        query += " ORDER BY category ASC, sort_index ASC, created_at DESC"
        
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        
        return [
            LinkResponse(
                id=row["id"],
                title=row["title"],
                url=row["url"],
                icon=row["icon"],
                icon_url=row["icon_url"],
                category=row["category"],
                is_favorite=bool(row["is_favorite"]),
                description=row["description"],
                usage_count=row["usage_count"],
                sort_index=row["sort_index"],
                created_at=row["created_at"]
            )
            for row in rows
        ]


@app.get("/api/categories")
async def get_categories():
    """Get all unique categories."""
    with get_db() as conn:
        cursor = conn.execute("SELECT DISTINCT category FROM links")
        categories = [row["category"] for row in cursor.fetchall()]

        cursor = conn.execute("SELECT value FROM settings WHERE key = 'category_order'")
        row = cursor.fetchone()
        category_order = json.loads(row["value"]) if row else []

        ordered = [cat for cat in category_order if cat in categories]
        remaining = sorted([cat for cat in categories if cat not in ordered])
        return ordered + remaining


@app.put("/api/categories/order")
async def update_category_order(payload: CategoryOrderUpdate, username: str = Depends(verify_token)):
    """Update category order (requires authentication)."""
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("category_order", json.dumps(payload.order)))
        conn.commit()
        return {"order": payload.order}


@app.post("/api/links", response_model=LinkResponse)
async def create_link(link: LinkCreate, username: str = Depends(verify_token)):
    """Create a new link (requires authentication)."""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT COALESCE(MAX(sort_index), 0) FROM links WHERE category = ?",
            (link.category,)
        )
        next_sort = int(cursor.fetchone()[0]) + 1
        cursor = conn.execute(
            """INSERT INTO links (title, url, icon, icon_url, category, is_favorite, description, sort_index)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (link.title, link.url, link.icon, link.icon_url, link.category, 1 if link.is_favorite else 0, link.description, next_sort)
        )
        conn.commit()
        link_id = cursor.lastrowid
        
        # Fetch the created link
        cursor = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,))
        row = cursor.fetchone()
        
        return LinkResponse(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            icon=row["icon"],
            icon_url=row["icon_url"],
            category=row["category"],
            is_favorite=bool(row["is_favorite"]),
            description=row["description"],
            usage_count=row["usage_count"],
            sort_index=row["sort_index"],
            created_at=row["created_at"]
        )


@app.put("/api/links/reorder")
async def reorder_links(payload: LinkReorderRequest, username: str = Depends(verify_token)):
    """Reorder and/or move links across categories (requires authentication)."""
    if not payload.items:
        raise HTTPException(status_code=400, detail="No items to reorder")

    with get_db() as conn:
        for item in payload.items:
            conn.execute(
                "UPDATE links SET category = ?, sort_index = ? WHERE id = ?",
                (item.category, item.sort_index, item.id)
            )
        conn.commit()
        return {"status": "ok"}


@app.put("/api/links/{link_id}", response_model=LinkResponse)
async def update_link(link_id: int, link: LinkUpdate, username: str = Depends(verify_token)):
    """Update an existing link (requires authentication)."""
    with get_db() as conn:
        cursor = conn.execute("SELECT category FROM links WHERE id = ?", (link_id,))
        existing = cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Link not found")

        # Build dynamic update query
        updates = []
        params = []
        
        if link.title is not None:
            updates.append("title = ?")
            params.append(link.title)
        if link.url is not None:
            updates.append("url = ?")
            params.append(link.url)
        if link.icon is not None:
            updates.append("icon = ?")
            params.append(link.icon)
        if link.icon_url is not None:
            updates.append("icon_url = ?")
            params.append(link.icon_url)
        if link.description is not None:
            updates.append("description = ?")
            params.append(link.description)
        if link.category is not None:
            updates.append("category = ?")
            params.append(link.category)

            if link.category != existing["category"]:
                cursor = conn.execute(
                    "SELECT COALESCE(MAX(sort_index), 0) FROM links WHERE category = ?",
                    (link.category,)
                )
                next_sort = int(cursor.fetchone()[0]) + 1
                updates.append("sort_index = ?")
                params.append(next_sort)
        if link.is_favorite is not None:
            updates.append("is_favorite = ?")
            params.append(1 if link.is_favorite else 0)
        
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        params.append(link_id)
        query = f"UPDATE links SET {', '.join(updates)} WHERE id = ?"
        conn.execute(query, params)
        conn.commit()
        
        # Fetch updated link
        cursor = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,))
        row = cursor.fetchone()

        return LinkResponse(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            icon=row["icon"],
            icon_url=row["icon_url"],
            category=row["category"],
            is_favorite=bool(row["is_favorite"]),
            description=row["description"],
            usage_count=row["usage_count"],
            sort_index=row["sort_index"],
            created_at=row["created_at"]
        )


@app.post("/api/links/{link_id}/click")
async def track_click(link_id: int):
    """Track a link click (increment usage count)."""
    with get_db() as conn:
        conn.execute("UPDATE links SET usage_count = usage_count + 1 WHERE id = ?", (link_id,))
        conn.commit()
        return {"status": "ok"}


@app.delete("/api/links/{link_id}")
async def delete_link(link_id: int, username: str = Depends(verify_token)):
    """Delete a link (requires authentication)."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
        conn.commit()
        
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Link not found")
        
        return {"status": "deleted"}





# --- Run with: uvicorn main:app --reload ---
