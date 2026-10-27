import os
import re
import io
import time
import json
import base64
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, unquote

import requests
import pymupdf
import gdown
from bs4 import BeautifulSoup
from PIL import Image
from dotenv import load_dotenv
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from huggingface_hub import HfApi, CommitOperationCopy, CommitOperationDelete
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CATALOG_PATH = BASE_DIR / "catalog.json"
ANNOUNCEMENTS_PATH = BASE_DIR / "announcements.json"
COVERS_DIR = BASE_DIR / "assets" / "covers"
BANNERS_DIR = BASE_DIR / "assets" / "banners"
COVERS_DIR.mkdir(parents=True, exist_ok=True)
BANNERS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Mulberry Operations & CMS Portal")
app.mount("/assets", StaticFiles(directory=str(BASE_DIR / "assets")), name="assets")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ==========================================
# CONFIGURATION & ROUTING MAPS
# ==========================================
VAULT_KEY = os.getenv("VAULT_ENCRYPTION_KEY", "").strip()
GH_TOKEN = os.getenv("GH_TOKEN", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
HF_REPO_ID = os.getenv("HF_REPO_ID", "sahabul1064/mulberry-archives").strip()
CATALOG_REPO = os.getenv("CATALOG_REPO", "doubtsolved/mulberry-catalog").strip()
ENV_CF_WORKER_URL = os.getenv("CF_WORKER_URL", "").strip().rstrip("/")

PROF_TO_YEAR = {
    "1st Prof": "1st Year",
    "2nd Prof": "2nd Year",
    "3rd Prof Part 1": "3rd Year",
    "Final Prof": "Final Year"
}

PROF_SLUG_MAP = {
    "1st Prof": "prof1",
    "2nd Prof": "prof2",
    "3rd Prof Part 1": "prof3",
    "Final Prof": "prof4"
}

DEFAULT_TYPE_SLUG_MAP = {
    "Standard Textbook": "textbooks",
    "Question Bank (PYQ)": "pyqs",
    "Handwritten Notes": "notes",
    "Review / High-Yield": "review",
    "Atlas / Practical": "practicals"
}

SUBJECT_TO_PROF = {
    "Anatomy": "1st Prof",
    "Physiology": "1st Prof",
    "Biochemistry": "1st Prof",
    "Pathology": "2nd Prof",
    "Pharmacology": "2nd Prof",
    "Microbiology": "2nd Prof",
    "Forensic Medicine": "2nd Prof",
    "Community Medicine": "3rd Prof Part 1",
    "Ophthalmology": "3rd Prof Part 1",
    "ENT": "3rd Prof Part 1",
    "General Medicine": "Final Prof",
    "General Surgery": "Final Prof",
    "Obstetrics & Gynaecology": "Final Prof",
    "Paediatrics": "Final Prof",
    "Orthopaedics": "Final Prof",
    "Dermatology": "Final Prof",
    "Psychiatry": "Final Prof",
    "Radiology": "Final Prof",
    "Anaesthesiology": "Final Prof"
}

job_logs: List[str] = []
is_processing = False


# ==========================================
# UTILITY HELPERS
# ==========================================
def log(msg: str):
    global job_logs
    timestamp = time.strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted)
    job_logs.append(formatted)


def slugify(text: str) -> str:
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '_', text)


def get_prof_slug(prof: str) -> str:
    return PROF_SLUG_MAP.get(prof, slugify(prof) or "prof1")


def get_type_slug(asset_type: str) -> str:
    return DEFAULT_TYPE_SLUG_MAP.get(asset_type, slugify(asset_type) or "general")


def build_hf_path(prof: str, asset_type: str, book_id: str) -> str:
    p_slug = get_prof_slug(prof)
    t_slug = get_type_slug(asset_type)
    return f"{p_slug}/{t_slug}/{book_id}.bin"


def load_catalog() -> Dict[str, Any]:
    if CATALOG_PATH.exists():
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {
            "version": 2,
            "lastUpdated": int(time.time() * 1000),
            "filters": {
                "profs": list(PROF_TO_YEAR.keys()),
                "types": list(DEFAULT_TYPE_SLUG_MAP.keys()),
                "subjects": list(SUBJECT_TO_PROF.keys())
            },
            "books": []
        }
    data.setdefault("filters", {})
    data["filters"].setdefault("profs", list(PROF_TO_YEAR.keys()))
    data["filters"].setdefault("types", list(DEFAULT_TYPE_SLUG_MAP.keys()))
    data["filters"].setdefault("subjects", list(SUBJECT_TO_PROF.keys()))
    data.setdefault("books", [])
    return data


def save_catalog(catalog: Dict[str, Any]):
    catalog["lastUpdated"] = int(time.time() * 1000)
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)


def load_announcements() -> Dict[str, Any]:
    if ANNOUNCEMENTS_PATH.exists():
        try:
            with open(ANNOUNCEMENTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"version": 1, "lastUpdated": int(time.time() * 1000), "banners": []}
    else:
        data = {"version": 1, "lastUpdated": int(time.time() * 1000), "banners": []}
    data.setdefault("version", 1)
    data.setdefault("banners", [])
    return data


