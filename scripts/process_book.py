import os
import sys
import json
import re
import io
import time
import requests
from PIL import Image
import pymupdf
import gdown
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VAULT_ROUTING = {
    # Prof 1
    "Anatomy": "mulberry-vault-prof1",
    "Physiology": "mulberry-vault-prof1",
    "Biochemistry": "mulberry-vault-prof1",
    # Prof 2
    "Pathology": "mulberry-vault-prof2",
    "Pharmacology": "mulberry-vault-prof2",
    "Microbiology": "mulberry-vault-prof2",
    "Forensic Medicine": "mulberry-vault-prof2",
    # Prof 3 Part 1
    "Community Medicine": "mulberry-vault-prof3",
    "Ophthalmology": "mulberry-vault-prof3",
    "ENT": "mulberry-vault-prof3",
    # Final Prof
    "General Medicine": "mulberry-vault-prof4",
    "General Surgery": "mulberry-vault-prof4",
    "Obstetrics & Gynaecology": "mulberry-vault-prof4",
    "Paediatrics": "mulberry-vault-prof4",
    "Orthopaedics": "mulberry-vault-prof4",
    "Dermatology": "mulberry-vault-prof4",
    "Psychiatry": "mulberry-vault-prof4",
    "Radiology": "mulberry-vault-prof4",
    "Anaesthesiology": "mulberry-vault-prof4"
}

PROF_TO_YEAR = {
    "1st Prof": "1st Year",
    "2nd Prof": "2nd Year",
    "3rd Prof Part 1": "3rd Year",
    "Final Prof": "Final Year"
}

def slugify(text):
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '_', text)

def extract_drive_id(url):
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

def build_tag_array(title, author, subject, prof, book_type, extra_tags):
    tags = set()
    tags.add(prof.lower())
    tags.add(PROF_TO_YEAR.get(prof, "").lower())
    tags.add(subject.lower())
    tags.add(book_type.lower())
    
    for word in re.findall(r'\b\w{3,}\b', title.lower()):
        tags.add(word)
    for word in re.findall(r'\b\w{3,}\b', author.lower()):
        if word not in ["faculty", "medical", "books"]:
            tags.add(word)
            
    if extra_tags:
        for t in extra_tags.split(","):
            cleaned = t.strip().lower()
            if cleaned:
                tags.add(cleaned)
                
    return sorted(list(filter(None, tags)))

def encrypt_file(input_path, output_path, hex_key):
    """Encrypts raw PDF using AES-256-GCM. Prepend 12-byte Nonce to ciphertext."""
    key = bytes.fromhex(hex_key)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit initialization vector
    
    with open(input_path, "rb") as f:
        plaintext = f.read()
        
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    
    with open(output_path, "wb") as f:
        f.write(nonce + ciphertext)

