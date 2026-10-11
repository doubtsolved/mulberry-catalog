import os
import sys
import json
import re
import fitz  # PyMuPDF
import gdown
import requests
from PIL import Image

def slugify(text):
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '_', text)

def extract_drive_id(url):
    """Extract clean Google Drive file ID from any URL format."""
    patterns = [
        r'/file/d/([a-zA-Z0-9_-]+)',
        r'id=([a-zA-Z0-9_-]+)',
        r'/open\?id=([a-zA-Z0-9_-]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return url

def main():
    raw_drive_url = os.environ.get("DRIVE_URL", "").strip()
    title = os.environ.get("BOOK_TITLE", "").strip()
    author = os.environ.get("BOOK_AUTHOR", "Medical Faculty").strip()
    category = os.environ.get("BOOK_CATEGORY", "Anatomy").strip()
    uploader = os.environ.get("UPLOADER_NAME", "Sahabul").strip()
    
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not raw_drive_url or not title:
        print("[-] Error: DRIVE_URL and BOOK_TITLE are required.")
        sys.exit(1)

    book_id = slugify(title)
    pdf_filename = f"{book_id}.pdf"
    cover_filename = f"{book_id}.webp"
    cover_rel_path = f"assets/covers/{cover_filename}"

    # 1. Download PDF
    file_id = extract_drive_id(raw_drive_url)
    clean_drive_url = f"https://drive.google.com/uc?id={file_id}"
    print(f"[*] Downloading '{title}' (ID: {file_id})...")
    
    try:
        gdown.download(clean_drive_url, pdf_filename, quiet=False, fuzzy=True)
    except Exception as e:
        print(f"[-] gdown encountered an error: {e}")

    if not os.path.exists(pdf_filename) or os.path.getsize(pdf_filename) == 0:
        print("[-] Download failed. Ensure Google Drive sharing is set to 'Anyone with the link'.")
        sys.exit(1)

    file_size_bytes = os.path.getsize(pdf_filename)
    file_size_mb = round(file_size_bytes / (1024 * 1024), 1)
    print(f"[+] Download complete: {file_size_mb} MB")

    # 2. Extract Cover (Safely handling RGB/CMYK)
    print("[*] Extracting cover image (Page 1)...")
    os.makedirs("assets/covers", exist_ok=True)
    
    doc = fitz.open(pdf_filename)
    total_pages = len(doc)
    page_1 = doc.load_page(0)
    
    pix = page_1.get_pixmap(dpi=150)
    # Convert CMYK to RGB if necessary to avoid WebP conversion errors
    if pix.colorspace and pix.colorspace.name == "DeviceCMYK":
        pix = fitz.Pixmap(fitz.csRGB, pix)
    
    # Save pixmap as PNG in memory, then export as compressed WebP
    img_data = pix.tobytes("png")
    doc.close()
    
    import io
    img = Image.open(io.BytesIO(img_data)).convert("RGB")
    img.save(cover_rel_path, format="WEBP", quality=80)
    print(f"[+] Cover saved to {cover_rel_path} ({total_pages} total pages)")

    # 3. Direct Download Link via GitHub Releases
    download_url = f"https://github/{repo}/releases/download/v1-books/{pdf_filename}"
    cover_cdn_url = f"https://raw.githubusercontent.com/{repo}/main/{cover_rel_path}"

    # 4. Telegram Notification
    if bot_token and channel_id:
        print("[*] Sending to Telegram channel...")
        caption = (
            f"📚 <b>{title}</b>\n"
            f"👤 Author: {author}\n"
            f"🏷️ Category: {category}\n"
            f"📄 Pages: {total_pages} ({file_size_mb} MB)\n"
            f"✨ Added by: {uploader}\n\n"
            f"⬇️ <a href=\"{download_url}\">Direct PDF Download</a>"
        )
        try:
            if file_size_mb <= 49.0:
                with open(pdf_filename, "rb") as f:
                    requests.post(
                        f"https://api.telegram.org/bot{bot_token}/sendDocument",
                        data={"chat_id": channel_id, "caption": caption, "parse_mode": "HTML"},
                        files={"document": f},
                        timeout=60
                    )
            else:
                with open(cover_rel_path, "rb") as f:
                    requests.post(
                        f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                        data={"chat_id": channel_id, "caption": caption, "parse_mode": "HTML"},
                        files={"photo": f},
                        timeout=30
                    )
        except Exception as e:
            print(f"[!] Telegram alert skipped due to network notice: {e}")

    # 5. Update catalog.json
    print("[*] Updating catalog.json...")
    catalog_path = "catalog.json"
    catalog = {"version": 1, "categories": ["All", "Anatomy", "Physiology", "Biochemistry", "Pathology", "Pharmacology", "Handwritten Notes"], "books": []}
    
    if os.path.exists(catalog_path):
        try:
            with open(catalog_path, "r", encoding="utf-8") as f:
                catalog = json.load(f)
        except Exception:
            pass

    catalog["books"] = [b for b in catalog.get("books", []) if b.get("id") != book_id]
    new_entry = {
        "id": book_id,
        "title": title,
        "author": author,
        "category": category,
        "totalPages": total_pages,
        "sizeMb": file_size_mb,
        "coverUrl": cover_cdn_url,
        "downloadUrl": download_url,
        "uploader": uploader
    }
    catalog["books"].insert(0, new_entry)
    
    import time
    catalog["lastUpdated"] = int(time.time() * 1000)

    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    print(f"[✓] Successfully cataloged: '{title}'")

if __name__ == "__main__":
    main()