def save_announcements(data: Dict[str, Any]):
    data["lastUpdated"] = int(time.time() * 1000)
    data["version"] = int(data.get("version", 1)) + 1
    with open(ANNOUNCEMENTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_worker_base_url() -> str:
    """Resolves CF Worker base URL from env, existing catalog records, or default."""
    if ENV_CF_WORKER_URL:
        return ENV_CF_WORKER_URL
    try:
        catalog = load_catalog()
        for b in catalog.get("books", []):
            for m in b.get("download", {}).get("mirrors", []):
                url = m.get("url", "")
                if ".workers.dev/" in url:
                    return url.split(".workers.dev/")[0] + ".workers.dev"
    except Exception:
        pass
    return "https://mulberry-vault.sahabulmondal2023.workers.dev"


def build_worker_url(hf_path: str) -> str:
    base = get_worker_base_url()
    return f"{base}/{hf_path}"


def sync_git(commit_message: str) -> bool:
    """Stages catalog.json, announcements.json, covers, and banners, then commits and pushes to GitHub."""
    log(f"Git Sync: '{commit_message}'...")
    subprocess.run(["git", "config", "user.name", "Sahabul Mondal"], cwd=BASE_DIR, check=False)
    subprocess.run(["git", "config", "user.email", "sahabul@users.noreply.github.com"], cwd=BASE_DIR, check=False)

    lfs_hook = BASE_DIR / ".git" / "hooks" / "pre-push"
    if lfs_hook.exists():
        try:
            lfs_hook.unlink()
        except Exception:
            pass

    remote_target = f"https://x-access-token:{GH_TOKEN}@github.com/{CATALOG_REPO}.git" if GH_TOKEN else "origin"

    subprocess.run(["git", "pull", remote_target, "main", "--rebase"], cwd=BASE_DIR, check=False, capture_output=True)
    subprocess.run(["git", "add", "assets/covers/", "assets/banners/", "catalog.json"], cwd=BASE_DIR, check=False)
    if ANNOUNCEMENTS_PATH.exists():
        subprocess.run(["git", "add", "announcements.json"], cwd=BASE_DIR, check=False)

    subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=BASE_DIR, capture_output=True, text=True, check=False
    )

    push = subprocess.run(
        ["git", "push", remote_target, "HEAD:main"],
        cwd=BASE_DIR, capture_output=True, text=True, check=False
    )

    if push.returncode == 0:
        log("Git Sync Successful!")
        return True
    else:
        log(f"Git Push Note: {push.stderr.strip()}")
        return False


def build_tags(title: str, author: str, subject: str, prof: str, book_type: str, extra_tags: str) -> List[str]:
    tags = set()
    tags.add(prof.lower())
    tags.add(PROF_TO_YEAR.get(prof, "").lower())
    tags.add(subject.lower())
    tags.add(book_type.lower())
    if "pyq" in book_type.lower():
        tags.add("pyq")
    for word in re.findall(r'\b\w{3,}\b', title.lower()):
        tags.add(word)
    for word in re.findall(r'\b\w{3,}\b', author.lower()):
        if word not in ["faculty", "medical", "books", "dr", "and", "prof", "students", "local", "import"]:
            tags.add(word)
    if extra_tags:
        for t in extra_tags.split(","):
            cleaned = t.strip().lower()
            if cleaned:
                tags.add(cleaned)
    return sorted(list(filter(None, tags)))


def decode_base64_bytes(b64_string: str) -> bytes:
    """Safely decodes a Base64 string (stripping any data URI header if present)."""
    if "," in b64_string:
        b64_string = b64_string.split(",", 1)[1]
    return base64.b64decode(b64_string)


def save_banner_webp_16_6(banner_id: str, b64_image: str) -> str:
    """Decodes an uploaded image, crops/resizes to 16:6 (1200x450) WebP, and saves to assets/banners/."""
    raw_bytes = decode_base64_bytes(b64_image)
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

    target_w, target_h = 1200, 450
    target_ratio = target_w / target_h
    src_w, src_h = img.size
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    dest_file = BANNERS_DIR / f"{banner_id}.webp"
    img.save(str(dest_file), format="WEBP", quality=85)
    return f"https://cdn.jsdelivr.net/gh/{CATALOG_REPO}@main/assets/banners/{banner_id}.webp"


# ==========================================
# HUGGING FACE OPERATIONS
# ==========================================
def get_hf_api() -> HfApi:
    if not HF_TOKEN:
        raise ValueError("HF_TOKEN is missing! Set it in GitHub Codespaces Secrets or .env")
    return HfApi(token=HF_TOKEN)


def find_existing_hf_path(book: Dict[str, Any], hf_files: List[str]) -> Optional[str]:
    book_id = book["id"]
    target_filename = f"{book_id}.bin"

    for m in book.get("download", {}).get("mirrors", []):
        url = m.get("url", "")
        if "resolve/main/" in url:
            candidate = url.split("resolve/main/")[-1].strip("/")
            if candidate in hf_files:
                return candidate
        elif ".workers.dev/" in url:
            candidate = url.split(".workers.dev/")[-1].strip("/")
            if candidate in hf_files:
                return candidate

    for f in hf_files:
        if f == target_filename or f.endswith(f"/{target_filename}"):
            return f

    return None


def relocate_hf_file_if_needed(book: Dict[str, Any], new_prof: str, new_type: str) -> str:
    book_id = book["id"]
    desired_path = build_hf_path(new_prof, new_type, book_id)

    api = get_hf_api()
    hf_files = api.list_repo_files(repo_id=HF_REPO_ID, repo_type="dataset")
    current_path = find_existing_hf_path(book, hf_files)

    if not current_path:
        log(f"Notice: {book_id}.bin not found at old HF path; setting path to {desired_path}")
        return desired_path

    if current_path == desired_path:
        return desired_path

    log(f"Relocating on Hugging Face: '{current_path}' -> '{desired_path}'...")
    operations = [
        CommitOperationCopy(src_path_in_repo=current_path, path_in_repo=desired_path),
        CommitOperationDelete(path_in_repo=current_path)
    ]
    api.create_commit(
        repo_id=HF_REPO_ID,
        repo_type="dataset",
        operations=operations,
        commit_message=f"chore(move): relocate {book_id}.bin to {desired_path}"
    )
    log(f"Successfully moved file on HF to {desired_path}")
    return desired_path


