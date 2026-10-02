"""
Tiny SQLite helper for GiftScout.

No ORM on purpose — the schema is small and plain sqlite3 keeps the
dependency list (and the mental model) small. Query helpers live here so
route handlers in main.py stay focused on HTTP concerns.
"""
import re
import sqlite3
from pathlib import Path

DATA_DIR = Path("/data")
DB_PATH = DATA_DIR / "giftscout.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    display_order   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    slug            TEXT,
    description     TEXT NOT NULL DEFAULT '',
    why_recommend   TEXT NOT NULL DEFAULT '',
    image_path      TEXT,
    price           TEXT,
    category        TEXT,
    category_id     INTEGER,
    affiliate_url   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    display_order   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS product_images (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    image_path      TEXT NOT NULL,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    path            TEXT,
    product_id      INTEGER,
    category_id     INTEGER,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON events (event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_events_product ON events (product_id);
"""

PRODUCT_COLUMNS = """
    products.id AS id, products.name AS name, products.slug AS slug,
    products.description AS description,
    products.why_recommend AS why_recommend, products.image_path AS image_path,
    products.price AS price, products.category_id AS category_id,
    products.affiliate_url AS affiliate_url, products.status AS status,
    products.display_order AS display_order, products.created_at AS created_at,
    categories.name AS category_name, categories.slug AS category_slug
"""


def get_connection() -> sqlite3.Connection:
    """Open a new connection with row access by column name."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def slugify(text: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or fallback


def _unique_slug(conn: sqlite3.Connection, table: str, base_slug: str, ignore_id: int = None) -> str:
    slug = base_slug
    n = 2
    exclude_id = ignore_id if ignore_id is not None else -1
    while True:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE slug = ? AND id != ?",
            (slug, exclude_id),
        ).fetchone()
        if row is None:
            return slug
        slug = f"{base_slug}-{n}"
        n += 1


def init_db() -> None:
    """
    Create tables if they don't exist, add new columns to older databases,
    and one-time-migrate legacy data (single image_path, free-text category)
    into their proper tables. Safe to call on every startup.
    """
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)

        # --- upgrade path: columns added after the first release ---
        cols = _column_names(conn, "products")
        if "status" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "display_order" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0")
        if "category_id" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN category_id INTEGER")
        if "slug" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN slug TEXT")

        # --- backfill slugs for products that don't have one yet ---
        unslugged = conn.execute(
            "SELECT id, name FROM products WHERE slug IS NULL OR TRIM(slug) = ''"
        ).fetchall()
        for row in unslugged:
            slug = _unique_slug(conn, "products", slugify(row["name"]))
            conn.execute("UPDATE products SET slug = ? WHERE id = ?", (slug, row["id"]))

        # --- migrate legacy single image_path into product_images ---
        legacy_images = conn.execute(
            """
            SELECT p.id, p.image_path FROM products p
            WHERE p.image_path IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM product_images pi WHERE pi.product_id = p.id)
            """
        ).fetchall()
        for row in legacy_images:
            conn.execute(
                "INSERT INTO product_images (product_id, image_path, sort_order) VALUES (?, ?, 0)",
                (row["id"], row["image_path"]),
            )

        # --- migrate legacy free-text category into the categories table ---
        legacy_categories = conn.execute(
            """
            SELECT DISTINCT category FROM products
            WHERE category IS NOT NULL AND TRIM(category) != '' AND category_id IS NULL
            """
        ).fetchall()
        for row in legacy_categories:
            name = row["category"].strip()
            order = next_category_order(conn)
            slug = _unique_slug(conn, "categories", slugify(name))
            cur = conn.execute(
                "INSERT INTO categories (name, slug, display_order) VALUES (?, ?, ?)",
                (name, slug, order),
            )
            conn.execute(
                "UPDATE products SET category_id = ? WHERE category = ? AND category_id IS NULL",
                (cur.lastrowid, row["category"]),
            )

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Settings (key/value — currently just the admin username/password hash)
# ---------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def get_categories(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM categories ORDER BY display_order ASC, name ASC").fetchall()


