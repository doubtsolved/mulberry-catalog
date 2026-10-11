import os
import sys
import json
import re
import fitz  # PyMuPDF
import gdown
import requests

def slugify(text):
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '_', text)

def main():
    drive_url = os.environ.get("DRIVE_URL", "").strip()
    title = os.environ.get("BOOK_TITLE", "").strip()
    author = os.environ.get("BOOK_AUTHOR", "Medical Faculty").strip()
    category = os.environ.get("BOOK_CATEGORY", "Anatomy").strip()
    uploader = os.environ.get("UPLOADER_NAME", "Sahabul").strip()
    
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not drive_url or not title:
        print("Error: DRIVE_URL and BOOK_TITLE are required.")
        sys.exit(1)

    book_id = slugify(title)
    pdf_filename = f"{book_id}.pdf"
    cover_filename = f"{book_id}.webp"
    cover_rel_path = f"assets/covers/{cover_filename}"

    # 1. Download the PDF from Google Drive
    print(f"[*] Downloading '{title}' from Google Drive...")
    gdown.download(drive_url, pdf_filename, quiet=False, fuzzy=True)

    if not os.path.exists(pdf_filename):
        print("[-] Download failed. Check the Google Drive link and sharing settings.")
        sys.exit(1)

    file_size_bytes = os.path.getsize(pdf_filename)
    file_size_mb = round(file_size_bytes / (1024 * 1024), 1)
    print(f"[+] Download complete: {file_size_mb} MB")

    # 2. Extract Page 1 as WebP Cover Image
    print("[*] Extracting cover image (Page 1)...")
    doc = fitz.open(pdf_filename)
    total_pages = len(doc)

    os.makedirs("assets/covers", exist_ok=True)
    page_1 = doc.load_page(0)
    pix = page_1.get_pixmap(dpi=150)
    pix.pil_save(cover_rel_path, format="WEBP", quality=80)
    doc.close()
    print(f"[+] Cover saved to {cover_rel_path} ({total_pages} total pages)")

    # 3. Direct Download Link via GitHub Releases
    download_url = f"https://github.com/{repo}/releases/download/v1-books/{pdf_filename}"
    cover_cdn_url = f"https://raw.githubusercontent/{repo}/main/{cover_rel_path}"

    # 4. Notify / Upload to Telegram Channel
    if bot_token and channel_id:
        print("[*] Sending update to Telegram channel...")
        caption = (
            f"📚 <b>{title}</b>\n"
            f"👤 Author: {author}\n"
            f"🏷️ Category: {category}\n"
            f"📄 Pages: {total_pages} ({file_size_mb} MB)\n"
            f"✨ Added by: {uploader}\n\n"
            f"⬇️ <a href=\"{download_url}\">Direct PDF Download</a>"
        )

        # If file is under 50MB, upload the actual PDF to Telegram
        if file_size_mb <= 49.0:
            print("[*] File is <= 50MB, uploading document directly to Telegram...")
            with open(pdf_filename, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendDocument",
                    data={"chat_id": channel_id, "caption": caption, "parse_mode": "HTML"},
                    files={"document": f}
                )
        else:
            # File is > 50MB: upload the cover photo and provide the direct download link
            print("[*] File is > 50MB, posting cover photo + download link to Telegram...")
            with open(cover_rel_path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                    data={"chat_id": channel_id, "caption": caption, "parse_mode": "HTML"},
                    files={"photo": f}
                )

    # 5. Update catalog.json
    print("[*] Updating catalog.json...")
    catalog_path = "catalog.json"
    if os.path.exists(catalog_path):
        with open(catalog_path, "r", encoding="utf-8") as f:
            catalog = json.load(f)
    else:
        catalog = {"version": 1, "categories": ["All", category], "books": []}

    # Remove existing entry if re-processing the same book
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