# ==========================================
# SOURCE DISCOVERY (DRIVE & DIRECT PDF URLS)
# ==========================================
def extract_drive_id(url: str) -> str:
    patterns = [
        r'/file/d/([a-zA-Z0-9_-]+)',
        r'/folders/([a-zA-Z0-9_-]+)',
        r'id=([a-zA-Z0-9_-]+)',
        r'/open\?id=([a-zA-Z0-9_-]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return url.strip()


def parse_filename_heuristics(filename: str) -> Dict[str, Any]:
    raw_name = Path(filename).stem.replace("_", " ").replace("-", " ")
    detected_subject = "Anatomy"
    for subj in SUBJECT_TO_PROF.keys():
        if re.search(rf"\b{re.escape(subj)}\b", raw_name, re.IGNORECASE):
            detected_subject = subj
            break
        elif subj == "Biochemistry" and re.search(r"\bbiochem\b", raw_name, re.IGNORECASE):
            detected_subject = "Biochemistry"
            break
        elif subj == "Physiology" and re.search(r"\b(physio|phisiology|guyton|ganong)\b", raw_name, re.IGNORECASE):
            detected_subject = "Physiology"
            break

    detected_type = "Standard Textbook"
    if re.search(r"\b(pyq|pyqs|question|solved|auxilia|auxilla)\b", raw_name, re.IGNORECASE):
        detected_type = "Question Bank (PYQ)"
    elif re.search(r"\b(notes|marrow|prepladder|handwritten)\b", raw_name, re.IGNORECASE):
        detected_type = "Handwritten Notes"
    elif re.search(r"\b(atlas|practical|manual|dissection)\b", raw_name, re.IGNORECASE):
        detected_type = "Atlas / Practical"
    elif re.search(r"\b(review|first aid|rapid)\b", raw_name, re.IGNORECASE):
        detected_type = "Review / High-Yield"

    edition_match = re.search(r'(\d+)(?:st|nd|rd|th|e)?\s*(?:ed|edition)?', raw_name, re.IGNORECASE)
    edition = f"{edition_match.group(1)}th Edition" if edition_match else "Standard Edition"
    detected_prof = SUBJECT_TO_PROF.get(detected_subject, "1st Prof")

    return {
        "title": raw_name.title(),
        "author": "Medical Faculty",
        "edition": edition,
        "subject": detected_subject,
        "prof": detected_prof,
        "type": detected_type,
        "uploader": "Sahabul"
    }


def fetch_public_drive_folder(folder_url_or_id: str) -> List[Dict[str, str]]:
    folder_id = extract_drive_id(folder_url_or_id)
    files = []
    seen_ids = set()
    embed_url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        res = requests.get(embed_url, headers=headers, timeout=15)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            entries = soup.find_all(class_=lambda c: c and "flip-entry" in c)
            for entry in entries:
                raw_id = entry.get("id", "")
                fid = raw_id.replace("entry-", "") if raw_id.startswith("entry-") else entry.get("data-id", "")
                name_el = entry.find(class_=lambda c: c and ("title" in c or "name" in c))
                fname = name_el.get_text(strip=True) if name_el else ""
                if not fid or not fname:
                    a_tag = entry.find("a")
                    if a_tag:
                        fname = fname or a_tag.get_text(strip=True) or a_tag.get("title", "")
                        href = a_tag.get("href", "")
                        m = re.search(r'id=([a-zA-Z0-9_-]+)', href) or re.search(r'/d/([a-zA-Z0-9_-]+)', href)
                        if m:
                            fid = fid or m.group(1)
                if fid and fname and fid not in seen_ids and fname.lower().endswith(".pdf"):
                    seen_ids.add(fid)
                    files.append({"id": fid, "name": fname, "source_type": "drive"})
    except Exception as e:
        print(f"Drive folder error: {e}")
    return files


def resolve_direct_drive_links(raw_text: str) -> List[Dict[str, str]]:
    files = []
    seen_ids = set()
    headers = {"User-Agent": "Mozilla/5.0"}
    for line in [l.strip() for l in raw_text.splitlines() if l.strip()]:
        fid = extract_drive_id(line)
        if fid and fid not in seen_ids:
            try:
                r = requests.get(f"https://drive.google.com/file/d/{fid}/view", headers=headers, timeout=10)
                soup = BeautifulSoup(r.text, "html.parser")
                title = soup.title.string if soup.title else ""
                clean_name = re.sub(r'\s*-\s*Google Drive\s*$', '', title, flags=re.IGNORECASE).strip() or f"doc_{fid}.pdf"
                if not clean_name.lower().endswith(".pdf"):
                    clean_name += ".pdf"
                seen_ids.add(fid)
                files.append({"id": fid, "name": clean_name, "source_type": "drive"})
            except Exception as e:
                print(f"Error resolving Drive link {fid}: {e}")
    return files


def resolve_direct_pdf_links(raw_text: str) -> List[Dict[str, str]]:
    files = []
    seen_urls = set()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for line in [l.strip() for l in raw_text.splitlines() if l.strip()]:
        if not line.startswith("http://") and not line.startswith("https://"):
            continue
        if line in seen_urls:
            continue
        seen_urls.add(line)

        parsed = urlparse(line)
        path_name = Path(unquote(parsed.path)).name
        filename = path_name if path_name else "direct_document.pdf"

        try:
            head = requests.head(line, headers=headers, allow_redirects=True, timeout=8)
            cd = head.headers.get("content-disposition", "")
            if "filename=" in cd:
                match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';]+)["\']?', cd, flags=re.IGNORECASE)
                if match:
                    filename = unquote(match.group(1).strip())
        except Exception:
            pass

        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        files.append({
            "id": line,
            "name": filename,
            "source_type": "direct_url"
        })
    return files