def main():
    raw_drive_url = os.environ.get("DRIVE_URL", "").strip()
    title = os.environ.get("BOOK_TITLE", "").strip()
    author = os.environ.get("BOOK_AUTHOR", "Medical Faculty").strip()
    edition = os.environ.get("BOOK_EDITION", "Standard Edition").strip()
    subject = os.environ.get("BOOK_SUBJECT", "Anatomy").strip()
    prof = os.environ.get("BOOK_PROF", "1st Prof").strip()
    book_type = os.environ.get("BOOK_TYPE", "Standard Textbook").strip()
    extra_tags = os.environ.get("EXTRA_TAGS", "").strip()
    uploader = os.environ.get("UPLOADER_NAME", "Sahabul").strip()
    
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    vault_key = os.environ.get("VAULT_ENCRYPTION_KEY", "").strip()
    catalog_repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner = catalog_repo.split("/")[0] if "/" in catalog_repo else "user"

    if not raw_drive_url or not title:
        print("[-] Error: DRIVE_URL and BOOK_TITLE are required.")
        sys.exit(1)

    if not vault_key or len(vault_key) != 64:
        print("[-] Error: VAULT_ENCRYPTION_KEY must be a valid 64-char hex string.")
        sys.exit(1)

    book_id = slugify(title)
    pdf_filename = f"{book_id}.pdf"
    bin_filename = f"{book_id}.bin"
    cover_filename = f"{book_id}.webp"
    cover_rel_path = f"assets/covers/{cover_filename}"

    vault_repo_name = VAULT_ROUTING.get(subject, "mulberry-vault-prof1")
    target_vault_repo = f"{owner}/{vault_repo_name}"

    # 1. Download raw PDF
    file_id = extract_drive_id(raw_drive_url)
    print(f"[*] Downloading '{title}' (Drive ID: {file_id})...")
    
    download_success = False
    try:
        output = gdown.download(id=file_id, output=pdf_filename, quiet=False)
        if output and os.path.exists(pdf_filename):
            download_success = True
    except Exception as e:
        print(f"[-] gdown ID download error: {e}")

    if not download_success:
        try:
            print("[*] Retrying with direct Drive URL...")
            gdown.download(url=raw_drive_url, output=pdf_filename, quiet=False)
        except Exception as e:
            print(f"[-] gdown URL download error: {e}")

    if not os.path.exists(pdf_filename) or os.path.getsize(pdf_filename) == 0:
        print("[-] Download failed. Check sharing settings.")
        sys.exit(1)

    file_size_bytes = os.path.getsize(pdf_filename)
    file_size_mb = round(file_size_bytes / (1024 * 1024), 1)

    # 2. Extract WebP cover before encryption
    print("[*] Extracting Page 1 cover...")
    os.makedirs("assets/covers", exist_ok=True)
    doc = pymupdf.open(pdf_filename)
    total_pages = len(doc)
    page_1 = doc.load_page(0)
    pix = page_1.get_pixmap(dpi=150)
    if pix.colorspace and pix.colorspace.name == "DeviceCMYK":
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    img_data = pix.tobytes("png")
    doc.close()
    
    img = Image.open(io.BytesIO(img_data)).convert("RGB")
    img.save(cover_rel_path, format="WEBP", quality=80)

    # 3. Encrypt PDF to .bin payload
    print("[*] Encrypting PDF payload with AES-256-GCM...")
    encrypt_file(pdf_filename, bin_filename, vault_key)
    print(f"[+] Created encrypted asset: {bin_filename}")

    # 4. URLs
    github_release_url = f"https://github.com/{target_vault_repo}/releases/download/v1-books/{bin_filename}"
    direct_drive_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    cover_cdn_url = f"https://cdn.jsdelivr.net/gh/{catalog_repo}@main/{cover_rel_path}"
    telegram_deeplink = f"https://t.me/MulberryIngestionBot?start={book_id}"

    # 5. Telegram backup (posts unencrypted file to private archive)
    if bot_token and channel_id:
        print("[*] Updating private Telegram vault...")
        caption = (
            f"📚 <b>{title}</b> ({edition})\n"
            f"👤 Author: {author}\n"
            f"🏥 Prof: {prof} | {subject}\n"
            f"📑 Type: {book_type}\n"
            f"📄 Pages: {total_pages} ({file_size_mb} MB)\n"
            f"✨ Added by: {uploader}\n\n"
            f"⬇️ <a href=\"{github_release_url}\">Encrypted Release Asset (.bin)</a>"
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
            print(f"[!] Telegram alert notice: {e}")

    # 6. Update catalog.json
    print("[*] Updating catalog.json...")
    catalog_path = "catalog.json"
    catalog = {"version": 2, "lastUpdated": 0, "books": []}
    
    if os.path.exists(catalog_path):
        try:
            with open(catalog_path, "r", encoding="utf-8") as f:
                catalog = json.load(f)
        except Exception:
            pass

    tags_list = build_tag_array(title, author, subject, prof, book_type, extra_tags)
    catalog["books"] = [b for b in catalog.get("books", []) if b.get("id") != book_id]
    
    new_book_record = {
        "id": book_id,
        "title": title,
        "author": author,
        "edition": edition,
        "prof": prof,
        "year": PROF_TO_YEAR.get(prof, "1st Year"),
        "subject": subject,
        "type": book_type,
        "tags": tags_list,
        "totalPages": total_pages,
        "sizeMb": file_size_mb,
        "coverUrl": cover_cdn_url,
        "download": {
            "mirrors": [
                {
                    "provider": "github",
                    "repo": vault_repo_name,
                    "url": github_release_url,
                    "isEncrypted": True,
                    "format": "aes-256-gcm"
                },
                {
                    "provider": "drive",
                    "url": direct_drive_url,
                    "isEncrypted": False
                }
            ],
            "telegram": {
                "deepLink": telegram_deeplink
            }
        },
        "uploader": uploader
    }

    catalog["books"].insert(0, new_book_record)
    catalog["lastUpdated"] = int(time.time() * 1000)

    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    with open("target_vault.txt", "w") as f:
        f.write(target_vault_repo)

    print(f"[✓] Processed and encrypted: '{title}' -> Released as {bin_filename}")

if __name__ == "__main__":
    main()
