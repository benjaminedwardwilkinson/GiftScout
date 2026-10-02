import asyncio
import json
import os
import re
import secrets
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from PIL import Image, ImageOps
from starlette.middleware.sessions import SessionMiddleware

from app import backup, db
from app.auth import check_credentials, is_logged_in, login_redirect, hash_password, verify_password

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path("/data/uploads")

# Site identity is configuration, not code — this is what lets the same
# application power DadGiftScout, MomGiftScout, CoffeeScout, etc.
SITE_NAME = os.environ.get("SITE_NAME", "GiftScout")
SITE_TAGLINE = os.environ.get("SITE_TAGLINE", "A few genuinely good recommendations.")
# Optional: set this to your real domain (e.g. https://dadgiftscout.com) so
# canonical URLs, Open Graph tags, and the sitemap are correct behind a
# reverse proxy. If unset, it's inferred from each request.
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
# Optional: the content value from Google Search Console's "HTML tag"
# verification method (Settings > Ownership verification > HTML tag —
# copy just the content="..." value, not the whole tag). Leave unset until
# you're ready to verify; harmless either way.
GOOGLE_SITE_VERIFICATION = os.environ.get("GOOGLE_SITE_VERIFICATION", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or uuid.uuid4().hex

app = FastAPI(title=SITE_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    """Blocks any admin POST that doesn't carry a token matching the one
    tied to this browser's session. Registered after SessionMiddleware
    (see ordering note below) so request.session is already populated by
    the time this runs.

    Ordering matters here: Starlette wraps middleware so the first one
    registered is outermost (runs first on the way in). SessionMiddleware
    is registered above this, so it decodes the session cookie before this
    function ever sees the request.
    """
    if request.method == "POST" and request.url.path.startswith("/admin"):
        form = await request.form()
        token = form.get("csrf_token")
        session_token = request.session.get("csrf_token")
        if not token or not session_token or not secrets.compare_digest(str(token), str(session_token)):
            return Response(
                "That form looks stale or invalid (its security token didn't match). "
                "Go back, refresh the page, and try again.",
                status_code=403,
                media_type="text/plain",
            )
    return await call_next(request)


def csrf_field(request: Request) -> Markup:
    """Called from templates as {{ csrf_field(request) }} right after every
    admin <form method="post">. Creates the session's CSRF token on first
    use and renders it as a hidden input."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return Markup(f'<input type="hidden" name="csrf_token" value="{token}">')

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["site_name"] = SITE_NAME
templates.env.globals["site_tagline"] = SITE_TAGLINE
templates.env.globals["google_site_verification"] = GOOGLE_SITE_VERIFICATION


def _to_json_ld(data) -> Markup:
    """Safe JSON for embedding in a <script type=application/ld+json> tag."""
    raw = json.dumps(data)
    raw = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return Markup(raw)


templates.env.filters["tojson_ld"] = _to_json_ld
templates.env.globals["csrf_field"] = csrf_field

# Cache-busting token for /static/* assets, so a browser that cached the old
# style.css doesn't keep using it after a deploy that renamed CSS classes.
_STATIC_VERSION = uuid.uuid4().hex[:8]
templates.env.globals["asset_version"] = _STATIC_VERSION

# Directories must exist before StaticFiles mounts them below.
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
async def startup():
    db.init_db()
    conn = db.get_connection()
    try:
        # One-time seed: the very first time the app runs against an empty
        # database, the admin account is created from these env vars. After
        # that, credentials live in the database and env vars are ignored —
        # change them from /admin/settings instead.
        if db.get_setting(conn, "admin_username") is None:
            initial_username = os.environ.get("ADMIN_USERNAME", "admin")
            initial_password = os.environ.get("ADMIN_PASSWORD", "admin")
            db.set_setting(conn, "admin_username", initial_username)
            db.set_setting(conn, "admin_password_hash", hash_password(initial_password))
            conn.commit()
    finally:
        conn.close()
    asyncio.create_task(backup.scheduler_loop())


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


MAX_IMAGE_DIMENSION = 1600
JPEG_QUALITY = 82


def _save_upload(image: UploadFile) -> Optional[str]:
    """Save one uploaded file under /data/uploads, compressed to a
    reasonable size. Returns its public path.

    Every upload is normalized to a JPEG capped at MAX_IMAGE_DIMENSION on
    its longest side — a phone photo can be 4-8MB; there's no reason to
    store (or back up, or serve to a visitor's phone) the original size.
    EXIF orientation is applied before compressing, so photos taken in
    portrait/upside-down on a phone display right-side-up. If Pillow can't
    process the file for some reason, we fall back to saving it as-is
    rather than losing the upload.
    """
    if image is None or not image.filename:
        return None
    try:
        img = Image.open(image.file)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
        safe_name = f"{uuid.uuid4().hex}.jpg"
        dest = UPLOAD_DIR / safe_name
        img.save(dest, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return f"/uploads/{safe_name}"
    except Exception:
        image.file.seek(0)
        ext = Path(image.filename).suffix.lower() or ".jpg"
        safe_name = f"{uuid.uuid4().hex}{ext}"
        dest = UPLOAD_DIR / safe_name
        with dest.open("wb") as out_file:
            shutil.copyfileobj(image.file, out_file)
        return f"/uploads/{safe_name}"


def _delete_upload(public_path: Optional[str]) -> None:
    """Remove a previously-saved upload from disk, if it's one of ours."""
    if not public_path or not public_path.startswith("/uploads/"):
        return
    file_path = UPLOAD_DIR / Path(public_path).name
    file_path.unlink(missing_ok=True)


def _site_root(request: Request) -> str:
    if SITE_URL:
        return SITE_URL
    return str(request.base_url).rstrip("/")


LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


def _login_locked_until(request: Request) -> Optional[datetime]:
    locked_until = request.session.get("login_locked_until")
    return datetime.fromisoformat(locked_until) if locked_until else None


def _login_lockout_message(locked_until: datetime) -> str:
    minutes_left = max(1, int((locked_until - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
    return f"Too many failed attempts. Try again in about {minutes_left} minute(s)."


def _register_failed_login(request: Request) -> None:
    count = request.session.get("login_fail_count", 0) + 1
    request.session["login_fail_count"] = count
    if count >= LOGIN_MAX_ATTEMPTS:
        locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
        request.session["login_locked_until"] = locked_until.isoformat()


def _clear_login_attempts(request: Request) -> None:
    request.session.pop("login_fail_count", None)
    request.session.pop("login_locked_until", None)


def _first_image_url(product_id: int, images_by_product: dict, site_root: str) -> Optional[str]:
    images = images_by_product.get(product_id, [])
    return site_root + images[0]["image_path"] if images else None


def _build_item_list_ld(products, images_by_product: dict, site_root: str) -> Optional[dict]:
    if not products:
        return None
    elements = []
    for idx, p in enumerate(products, start=1):
        item = {"@type": "Product", "name": p["name"]}
        if p["slug"]:
            item["url"] = f"{site_root}/product/{p['slug']}"
        description = p["why_recommend"] or p["description"]
        if description:
            item["description"] = description
        image_url = _first_image_url(p["id"], images_by_product, site_root)
        if image_url:
            item["image"] = image_url
        if p["price"]:
            numeric_price = re.sub(r"[^0-9.]", "", p["price"])
            if numeric_price:
                item["offers"] = {
                    "@type": "Offer",
                    "price": numeric_price,
                    "priceCurrency": "USD",
                    "url": p["affiliate_url"],
                }
        elements.append({"@type": "ListItem", "position": idx, "item": item})
    return {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": elements}


def _products_context(request: Request, products, category=None) -> dict:
    """Shared context builder for the homepage and category pages."""
    conn = db.get_connection()
    try:
        images_by_product = {p["id"]: db.get_product_images(conn, p["id"]) for p in products}
        categories_nav = db.get_categories_with_active_products(conn)
    finally:
        conn.close()

    site_root = _site_root(request)
    og_image = None
    for p in products:
        og_image = _first_image_url(p["id"], images_by_product, site_root)
        if og_image:
            break

    if category is not None:
        meta_description = f"{category['name']} gift ideas — curated picks from {SITE_NAME}."
        canonical_url = f"{site_root}/category/{category['slug']}"
    else:
        meta_description = SITE_TAGLINE
        canonical_url = f"{site_root}/"

    return {
        "request": request,
        "products": products,
        "images_by_product": images_by_product,
        "categories_nav": categories_nav,
        "active_category_id": category["id"] if category is not None else None,
        "meta_description": meta_description,
        "canonical_url": canonical_url,
        "og_image": og_image,
        "structured_data": _build_item_list_ld(products, images_by_product, site_root),
    }


# ---------------------------------------------------------------------------
# Public site
# ---------------------------------------------------------------------------

@app.get("/")
def homepage(request: Request):
    conn = db.get_connection()
    try:
        products = db.get_active_products(conn)
        db.log_event(conn, "pageview", path="/")
    finally:
        conn.close()
    return templates.TemplateResponse("index.html", _products_context(request, products))


@app.get("/category/{slug}")
def category_page(request: Request, slug: str):
    conn = db.get_connection()
    try:
        category = db.get_category_by_slug(conn, slug)
        if category is None:
            raise HTTPException(status_code=404, detail="Category not found")
        products = db.get_active_products_by_category(conn, category["id"])
        db.log_event(conn, "pageview", path=f"/category/{slug}", category_id=category["id"])
    finally:
        conn.close()
    context = _products_context(request, products, category=category)
    context["category"] = category
    return templates.TemplateResponse("category.html", context)


@app.get("/product/{slug}")
def product_page(request: Request, slug: str):
    conn = db.get_connection()
    try:
        product = db.get_product_by_slug(conn, slug)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        images = db.get_product_images(conn, product["id"])
        db.log_event(conn, "pageview", path=f"/product/{slug}", product_id=product["id"])
    finally:
        conn.close()

    site_root = _site_root(request)
    images_by_product = {product["id"]: images}
    og_image = _first_image_url(product["id"], images_by_product, site_root)
    description = product["why_recommend"] or product["description"] or SITE_TAGLINE
    canonical_url = f"{site_root}/product/{product['slug']}"

    structured_data = {"@context": "https://schema.org", "@type": "Product", "name": product["name"], "url": canonical_url}
    if description:
        structured_data["description"] = description
    if og_image:
        structured_data["image"] = og_image
    if product["price"]:
        numeric_price = re.sub(r"[^0-9.]", "", product["price"])
        if numeric_price:
            structured_data["offers"] = {
                "@type": "Offer",
                "price": numeric_price,
                "priceCurrency": "USD",
                "url": product["affiliate_url"],
            }

    return templates.TemplateResponse(
        "product.html",
        {
            "request": request,
            "product": product,
            "images": images,
            "meta_description": description,
            "canonical_url": canonical_url,
            "og_image": og_image,
            "structured_data": structured_data,
        },
    )


@app.get("/go/{product_id}")
def go_to_product(product_id: int):
    """Public redirect that logs a click before sending the visitor on to
    the affiliate URL — this is the only way click counts get recorded, so
    every 'Check it out' link points here instead of straight to the
    affiliate link."""
    conn = db.get_connection()
    try:
        product = db.get_product(conn, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        db.log_event(conn, "click", product_id=product_id)
        url = product["affiliate_url"]
    finally:
        conn.close()
    return RedirectResponse(url=url, status_code=302)


@app.get("/robots.txt")
def robots_txt(request: Request):
    site_root = _site_root(request)
    content = f"User-agent: *\nAllow: /\n\nSitemap: {site_root}/sitemap.xml\n"
    return Response(content=content, media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml(request: Request):
    site_root = _site_root(request)
    conn = db.get_connection()
    try:
        categories = db.get_categories_with_active_products(conn)
        products = db.get_active_products(conn)
    finally:
        conn.close()
    urls = [f"{site_root}/"] + [f"{site_root}/category/{c['slug']}" for c in categories]
    urls += [f"{site_root}/product/{p['slug']}" for p in products if p["slug"]]
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    body += [f"  <url><loc>{u}</loc></url>" for u in urls]
    body.append("</urlset>")
    return Response(content="\n".join(body), media_type="application/xml")


# ---------------------------------------------------------------------------
# Admin: auth
# ---------------------------------------------------------------------------

@app.get("/admin/login")
def login_form(request: Request):
    if is_logged_in(request):
        return RedirectResponse(url="/admin", status_code=303)
    locked_until = _login_locked_until(request)
    error = _login_lockout_message(locked_until) if locked_until and datetime.now(timezone.utc) < locked_until else None
    return templates.TemplateResponse(
        "admin/login.html", {"request": request, "error": error}
    )


@app.post("/admin/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    locked_until = _login_locked_until(request)
    if locked_until and datetime.now(timezone.utc) < locked_until:
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": _login_lockout_message(locked_until)},
            status_code=429,
        )
    conn = db.get_connection()
    try:
        ok = check_credentials(conn, username, password)
    finally:
        conn.close()
    if ok:
        _clear_login_attempts(request)
        request.session["logged_in"] = True
        return RedirectResponse(url="/admin", status_code=303)
    _register_failed_login(request)
    return templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "error": "Incorrect username or password."},
        status_code=401,
    )


@app.get("/admin/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/admin/settings")
def admin_settings(request: Request):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        current_username = db.get_setting(conn, "admin_username", "admin")
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin/settings.html",
        {
            "request": request,
            "current_username": current_username,
            "error": None,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@app.post("/admin/settings")
def update_settings(
    request: Request,
    username: str = Form(...),
    current_password: str = Form(...),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    if not is_logged_in(request):
        return login_redirect()

    conn = db.get_connection()
    try:
        current_username = db.get_setting(conn, "admin_username", "admin")
        stored_hash = db.get_setting(conn, "admin_password_hash", "")

        if not verify_password(current_password, stored_hash):
            return templates.TemplateResponse(
                "admin/settings.html",
                {
                    "request": request,
                    "current_username": current_username,
                    "error": "Current password is incorrect.",
                    "saved": False,
                },
                status_code=401,
            )

        if new_password or confirm_password:
            if new_password != confirm_password:
                return templates.TemplateResponse(
                    "admin/settings.html",
                    {
                        "request": request,
                        "current_username": current_username,
                        "error": "New passwords don't match.",
                        "saved": False,
                    },
                    status_code=400,
                )
            if len(new_password) < 8:
                return templates.TemplateResponse(
                    "admin/settings.html",
                    {
                        "request": request,
                        "current_username": current_username,
                        "error": "New password must be at least 8 characters.",
                        "saved": False,
                    },
                    status_code=400,
                )
            db.set_setting(conn, "admin_password_hash", hash_password(new_password))

        if username.strip() and username.strip() != current_username:
            db.set_setting(conn, "admin_username", username.strip())

        conn.commit()
    finally:
        conn.close()

    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)


@app.get("/admin/backup")
def admin_backup(request: Request):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        destination = db.get_setting(conn, "backup_destination", "")
        frequency = db.get_setting(conn, "backup_frequency", "off")
        last_run_at = db.get_setting(conn, "backup_last_run_at")
        last_status = db.get_setting(conn, "backup_last_status")
        last_output = db.get_setting(conn, "backup_last_output")
        restore_last_run_at = db.get_setting(conn, "restore_last_run_at")
        restore_last_status = db.get_setting(conn, "restore_last_status")
        restore_last_output = db.get_setting(conn, "restore_last_output")
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin/backup.html",
        {
            "request": request,
            "destination": destination,
            "frequency": frequency,
            "last_run_at": last_run_at,
            "last_status": last_status,
            "last_output": last_output,
            "restore_last_run_at": restore_last_run_at,
            "restore_last_status": restore_last_status,
            "restore_last_output": restore_last_output,
            "ssh_key_present": os.path.exists(backup.SSH_KEY_PATH),
        },
    )


@app.post("/admin/backup/save")
def save_backup_settings(request: Request, destination: str = Form(""), frequency: str = Form("off")):
    if not is_logged_in(request):
        return login_redirect()
    if frequency not in ("off", "daily", "weekly"):
        frequency = "off"
    conn = db.get_connection()
    try:
        db.set_setting(conn, "backup_destination", destination.strip())
        db.set_setting(conn, "backup_frequency", frequency)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/backup", status_code=303)


@app.post("/admin/backup/run")
def run_backup_route(request: Request):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        destination = db.get_setting(conn, "backup_destination", "")
    finally:
        conn.close()
    backup.run_backup_now(destination)
    return RedirectResponse(url="/admin/backup", status_code=303)


@app.get("/admin/backup/restore")
def restore_backup_form(request: Request):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        destination = db.get_setting(conn, "backup_destination", "")
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin/backup_restore.html", {"request": request, "source": destination, "error": None}
    )


@app.post("/admin/backup/restore")
def restore_backup_route(request: Request, source: str = Form(...), confirm: str = Form("")):
    if not is_logged_in(request):
        return login_redirect()
    if confirm != "RESTORE":
        return templates.TemplateResponse(
            "admin/backup_restore.html",
            {"request": request, "source": source, "error": "Type RESTORE exactly (all capitals) to confirm."},
            status_code=400,
        )
    backup.restore_from_backup(source)
    return RedirectResponse(url="/admin/backup", status_code=303)


# ---------------------------------------------------------------------------
# Admin: dashboard
# ---------------------------------------------------------------------------

@app.get("/admin")
def admin_dashboard(request: Request):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        products = db.get_all_products(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin/dashboard.html", {"request": request, "products": products}
    )


@app.get("/admin/stats")
def admin_stats(request: Request):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        views_7d = db.count_events(conn, "pageview", since_days=7)
        views_30d = db.count_events(conn, "pageview", since_days=30)
        views_all = db.count_events(conn, "pageview")
        clicks_7d = db.count_events(conn, "click", since_days=7)
        clicks_30d = db.count_events(conn, "click", since_days=30)
        clicks_all = db.count_events(conn, "click")
        top_products = db.top_products_by_clicks(conn)
        top_pages = db.top_pages_by_views(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin/stats.html",
        {
            "request": request,
            "views_7d": views_7d,
            "views_30d": views_30d,
            "views_all": views_all,
            "clicks_7d": clicks_7d,
            "clicks_30d": clicks_30d,
            "clicks_all": clicks_all,
            "top_products": top_products,
            "top_pages": top_pages,
        },
    )


# ---------------------------------------------------------------------------
# Admin: create product
# ---------------------------------------------------------------------------

@app.get("/admin/products/new")
def new_product_form(request: Request):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        categories = db.get_categories(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin/product_form.html",
        {"request": request, "product": None, "images": [], "categories": categories},
    )


@app.post("/admin/products/new")
def create_product(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    why_recommend: str = Form(""),
    price: str = Form(""),
    category_id: str = Form(""),
    affiliate_url: str = Form(...),
    images: List[UploadFile] = File(None),
):
    if not is_logged_in(request):
        return login_redirect()

    cat_id = int(category_id) if category_id.strip().isdigit() else None

    conn = db.get_connection()
    try:
        order = db.next_display_order(conn)
        cur = conn.execute(
            """
            INSERT INTO products
                (name, description, why_recommend, price, category_id, affiliate_url, display_order)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, description, why_recommend, price, cat_id, affiliate_url, order),
        )
        product_id = cur.lastrowid
        db.assign_product_slug(conn, product_id, name, slug)

        for image in images or []:
            saved_path = _save_upload(image)
            if saved_path:
                db.add_product_image(conn, product_id, saved_path)

        conn.commit()
    finally:
        conn.close()

    return RedirectResponse(url="/admin", status_code=303)


# ---------------------------------------------------------------------------
# Admin: edit product
# ---------------------------------------------------------------------------

@app.get("/admin/products/{product_id}/edit")
def edit_product_form(request: Request, product_id: int):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        product = db.get_product(conn, product_id)
        if product is None:
            return RedirectResponse(url="/admin", status_code=303)
        images = db.get_product_images(conn, product_id)
        categories = db.get_categories(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin/product_form.html",
        {"request": request, "product": product, "images": images, "categories": categories},
    )


@app.post("/admin/products/{product_id}/edit")
def update_product(
    request: Request,
    product_id: int,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    why_recommend: str = Form(""),
    price: str = Form(""),
    category_id: str = Form(""),
    affiliate_url: str = Form(...),
    images: List[UploadFile] = File(None),
):
    if not is_logged_in(request):
        return login_redirect()

    cat_id = int(category_id) if category_id.strip().isdigit() else None

    conn = db.get_connection()
    try:
        product = db.get_product(conn, product_id)
        if product is None:
            return RedirectResponse(url="/admin", status_code=303)

        conn.execute(
            """
            UPDATE products
            SET name = ?, description = ?, why_recommend = ?, price = ?, category_id = ?, affiliate_url = ?
            WHERE id = ?
            """,
            (name, description, why_recommend, price, cat_id, affiliate_url, product_id),
        )
        db.assign_product_slug(conn, product_id, name, slug)

        for image in images or []:
            saved_path = _save_upload(image)
            if saved_path:
                db.add_product_image(conn, product_id, saved_path)

        conn.commit()
    finally:
        conn.close()

    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/products/{product_id}/photos/{image_id}/delete")
def delete_product_image_route(request: Request, product_id: int, image_id: int):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        removed_path = db.delete_product_image(conn, image_id)
        conn.commit()
    finally:
        conn.close()
    _delete_upload(removed_path)
    return RedirectResponse(url=f"/admin/products/{product_id}/edit", status_code=303)


@app.post("/admin/products/{product_id}/photos/{image_id}/move/{direction}")
def move_product_image_route(request: Request, product_id: int, image_id: int, direction: str):
    if not is_logged_in(request):
        return login_redirect()
    if direction not in ("up", "down"):
        return RedirectResponse(url=f"/admin/products/{product_id}/edit", status_code=303)
    conn = db.get_connection()
    try:
        db.move_product_image(conn, image_id, direction)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/admin/products/{product_id}/edit", status_code=303)


# ---------------------------------------------------------------------------
# Admin: delete / archive / restore / reorder products
# ---------------------------------------------------------------------------

@app.post("/admin/products/{product_id}/delete")
def delete_product_route(request: Request, product_id: int):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        paths = db.delete_product(conn, product_id)
        conn.commit()
    finally:
        conn.close()
    for path in paths:
        _delete_upload(path)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/products/{product_id}/archive")
def archive_product_route(request: Request, product_id: int):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        db.archive_product(conn, product_id)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/products/{product_id}/restore")
def restore_product_route(request: Request, product_id: int):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        db.restore_product(conn, product_id)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/products/{product_id}/move/{direction}")
def move_product_route(request: Request, product_id: int, direction: str):
    if not is_logged_in(request):
        return login_redirect()
    if direction not in ("up", "down"):
        return RedirectResponse(url="/admin", status_code=303)
    conn = db.get_connection()
    try:
        db.move_product(conn, product_id, direction)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin", status_code=303)


# ---------------------------------------------------------------------------
# Admin: categories
# ---------------------------------------------------------------------------

@app.get("/admin/categories")
def admin_categories(request: Request):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        categories = db.get_categories(conn)
        counts = {
            c["id"]: conn.execute(
                "SELECT COUNT(*) AS n FROM products WHERE category_id = ?", (c["id"],)
            ).fetchone()["n"]
            for c in categories
        }
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin/categories.html", {"request": request, "categories": categories, "counts": counts}
    )


@app.post("/admin/categories/new")
def create_category_route(request: Request, name: str = Form(...)):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        if name.strip():
            db.create_category(conn, name.strip())
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/categories", status_code=303)


@app.get("/admin/categories/{category_id}/edit")
def edit_category_form(request: Request, category_id: int):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        category = db.get_category(conn, category_id)
        if category is None:
            return RedirectResponse(url="/admin/categories", status_code=303)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin/category_form.html", {"request": request, "category": category}
    )


@app.post("/admin/categories/{category_id}/edit")
def update_category_route(request: Request, category_id: int, name: str = Form(...), slug: str = Form("")):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        if name.strip():
            db.update_category(conn, category_id, name.strip(), slug.strip())
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/categories", status_code=303)


@app.post("/admin/categories/{category_id}/delete")
def delete_category_route(request: Request, category_id: int):
    if not is_logged_in(request):
        return login_redirect()
    conn = db.get_connection()
    try:
        db.delete_category(conn, category_id)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/categories", status_code=303)


@app.post("/admin/categories/{category_id}/move/{direction}")
def move_category_route(request: Request, category_id: int, direction: str):
    if not is_logged_in(request):
        return login_redirect()
    if direction not in ("up", "down"):
        return RedirectResponse(url="/admin/categories", status_code=303)
    conn = db.get_connection()
    try:
        db.move_category(conn, category_id, direction)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/categories", status_code=303)