# ==========================================
# BATCH INGESTION WORKER
# ==========================================
def run_batch_ingestion(books_queue: List[Dict[str, Any]]):
    global is_processing
    is_processing = True

    try:
        catalog = load_catalog()
        api = get_hf_api()

        for book in books_queue:
            title = book["title"].strip()
            source_id = (book.get("source_id") or book.get("drive_id") or "").strip()
            source_type = book.get("source_type", "drive")
            author = book.get("author", "Medical Faculty").strip()
            edition = book.get("edition", "Standard Edition").strip()
            subject = book.get("subject", "Anatomy").strip()
            prof = book.get("prof", "1st Prof").strip()
            book_type = book.get("type", "Standard Textbook").strip()
            uploader = book.get("uploader", "Sahabul").strip()
            extra_tags = book.get("extra_tags", "")

            book_id = slugify(title)
            pdf_path = BASE_DIR / f"temp_{book_id}.pdf"
            bin_path = BASE_DIR / f"{book_id}.bin"
            cover_path = COVERS_DIR / f"{book_id}.webp"

            log(f"=== Ingesting: {title} ===")

            # --- 1. DOWNLOAD ---
            try:
                if source_type == "direct_url":
                    log(f"1. Downloading Direct PDF URL ({source_id})...")
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    with requests.get(source_id, stream=True, headers=headers, allow_redirects=True, timeout=120) as r:
                        r.raise_for_status()
                        with open(pdf_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    f.write(chunk)
                    fallback_mirror_url = source_id
                else:
                    log(f"1. Downloading from Google Drive ({source_id})...")
                    gdown.download(id=source_id, output=str(pdf_path), quiet=False)
                    fallback_mirror_url = f"https://drive.google.com/uc?export=download&id={source_id}"
            except Exception as dl_err:
                log(f"ERROR: Download failed for '{title}': {dl_err}. Skipping.")
                if pdf_path.exists():
                    pdf_path.unlink()
                continue

            if not pdf_path.exists() or pdf_path.stat().st_size == 0:
                log(f"ERROR: Downloaded file is empty for '{title}'. Skipping.")
                continue

            # --- 2. VERIFY %PDF SIGNATURE ---
            with open(pdf_path, "rb") as f:
                header_bytes = f.read(1024)

            if b"%PDF" not in header_bytes:
                preview_text = header_bytes.decode("utf-8", errors="ignore")[:160].replace("\n", " ")
                log(f"ERROR: Source returned HTML/invalid data instead of a PDF (Google Drive quota or auth wall): '{preview_text}...'")
                pdf_path.unlink()
                continue

            file_size_mb = round(pdf_path.stat().st_size / (1024 * 1024), 1)

            # --- 3. RENDER 150 DPI WEBP COVER ---
            log(f"2. Rendering 150 DPI WebP cover ({file_size_mb} MB PDF)...")
            doc = pymupdf.open(str(pdf_path))
            if len(doc) == 0:
                log(f"ERROR: '{title}' has 0 pages. Skipping.")
                doc.close()
                pdf_path.unlink()
                continue

            total_pages = len(doc)
            page_1 = doc.load_page(0)
            pix = page_1.get_pixmap(dpi=150)
            if pix.colorspace and pix.colorspace.name == "DeviceCMYK":
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            img.save(str(cover_path), format="WEBP", quality=80)
            doc.close()

            # --- 4. AES-256-GCM ENCRYPTION ---
            log("3. Encrypting payload with AES-256-GCM...")
            if not VAULT_KEY:
                raise ValueError("VAULT_ENCRYPTION_KEY is missing in environment!")
            key = bytes.fromhex(VAULT_KEY)
            aesgcm = AESGCM(key)
            nonce = os.urandom(12)
            with open(pdf_path, "rb") as f:
                plaintext = f.read()
            ciphertext = aesgcm.encrypt(nonce, plaintext, None)
            with open(bin_path, "wb") as f:
                f.write(nonce + ciphertext)

            # --- 5. UPLOAD TO HUGGING FACE ---
            hf_dest_path = build_hf_path(prof, book_type, book_id)
            log(f"4. Uploading to Hugging Face ({HF_REPO_ID} -> {hf_dest_path})...")
            api.upload_file(
                path_or_fileobj=str(bin_path),
                path_in_repo=hf_dest_path,
                repo_id=HF_REPO_ID,
                repo_type="dataset",
                commit_message=f"feat(ingest): add {hf_dest_path}"
            )

            # --- 6. UPDATE CATALOG.JSON ---
            worker_download_url = build_worker_url(hf_dest_path)
            cover_cdn_url = f"https://cdn.jsdelivr.net/gh/{CATALOG_REPO}@main/assets/covers/{book_id}.webp"
            tags = build_tags(title, author, subject, prof, book_type, extra_tags)

            if book_type not in catalog["filters"]["types"]:
                catalog["filters"]["types"].append(book_type)

            record = {
                "id": book_id,
                "title": title,
                "author": author,
                "edition": edition,
                "prof": prof,
                "year": PROF_TO_YEAR.get(prof, "1st Year"),
                "subject": subject,
                "type": book_type,
                "tags": tags,
                "totalPages": total_pages,
                "sizeMb": file_size_mb,
                "coverUrl": cover_cdn_url,
                "download": {
                    "mirrors": [
                        {
                            "provider": "mulberry-vault",
                            "repo": HF_REPO_ID,
                            "url": worker_download_url,
                            "isEncrypted": True,
                            "format": "aes-256-gcm"
                        },
                        {
                            "provider": "source" if source_type == "direct_url" else "drive",
                            "url": fallback_mirror_url,
                            "isEncrypted": False
                        }
                    ],
                    "telegram": {
                        "deepLink": f"https://t.me/MulberryIngestionBot?start={book_id}"
                    }
                },
                "uploader": uploader
            }

            catalog["books"] = [b for b in catalog["books"] if b.get("id") != book_id]
            catalog["books"].insert(0, record)
            save_catalog(catalog)

            if pdf_path.exists():
                pdf_path.unlink()
            if bin_path.exists():
                bin_path.unlink()

            log(f"SUCCESS: Cataloged '{title}' -> {hf_dest_path}")

        sync_git("feat(vault): batch ingest books to HF & update catalog")
        log("=== BATCH INGESTION COMPLETE! ===")

    except Exception as e:
        log(f"CRITICAL ERROR: {str(e)}")
    finally:
        is_processing = False


# ==========================================
# API ROUTES: CMS & CATALOG MANAGEMENT
# ==========================================
@app.get("/", response_class=HTMLResponse)
def index_view(request: Request):
    index_file = BASE_DIR / "templates" / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>templates/index.html not found</h1>", status_code=404)
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


@app.get("/api/catalog")
def api_get_catalog():
    return load_catalog()


class CategoryRequest(BaseModel):
    name: str
    slug: Optional[str] = None


@app.post("/api/categories/add")
def api_add_category(req: CategoryRequest):
    cat_name = req.name.strip()
    if not cat_name:
        raise HTTPException(status_code=400, detail="Category name required")

    catalog = load_catalog()
    if cat_name not in catalog["filters"]["types"]:
        catalog["filters"]["types"].append(cat_name)
        DEFAULT_TYPE_SLUG_MAP[cat_name] = req.slug.strip() if req.slug else slugify(cat_name)
        save_catalog(catalog)
        sync_git(f"chore(catalog): add new asset category '{cat_name}'")

    return {"status": "success", "types": catalog["filters"]["types"]}


class BookUpdateRequest(BaseModel):
    title: str
    author: str
    edition: str
    prof: str
    subject: str
    type: str
    uploader: str
    tags: List[str]
    relocate_hf: bool = True


@app.put("/api/books/{book_id}")
def api_update_book(book_id: str, req: BookUpdateRequest):
    catalog = load_catalog()
    target_book = next((b for b in catalog["books"] if b.get("id") == book_id), None)
    if not target_book:
        raise HTTPException(status_code=404, detail="Book not found in catalog.json")

    old_prof = target_book.get("prof", "")
    old_type = target_book.get("type", "")
    old_subject = target_book.get("subject", "")

    needs_move = req.relocate_hf and (old_prof != req.prof or old_type != req.type)
    relocated_path = None

    if needs_move:
        try:
            relocated_path = relocate_hf_file_if_needed(target_book, req.prof, req.type)
            new_worker_url = build_worker_url(relocated_path)
            mirrors = target_book.get("download", {}).get("mirrors", [])
            updated_mirror = False
            for m in mirrors:
                if m.get("isEncrypted"):
                    m["provider"] = "mulberry-vault"
                    m["repo"] = HF_REPO_ID
                    m["url"] = new_worker_url
                    updated_mirror = True
                    break
            if not updated_mirror:
                mirrors.insert(0, {
                    "provider": "mulberry-vault",
                    "repo": HF_REPO_ID,
                    "url": new_worker_url,
                    "isEncrypted": True,
                    "format": "aes-256-gcm"
                })
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Hugging Face Move Failed: {str(e)}")

    all_known_types = {t.lower() for t in catalog["filters"].get("types", [])}
    all_known_profs = {p.lower() for p in catalog["filters"].get("profs", [])}
    stale_tags = {
        old_type.lower(),
        old_prof.lower(),
        PROF_TO_YEAR.get(old_prof, "").lower(),
        old_subject.lower()
    } | (all_known_types - {req.type.strip().lower()}) | (all_known_profs - {req.prof.strip().lower()})

    cleaned_tags = [
        t.strip().lower() for t in req.tags
        if t.strip() and t.strip().lower() not in stale_tags
    ]
    for mandatory_tag in [
        req.prof.strip().lower(),
        PROF_TO_YEAR.get(req.prof.strip(), "").lower(),
        req.subject.strip().lower(),
        req.type.strip().lower()
    ]:
        if mandatory_tag and mandatory_tag not in cleaned_tags:
            cleaned_tags.append(mandatory_tag)

    if "pyq" in req.type.lower() and "pyq" not in cleaned_tags:
        cleaned_tags.append("pyq")

    target_book["title"] = req.title.strip()
    target_book["author"] = req.author.strip()
    target_book["edition"] = req.edition.strip()
    target_book["prof"] = req.prof.strip()
    target_book["year"] = PROF_TO_YEAR.get(req.prof.strip(), "1st Year")
    target_book["subject"] = req.subject.strip()
    target_book["type"] = req.type.strip()
    target_book["uploader"] = req.uploader.strip()
    target_book["tags"] = sorted(list(set(cleaned_tags)))

    if req.type not in catalog["filters"]["types"]:
        catalog["filters"]["types"].append(req.type)

    save_catalog(catalog)
    sync_git(f"chore(catalog): update metadata for {book_id}")

    return {
        "status": "success",
        "relocated_to": relocated_path,
        "book": target_book
    }


@app.delete("/api/books/{book_id}")
def api_delete_book(book_id: str, mode: str = "soft"):
    if mode not in ["soft", "purge"]:
        raise HTTPException(status_code=400, detail="Mode must be 'soft' or 'purge'")
    catalog = load_catalog()
    target_book = next((b for b in catalog["books"] if b.get("id") == book_id), None)
    if not target_book:
        raise HTTPException(status_code=404, detail="Book not found")

    hf_deleted_path = None
    if mode == "purge":
        try:
            api = get_hf_api()
            hf_files = api.list_repo_files(repo_id=HF_REPO_ID, repo_type="dataset")
            existing_path = find_existing_hf_path(target_book, hf_files)
            if existing_path:
                log(f"Purging {existing_path} from Hugging Face...")
                api.delete_file(
                    path_in_repo=existing_path,
                    repo_id=HF_REPO_ID,
                    repo_type="dataset",
                    commit_message=f"chore(purge): delete {existing_path}"
                )
                hf_deleted_path = existing_path
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to purge from HF: {str(e)}")

    cover_file = COVERS_DIR / f"{book_id}.webp"
    if cover_file.exists():
        cover_file.unlink()

    catalog["books"] = [b for b in catalog["books"] if b.get("id") != book_id]
    save_catalog(catalog)
    sync_git(f"chore(catalog): {mode} delete {book_id}")

    return {
        "status": "success",
        "mode": mode,
        "hf_deleted_path": hf_deleted_path
    }


# ==========================================
# API ROUTES: ANNOUNCEMENTS CAROUSEL CMS (16:6)
# ==========================================
class BannerActionModel(BaseModel):
    type: str = "NONE"  # OPEN_URL, OPEN_BOOK, FILTER_SUBJECT, FILTER_CATEGORY, OPEN_CONTRIBUTE, NONE
    target: str = ""


class BannerPayload(BaseModel):
    id: Optional[str] = None
    title: str
    subtitle: str = ""
    badge: str = ""
    imageUrl: str = ""
    imageBase64: Optional[str] = None
    bgColor: str = "#4a2511"
    textColor: str = "#ffffff"
    targetProf: str = "All"
    priority: int = 1
    active: bool = True
    action: BannerActionModel = BannerActionModel()


@app.get("/api/announcements")
def api_get_announcements():
    data = load_announcements()
    banners = data.get("banners", [])
    banners.sort(key=lambda x: (not x.get("active", True), int(x.get("priority", 99))))
    for b in banners:
        bid = b.get("id", "")
        local_img = BANNERS_DIR / f"{bid}.webp"
        b["localPreviewUrl"] = (
            f"/assets/banners/{bid}.webp?t={int(local_img.stat().st_mtime)}"
            if local_img.exists()
            else b.get("imageUrl", "")
        )
    return data


@app.post("/api/announcements")
def api_create_announcement(req: BannerPayload):
    data = load_announcements()
    banner_id = slugify(req.id or req.title) or f"banner_{int(time.time())}"
    if any(b.get("id") == banner_id for b in data["banners"]):
        banner_id = f"{banner_id}_{int(time.time()) % 10000}"

    image_url = req.imageUrl.strip()
    if req.imageBase64:
        image_url = save_banner_webp_16_6(banner_id, req.imageBase64)

    new_banner = {
        "id": banner_id,
        "title": req.title.strip(),
        "subtitle": req.subtitle.strip(),
        "badge": req.badge.strip(),
        "imageUrl": image_url,
        "bgColor": req.bgColor.strip() or "#4a2511",
        "textColor": req.textColor.strip() or "#ffffff",
        "targetProf": req.targetProf.strip() or "All",
        "priority": req.priority,
        "active": req.active,
        "action": {
            "type": req.action.type.strip().upper(),
            "target": req.action.target.strip()
        },
        "updatedAt": int(time.time() * 1000)
    }

    data["banners"].append(new_banner)
    data["banners"].sort(key=lambda x: int(x.get("priority", 99)))
    save_announcements(data)
    sync_git(f"feat(announcements): add carousel banner '{req.title.strip()}'")
    return {"status": "created", "banner": new_banner}


@app.put("/api/announcements/{banner_id}")
def api_update_announcement(banner_id: str, req: BannerPayload):
    data = load_announcements()
    target = next((b for b in data["banners"] if b.get("id") == banner_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Banner '{banner_id}' not found")

    image_url = req.imageUrl.strip() if req.imageUrl else target.get("imageUrl", "")
    if req.imageBase64:
        image_url = save_banner_webp_16_6(banner_id, req.imageBase64)

    target["title"] = req.title.strip()
    target["subtitle"] = req.subtitle.strip()
    target["badge"] = req.badge.strip()
    target["imageUrl"] = image_url
    target["bgColor"] = req.bgColor.strip() or "#4a2511"
    target["textColor"] = req.textColor.strip() or "#ffffff"
    target["targetProf"] = req.targetProf.strip() or "All"
    target["priority"] = req.priority
    target["active"] = req.active
    target["action"] = {
        "type": req.action.type.strip().upper(),
        "target": req.action.target.strip()
    }
    target["updatedAt"] = int(time.time() * 1000)

    data["banners"].sort(key=lambda x: int(x.get("priority", 99)))
    save_announcements(data)
    sync_git(f"chore(announcements): update carousel banner '{banner_id}'")
    return {"status": "updated", "banner": target}


@app.delete("/api/announcements/{banner_id}")
def api_delete_announcement(banner_id: str):
    data = load_announcements()
    before_count = len(data["banners"])
    data["banners"] = [b for b in data["banners"] if b.get("id") != banner_id]
    if len(data["banners"]) == before_count:
        raise HTTPException(status_code=404, detail=f"Banner '{banner_id}' not found")

    local_img = BANNERS_DIR / f"{banner_id}.webp"
    if local_img.exists():
        local_img.unlink()

    save_announcements(data)
    sync_git(f"chore(announcements): remove carousel banner '{banner_id}'")
    return {"status": "deleted", "id": banner_id}


# ==========================================
# API ROUTES: COMMUNITY SUBMISSIONS INBOX
# ==========================================
@app.get("/api/submissions")
def api_list_submissions():
    """Scans `submissions/<slug>/` on Hugging Face for pending student contributions."""
    try:
        api = get_hf_api()
        hf_files = api.list_repo_files(repo_id=HF_REPO_ID, repo_type="dataset")

        submission_files_map: Dict[str, List[str]] = {}
        for f in hf_files:
            if f.startswith("submissions/"):
                parts = f.split("/")
                if len(parts) >= 3:
                    slug = parts[1]
                    submission_files_map.setdefault(slug, []).append(f)

        headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
        submissions = []

        for slug, paths in submission_files_map.items():
            meta_path = f"submissions/{slug}/metadata.json"
            bin_path = next((p for p in paths if p.endswith(".bin")), None)
            cover_path = next((p for p in paths if p.endswith(".webp")), None)

            if not bin_path:
                continue

            meta: Dict[str, Any] = {
                "slug": slug,
                "title": slug.replace("_", " ").title(),
                "author": "Community Contributor",
                "edition": "Standard Edition",
                "prof": "1st Prof",
                "subject": "Anatomy",
                "type": "Handwritten Notes",
                "totalPages": 1,
                "sizeMb": 0.0,
                "uploader": "Student",
                "contributorTier": "STANDARD",
                "submittedAt": int(time.time() * 1000)
            }

            if meta_path in paths:
                try:
                    url = f"https://huggingface.co/datasets/{HF_REPO_ID}/resolve/main/{meta_path}"
                    r = requests.get(url, headers=headers, timeout=10)
                    if r.status_code == 200:
                        fetched_meta = r.json()
                        if isinstance(fetched_meta.get("metadata"), dict):
                            meta.update(fetched_meta["metadata"])
                        for k, v in fetched_meta.items():
                            if k != "metadata" and v is not None:
                                meta[k] = v
                except Exception as e:
                    print(f"Failed reading metadata for {slug}: {e}")

            if not meta.get("sizeMb") and meta.get("sizeBytes"):
                meta["sizeMb"] = round(float(meta["sizeBytes"]) / (1024 * 1024), 1)

            has_b64_cover = bool(meta.get("coverBase64"))
            has_any_cover = (cover_path is not None) or has_b64_cover

            meta["slug"] = slug
            meta["bin_hf_path"] = bin_path
            meta["has_cover"] = has_any_cover
            meta["cover_preview_url"] = f"/api/submissions/{slug}/cover" if has_any_cover else ""
            submissions.append(meta)

        submissions.sort(key=lambda x: x.get("submittedAt", 0), reverse=True)
        return {"status": "success", "count": len(submissions), "submissions": submissions}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/submissions/{slug}/cover")
def api_submission_cover(slug: str):
    """
    Serves the quarantined cover image.
    1. Checks `submissions/{slug}/metadata.json` for `coverBase64` and decodes it to image/webp.
    2. Falls back to `submissions/{slug}/{slug}.webp` if stored as a separate file.
    """
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

    meta_url = f"https://huggingface.co/datasets/{HF_REPO_ID}/resolve/main/submissions/{slug}/metadata.json"
    try:
        meta_res = requests.get(meta_url, headers=headers, timeout=10)
        if meta_res.status_code == 200:
            meta_json = meta_res.json()
            b64_str = meta_json.get("coverBase64") or (
                meta_json.get("metadata", {}).get("coverBase64")
                if isinstance(meta_json.get("metadata"), dict) else None
            )
            if b64_str:
                img_bytes = decode_base64_bytes(b64_str)
                return Response(content=img_bytes, media_type="image/webp")
    except Exception as e:
        print(f"Notice: coverBase64 decode check for {slug}: {e}")

    url = f"https://huggingface.co/datasets/{HF_REPO_ID}/resolve/main/submissions/{slug}/{slug}.webp"
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code == 200:
        return Response(content=r.content, media_type="image/webp")

    raise HTTPException(status_code=404, detail="Cover not found")


class SubmissionApproveRequest(BaseModel):
    title: str
    author: str = "Medical Faculty"
    edition: str = "Standard Edition"
    prof: str
    subject: str
    type: str
    uploader: str = "Student"
    extra_tags: str = ""
    totalPages: int = 1
    sizeMb: float = 1.0


@app.post("/api/submissions/{slug}/approve")
def api_approve_submission(slug: str, req: SubmissionApproveRequest):
    api = get_hf_api()
    hf_files = api.list_repo_files(repo_id=HF_REPO_ID, repo_type="dataset")
    sub_files = [f for f in hf_files if f.startswith(f"submissions/{slug}/")]

    bin_src = next((f for f in sub_files if f.endswith(".bin")), None)
    if not bin_src:
        raise HTTPException(status_code=404, detail=f"No .bin file found in submissions/{slug}/")

    book_id = slugify(req.title) or slug
    dest_hf_path = build_hf_path(req.prof, req.type, book_id)

    local_cover_path = COVERS_DIR / f"{book_id}.webp"
    cover_saved = False
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

    meta_src = f"submissions/{slug}/metadata.json"
    if meta_src in sub_files:
        try:
            m_url = f"https://huggingface.co/datasets/{HF_REPO_ID}/resolve/main/{meta_src}"
            m_res = requests.get(m_url, headers=headers, timeout=15)
            if m_res.status_code == 200:
                m_data = m_res.json()
                b64_str = m_data.get("coverBase64") or (
                    m_data.get("metadata", {}).get("coverBase64")
                    if isinstance(m_data.get("metadata"), dict) else None
                )
                if b64_str:
                    with open(local_cover_path, "wb") as f:
                        f.write(decode_base64_bytes(b64_str))
                    cover_saved = True
        except Exception as e:
            log(f"Warning: could not decode coverBase64 for {slug}: {e}")

    if not cover_saved:
        cover_src = next((f for f in sub_files if f.endswith(".webp")), None)
        if cover_src:
            try:
                c_url = f"https://huggingface.co/datasets/{HF_REPO_ID}/resolve/main/{cover_src}"
                r = requests.get(c_url, headers=headers, timeout=15)
                if r.status_code == 200 and len(r.content) > 0:
                    with open(local_cover_path, "wb") as f:
                        f.write(r.content)
            except Exception as e:
                log(f"Warning: could not save submission cover for {slug}: {e}")

    log(f"Approving submission '{slug}': moving {bin_src} -> {dest_hf_path}...")
    operations = [
        CommitOperationCopy(src_path_in_repo=bin_src, path_in_repo=dest_hf_path)
    ]
    for old_f in sub_files:
        operations.append(CommitOperationDelete(path_in_repo=old_f))

    try:
        api.create_commit(
            repo_id=HF_REPO_ID,
            repo_type="dataset",
            operations=operations,
            commit_message=f"feat(approve): publish community contribution {dest_hf_path} (by {req.uploader})"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"HF Commit Failed: {str(e)}")

    catalog = load_catalog()
    if req.type not in catalog["filters"]["types"]:
        catalog["filters"]["types"].append(req.type)

    worker_download_url = build_worker_url(dest_hf_path)
    cover_cdn_url = f"https://cdn.jsdelivr.net/gh/{CATALOG_REPO}@main/assets/covers/{book_id}.webp"
    tags = build_tags(req.title, req.author, req.subject, req.prof, req.type, req.extra_tags)

    record = {
        "id": book_id,
        "title": req.title.strip(),
        "author": req.author.strip(),
        "edition": req.edition.strip(),
        "prof": req.prof.strip(),
        "year": PROF_TO_YEAR.get(req.prof.strip(), "1st Year"),
        "subject": req.subject.strip(),
        "type": req.type.strip(),
        "tags": tags,
        "totalPages": max(1, req.totalPages),
        "sizeMb": round(req.sizeMb, 1),
        "coverUrl": cover_cdn_url,
        "download": {
            "mirrors": [
                {
                    "provider": "mulberry-vault",
                    "repo": HF_REPO_ID,
                    "url": worker_download_url,
                    "isEncrypted": True,
                    "format": "aes-256-gcm"
                }
            ],
            "telegram": {
                "deepLink": f"https://t.me/MulberryIngestionBot?start={book_id}"
            }
        },
        "uploader": req.uploader.strip() or "Community"
    }

    catalog["books"] = [b for b in catalog["books"] if b.get("id") != book_id]
    catalog["books"].insert(0, record)
    save_catalog(catalog)

    sync_git(f"feat(community): approve '{req.title}' contributed by {req.uploader}")
    log(f"PUBLISHED COMMUNITY SUBMISSION: {req.title} -> {dest_hf_path}")

    return {"status": "approved", "book": record, "hf_path": dest_hf_path}


@app.delete("/api/submissions/{slug}")
def api_reject_submission(slug: str):
    """Deletes all files inside `submissions/<slug>/` on Hugging Face."""
    api = get_hf_api()
    hf_files = api.list_repo_files(repo_id=HF_REPO_ID, repo_type="dataset")
    sub_files = [f for f in hf_files if f.startswith(f"submissions/{slug}/")]

    if not sub_files:
        raise HTTPException(status_code=404, detail="Submission folder not found on HF")

    operations = [CommitOperationDelete(path_in_repo=f) for f in sub_files]
    try:
        api.create_commit(
            repo_id=HF_REPO_ID,
            repo_type="dataset",
            operations=operations,
            commit_message=f"chore(reject): purge rejected submission {slug}"
        )
        log(f"Purged rejected submission: submissions/{slug}/")
        return {"status": "rejected", "deleted_files": sub_files}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"HF Purge Failed: {str(e)}")


# ==========================================
# API ROUTES: HF STORAGE & ORPHANS
# ==========================================
@app.get("/api/hf-status")
def api_hf_status():
    """Fetches real-time Hugging Face file list, folder structure, and orphan status."""
    try:
        api = get_hf_api()
        tree = list(api.list_repo_tree(repo_id=HF_REPO_ID, repo_type="dataset", recursive=True))
        catalog = load_catalog()

        total_bytes = 0
        files_list = []
        for item in tree:
            size = getattr(item, "size", None)
            if size is not None and item.path.endswith(".bin"):
                total_bytes += size
                files_list.append({
                    "path": item.path,
                    "size_mb": round(size / (1024 * 1024), 1)
                })

        catalog_ids = {f"{b['id']}.bin" for b in catalog.get("books", [])}
        orphans = [
            f for f in files_list
            if not f["path"].startswith("submissions/") and f["path"].split("/")[-1] not in catalog_ids
        ]

        return {
            "status": "success",
            "repo_id": HF_REPO_ID,
            "total_files": len(files_list),
            "total_gb": round(total_bytes / (1024 ** 3), 2),
            "quota_gb": 100,
            "files": files_list,
            "orphans": orphans
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ==========================================
# API ROUTES: INGESTION
# ==========================================
class ScanFolderRequest(BaseModel):
    folder_url: Optional[str] = ""
    direct_links: Optional[str] = ""
    direct_pdf_links: Optional[str] = ""


@app.post("/api/scan-folder")
def scan_folder(req: ScanFolderRequest):
    files = []
    if req.folder_url and req.folder_url.strip():
        files.extend(fetch_public_drive_folder(req.folder_url.strip()))
    if req.direct_links and req.direct_links.strip():
        files.extend(resolve_direct_drive_links(req.direct_links.strip()))
    if req.direct_pdf_links and req.direct_pdf_links.strip():
        files.extend(resolve_direct_pdf_links(req.direct_pdf_links.strip()))

    if not files:
        return {"status": "empty", "files": []}

    catalog = load_catalog()
    existing_ids = {b.get("id") for b in catalog.get("books", [])}

    results = []
    for f in files:
        heuristics = parse_filename_heuristics(f["name"])
        candidate_id = slugify(heuristics["title"])
        results.append({
            "source_id": f["id"],
            "source_type": f.get("source_type", "drive"),
            "filename": f["name"],
            "already_in_catalog": candidate_id in existing_ids,
            "heuristics": heuristics
        })
    return {"status": "success", "files": results}


class IngestBatchRequest(BaseModel):
    books: List[Dict[str, Any]]


@app.post("/api/start-ingestion")
def start_ingest(req: IngestBatchRequest, background_tasks: BackgroundTasks):
    global job_logs
    if is_processing:
        return JSONResponse({"status": "busy", "message": "A job is already running!"}, status_code=400)
    job_logs = []
    background_tasks.add_task(run_batch_ingestion, req.books)
    return {"status": "started", "queued_count": len(req.books)}


@app.get("/api/logs")
def get_logs():
    return {"logs": job_logs, "is_processing": is_processing}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("portal:app", host="0.0.0.0", port=8000, reload=True)