def get_categories_with_active_products(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT DISTINCT categories.* FROM categories
        JOIN products ON products.category_id = categories.id
        WHERE products.status = 'active'
        ORDER BY categories.display_order ASC, categories.name ASC
        """
    ).fetchall()


def get_category(conn: sqlite3.Connection, category_id: int):
    return conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()


def get_category_by_slug(conn: sqlite3.Connection, slug: str):
    return conn.execute("SELECT * FROM categories WHERE slug = ?", (slug,)).fetchone()


def next_category_order(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(display_order), 0) + 1 AS n FROM categories").fetchone()
    return row["n"]


def create_category(conn: sqlite3.Connection, name: str) -> int:
    slug = _unique_slug(conn, "categories", slugify(name))
    order = next_category_order(conn)
    cur = conn.execute(
        "INSERT INTO categories (name, slug, display_order) VALUES (?, ?, ?)", (name, slug, order)
    )
    return cur.lastrowid


def update_category(conn: sqlite3.Connection, category_id: int, name: str, slug: str) -> None:
    slug = _unique_slug(conn, "categories", slugify(slug) if slug.strip() else slugify(name), ignore_id=category_id)
    conn.execute("UPDATE categories SET name = ?, slug = ? WHERE id = ?", (name, slug, category_id))


def delete_category(conn: sqlite3.Connection, category_id: int) -> None:
    """Un-assigns any products in this category (they become uncategorized), then deletes it."""
    conn.execute("UPDATE products SET category_id = NULL WHERE category_id = ?", (category_id,))
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))


def move_category(conn: sqlite3.Connection, category_id: int, direction: str) -> None:
    category = get_category(conn, category_id)
    if category is None:
        return
    if direction == "up":
        neighbor = conn.execute(
            """
            SELECT * FROM categories WHERE
            (display_order < ? OR (display_order = ? AND id < ?))
            ORDER BY display_order DESC, id DESC LIMIT 1
            """,
            (category["display_order"], category["display_order"], category["id"]),
        ).fetchone()
    else:
        neighbor = conn.execute(
            """
            SELECT * FROM categories WHERE
            (display_order > ? OR (display_order = ? AND id > ?))
            ORDER BY display_order ASC, id ASC LIMIT 1
            """,
            (category["display_order"], category["display_order"], category["id"]),
        ).fetchone()
    if neighbor is None:
        return
    conn.execute("UPDATE categories SET display_order = ? WHERE id = ?", (neighbor["display_order"], category["id"]))
    conn.execute("UPDATE categories SET display_order = ? WHERE id = ?", (category["display_order"], neighbor["id"]))


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

def get_active_products(conn: sqlite3.Connection):
    return conn.execute(
        f"""
        SELECT {PRODUCT_COLUMNS} FROM products
        LEFT JOIN categories ON categories.id = products.category_id
        WHERE products.status = 'active'
        ORDER BY products.display_order ASC, products.created_at DESC
        """
    ).fetchall()


def get_active_products_by_category(conn: sqlite3.Connection, category_id: int):
    return conn.execute(
        f"""
        SELECT {PRODUCT_COLUMNS} FROM products
        LEFT JOIN categories ON categories.id = products.category_id
        WHERE products.status = 'active' AND products.category_id = ?
        ORDER BY products.display_order ASC, products.created_at DESC
        """,
        (category_id,),
    ).fetchall()


def get_all_products(conn: sqlite3.Connection):
    """For the admin dashboard: active first (in display order), then archived."""
    return conn.execute(
        f"""
        SELECT {PRODUCT_COLUMNS} FROM products
        LEFT JOIN categories ON categories.id = products.category_id
        ORDER BY (products.status = 'archived') ASC, products.display_order ASC, products.created_at DESC
        """
    ).fetchall()


def get_product(conn: sqlite3.Connection, product_id: int):
    return conn.execute(
        f"""
        SELECT {PRODUCT_COLUMNS} FROM products
        LEFT JOIN categories ON categories.id = products.category_id
        WHERE products.id = ?
        """,
        (product_id,),
    ).fetchone()


def get_product_by_slug(conn: sqlite3.Connection, slug: str):
    return conn.execute(
        f"""
        SELECT {PRODUCT_COLUMNS} FROM products
        LEFT JOIN categories ON categories.id = products.category_id
        WHERE products.slug = ? AND products.status = 'active'
        """,
        (slug,),
    ).fetchone()


def assign_product_slug(conn: sqlite3.Connection, product_id: int, name: str, requested_slug: str = "") -> str:
    """Generates (or normalizes an admin-provided) unique slug for a product."""
    base = requested_slug.strip() if requested_slug.strip() else name
    slug = _unique_slug(conn, "products", slugify(base), ignore_id=product_id)
    conn.execute("UPDATE products SET slug = ? WHERE id = ?", (slug, product_id))
    return slug


def next_display_order(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(display_order), 0) + 1 AS n FROM products").fetchone()
    return row["n"]


def move_product(conn: sqlite3.Connection, product_id: int, direction: str) -> None:
    """Swap display_order with the neighboring active product above/below."""
    product = get_product(conn, product_id)
    if product is None or product["status"] != "active":
        return
    if direction == "up":
        neighbor = conn.execute(
            """
            SELECT * FROM products WHERE status = 'active' AND
            (display_order < ? OR (display_order = ? AND id < ?))
            ORDER BY display_order DESC, id DESC LIMIT 1
            """,
            (product["display_order"], product["display_order"], product["id"]),
        ).fetchone()
    else:
        neighbor = conn.execute(
            """
            SELECT * FROM products WHERE status = 'active' AND
            (display_order > ? OR (display_order = ? AND id > ?))
            ORDER BY display_order ASC, id ASC LIMIT 1
            """,
            (product["display_order"], product["display_order"], product["id"]),
        ).fetchone()
    if neighbor is None:
        return
    conn.execute("UPDATE products SET display_order = ? WHERE id = ?", (neighbor["display_order"], product["id"]))
    conn.execute("UPDATE products SET display_order = ? WHERE id = ?", (product["display_order"], neighbor["id"]))


def archive_product(conn: sqlite3.Connection, product_id: int) -> None:
    conn.execute("UPDATE products SET status = 'archived' WHERE id = ?", (product_id,))


def restore_product(conn: sqlite3.Connection, product_id: int) -> None:
    order = next_display_order(conn)
    conn.execute("UPDATE products SET status = 'active', display_order = ? WHERE id = ?", (order, product_id))


def delete_product(conn: sqlite3.Connection, product_id: int) -> list:
    """Deletes the product and its image rows. Returns the image_paths that
    the caller should now remove from disk (files aren't tracked in SQLite)."""
    paths = [
        row["image_path"]
        for row in conn.execute("SELECT image_path FROM product_images WHERE product_id = ?", (product_id,))
    ]
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    return paths


def set_product_category(conn: sqlite3.Connection, product_id: int, category_id):
    conn.execute("UPDATE products SET category_id = ? WHERE id = ?", (category_id, product_id))


# ---------------------------------------------------------------------------
# Product images
# ---------------------------------------------------------------------------

def get_product_images(conn: sqlite3.Connection, product_id: int):
    return conn.execute(
        "SELECT * FROM product_images WHERE product_id = ? ORDER BY sort_order ASC, id ASC",
        (product_id,),
    ).fetchall()


def add_product_image(conn: sqlite3.Connection, product_id: int, image_path: str) -> None:
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM product_images WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO product_images (product_id, image_path, sort_order) VALUES (?, ?, ?)",
        (product_id, image_path, row["n"]),
    )


def delete_product_image(conn: sqlite3.Connection, image_id: int):
    """Deletes the row. Returns the image_path to remove from disk, or None."""
    row = conn.execute("SELECT image_path FROM product_images WHERE id = ?", (image_id,)).fetchone()
    conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,))
    return row["image_path"] if row else None


def get_product_image(conn: sqlite3.Connection, image_id: int):
    return conn.execute("SELECT * FROM product_images WHERE id = ?", (image_id,)).fetchone()


def move_product_image(conn: sqlite3.Connection, image_id: int, direction: str) -> None:
    """Swap sort_order with the neighboring photo (within the same product)."""
    image = get_product_image(conn, image_id)
    if image is None:
        return
    if direction == "up":
        neighbor = conn.execute(
            """
            SELECT * FROM product_images WHERE product_id = ? AND
            (sort_order < ? OR (sort_order = ? AND id < ?))
            ORDER BY sort_order DESC, id DESC LIMIT 1
            """,
            (image["product_id"], image["sort_order"], image["sort_order"], image["id"]),
        ).fetchone()
    else:
        neighbor = conn.execute(
            """
            SELECT * FROM product_images WHERE product_id = ? AND
            (sort_order > ? OR (sort_order = ? AND id > ?))
            ORDER BY sort_order ASC, id ASC LIMIT 1
            """,
            (image["product_id"], image["sort_order"], image["sort_order"], image["id"]),
        ).fetchone()
    if neighbor is None:
        return
    conn.execute("UPDATE product_images SET sort_order = ? WHERE id = ?", (neighbor["sort_order"], image["id"]))
    conn.execute("UPDATE product_images SET sort_order = ? WHERE id = ?", (image["sort_order"], neighbor["id"]))


# ---------------------------------------------------------------------------
# Events — a lightweight built-in analytics log (pageviews + affiliate clicks).
# No third-party service, no separate container: just rows in this database.
# ---------------------------------------------------------------------------

def log_event(
    conn: sqlite3.Connection,
    event_type: str,
    path: str = None,
    product_id: int = None,
    category_id: int = None,
) -> None:
    """Fire-and-forget event log. Commits immediately since callers (public
    page views, redirect clicks) don't otherwise need a transaction."""
    conn.execute(
        "INSERT INTO events (event_type, path, product_id, category_id) VALUES (?, ?, ?, ?)",
        (event_type, path, product_id, category_id),
    )
    conn.commit()


def count_events(conn: sqlite3.Connection, event_type: str, since_days: int = None) -> int:
    if since_days is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type = ?", (event_type,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE event_type = ? AND created_at >= datetime('now', ?)",
            (event_type, f"-{since_days} days"),
        ).fetchone()
    return row["n"]


def top_products_by_clicks(conn: sqlite3.Connection, since_days: int = None, limit: int = 25):
    where_extra = "" if since_days is None else "AND events.created_at >= datetime('now', ?)"
    params = [] if since_days is None else [f"-{since_days} days"]
    return conn.execute(
        f"""
        SELECT products.id, products.name, COUNT(events.id) AS clicks
        FROM events JOIN products ON products.id = events.product_id
        WHERE events.event_type = 'click' {where_extra}
        GROUP BY products.id ORDER BY clicks DESC LIMIT ?
        """,
        (*params, limit),
    ).fetchall()


def top_pages_by_views(conn: sqlite3.Connection, since_days: int = None, limit: int = 25):
    where_extra = "" if since_days is None else "AND created_at >= datetime('now', ?)"
    params = [] if since_days is None else [f"-{since_days} days"]
    return conn.execute(
        f"""
        SELECT path, COUNT(*) AS views FROM events
        WHERE event_type = 'pageview' {where_extra}
        GROUP BY path ORDER BY views DESC LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
