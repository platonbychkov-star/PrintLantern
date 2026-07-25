from __future__ import annotations

import base64
import cgi
import hashlib
import hmac
import html
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PRINTLANTERN_DATA_DIR") or (APP_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
FINAL_DIR = DATA_DIR / "prepared"
CONFIG_PATH = DATA_DIR / "config.json"
FIRST_LOGIN_PATH = DATA_DIR / "first_login.txt"
JOBS_PATH = DATA_DIR / "jobs.json"
SESSIONS_PATH = DATA_DIR / "sessions.json"

APP_NAME = "PrintLantern"
SESSION_COOKIE = "print_portal_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
SHORT_SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_LOGIN_FAILURES = 8
LOGIN_WINDOW_SECONDS = 15 * 60

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8088,
    "auth_enabled": False,
    "admin_user": "platon_admin",
    "max_upload_mb": 80,
    "allowed_extensions": [
        ".pdf",
        ".txt",
        ".rtf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".jpg",
        ".jpeg",
        ".png",
        ".bmp",
        ".gif",
        ".webp",
        ".tif",
        ".tiff",
        ".csv",
        ".md",
        ".json",
        ".xml",
        ".html",
        ".htm",
        ".odt",
        ".ods",
        ".odp",
        ".pages",
        ".numbers",
        ".key",
        ".heic",
        ".heif",
        ".svg",
    ],
    "printer_name": "",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
TEXT_EXTENSIONS = {".txt", ".csv", ".md", ".json", ".xml", ".html", ".htm"}
DOCUMENT_TEXT_EXTENSIONS = {".docx", ".rtf", ".odt", ".pdf"}
NATIVE_PRINT_EXTENSIONS = {
    ".doc",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".ods",
    ".odp",
    ".pages",
    ".numbers",
    ".key",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".svg",
}

BANNED_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".com",
    ".exe",
    ".hta",
    ".js",
    ".jse",
    ".lnk",
    ".msi",
    ".ps1",
    ".scr",
    ".vbe",
    ".vbs",
    ".wsf",
}

jobs_lock = threading.RLock()
jobs: dict[str, dict] = {}
print_queue: queue.Queue[str] = queue.Queue()
sessions_lock = threading.RLock()
sessions: dict[str, dict] = {}
login_failures: dict[str, list[float]] = {}


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_DIR.mkdir(parents=True, exist_ok=True)


def hash_password(password: str, salt: bytes | None = None, iterations: int = 260_000) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${iterations}${salt_b64}${digest_b64}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations_raw, salt_b64, expected_b64 = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        iterations = int(iterations_raw)
        candidate = hash_password(password, salt=salt, iterations=iterations)
        return hmac.compare_digest(candidate, encoded)
    except Exception:
        return False


def load_config() -> dict:
    ensure_dirs()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as fh:
            config = json.load(fh)
        merged = DEFAULT_CONFIG | config
        merged["allowed_extensions"] = sorted(
            {ext.lower() for ext in DEFAULT_CONFIG["allowed_extensions"]}
            | {ext.lower() for ext in config.get("allowed_extensions", [])}
        )
        return merged

    password = "Print-" + secrets.token_urlsafe(12).replace("_", "9").replace("-", "7")
    config = DEFAULT_CONFIG | {"admin_password_hash": hash_password(password)}
    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
    with FIRST_LOGIN_PATH.open("w", encoding="utf-8") as fh:
        fh.write(
            "PrintLantern first login\n"
            f"URL: http://127.0.0.1:{config['port']}\n"
            f"Login: {config['admin_user']}\n"
            f"Password: {password}\n\n"
            "After logging in, keep this file private or delete it.\n"
        )
    print("Created admin account for PrintLantern.")
    print(f"Login: {config['admin_user']}")
    print(f"Password: {password}")
    print(f"Saved first login details to: {FIRST_LOGIN_PATH}")
    return config


config = load_config()
if os.environ.get("PRINTLANTERN_HOST"):
    config["host"] = os.environ["PRINTLANTERN_HOST"]
if os.environ.get("PRINTLANTERN_PORT"):
    try:
        config["port"] = int(os.environ["PRINTLANTERN_PORT"])
    except ValueError:
        pass
if os.environ.get("PRINTLANTERN_PRINTER_NAME") is not None:
    config["printer_name"] = os.environ["PRINTLANTERN_PRINTER_NAME"].strip()
config["require_desktop_approval"] = (
    os.environ.get("PRINTLANTERN_REQUIRE_DESKTOP_APPROVAL", "0") == "1"
)
DESKTOP_API_TOKEN = os.environ.get("PRINTLANTERN_DESKTOP_TOKEN", "")
TEST_NO_PRINT = os.environ.get("PRINTLANTERN_TEST_NO_PRINT", "0") == "1"


def load_jobs() -> None:
    global jobs
    if not JOBS_PATH.exists():
        jobs = {}
        return
    with JOBS_PATH.open("r", encoding="utf-8-sig") as fh:
        loaded = json.load(fh)
    if not isinstance(loaded, dict):
        jobs = {}
        return
    now = utc_now()
    for job in loaded.values():
        if job.get("status") in {"queued", "printing"}:
            job["status"] = "interrupted"
            job["progress"] = 0
            job["message"] = "Сервер был перезапущен до завершения печати"
            job["updated_at"] = now
    jobs = loaded
    save_jobs()


def save_jobs() -> None:
    tmp = JOBS_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(jobs, fh, indent=2, ensure_ascii=False)
    tmp.replace(JOBS_PATH)


def load_sessions() -> None:
    global sessions
    if not SESSIONS_PATH.exists():
        sessions = {}
        return
    try:
        with SESSIONS_PATH.open("r", encoding="utf-8-sig") as fh:
            loaded = json.load(fh)
    except (OSError, json.JSONDecodeError):
        sessions = {}
        return
    if not isinstance(loaded, dict):
        sessions = {}
        return
    now = time.time()
    sessions = {
        sid: data
        for sid, data in loaded.items()
        if isinstance(data, dict) and float(data.get("expires", 0)) > now
    }
    save_sessions()


def save_sessions() -> None:
    tmp = SESSIONS_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(sessions, fh, indent=2, ensure_ascii=False)
    tmp.replace(SESSIONS_PATH)


def get_max_upload_bytes() -> int:
    return int(config.get("max_upload_mb", 80)) * 1024 * 1024


def sanitize_filename(name: str) -> str:
    base = Path(name).name.strip().replace("\x00", "")
    allowed = []
    for char in base:
        if char.isalnum() or char in " ._-()[]":
            allowed.append(char)
        else:
            allowed.append("_")
    cleaned = "".join(allowed).strip(" .")
    return cleaned[:120] or "upload"


def extension_allowed(filename: str) -> tuple[bool, str]:
    ext = Path(filename).suffix.lower()
    allowed = {x.lower() for x in config.get("allowed_extensions", [])}
    if not ext or ext in BANNED_EXTENSIONS:
        return False, ext
    return ext in allowed, ext


def preview_mode_for_extension(ext: str) -> str:
    ext = ext.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext == ".pdf":
        return "pdf_pages"
    if ext in TEXT_EXTENSIONS or ext in DOCUMENT_TEXT_EXTENSIONS:
        return "text"
    return "native"


def public_job(job: dict) -> dict:
    item = dict(job)
    item["message"] = friendly_status_message(str(job.get("message", "")), str(job.get("status", "")))
    item["prepared_count"] = len(job.get("prepared_paths", []) or [])
    item.pop("file_path", None)
    item.pop("prepared_paths", None)
    return item


def friendly_status_message(message: str, status: str = "") -> str:
    technical_bits = [
        "Command '['",
        "Start-Process",
        "returned non-zero exit status",
        "Traceback",
        "powershell.exe",
        "subprocess",
    ]
    if status == "failed" and any(bit in message for bit in technical_bits):
        return (
            "Windows не смогла отправить файл в печать. Для PDF/DOCX нужна установленная программа, "
            "которая умеет печатать этот формат. Для картинок и текста попробуй новый A4-предпросмотр."
        )
    return message[:500]


def read_text_file(path: Path, max_chars: int = 250_000) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return read_docx_text(path, max_chars)
    if ext == ".odt":
        return read_odt_text(path, max_chars)
    if ext == ".rtf":
        return read_rtf_text(path, max_chars)
    if ext == ".pdf":
        return read_pdf_text(path, max_chars)
    raw = path.read_bytes()[: max_chars * 4]
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_docx_text(path: Path, max_chars: int = 250_000) -> str:
    try:
        with zipfile.ZipFile(path) as docx:
            xml_data = docx.read("word/document.xml")
    except Exception as exc:
        raise RuntimeError(f"Не удалось прочитать DOCX: {exc}") from exc
    root = ElementTree.fromstring(xml_data)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    result = "\n".join(paragraphs).strip()
    return result[:max_chars] or "В DOCX не найден текст для печати."


def read_odt_text(path: Path, max_chars: int = 250_000) -> str:
    try:
        with zipfile.ZipFile(path) as odt:
            xml_data = odt.read("content.xml")
    except Exception as exc:
        raise RuntimeError(f"Не удалось прочитать ODT: {exc}") from exc
    root = ElementTree.fromstring(xml_data)
    paragraphs: list[str] = []
    for element in root.iter():
        if element.tag.endswith("}p") or element.tag.endswith("}h"):
            text = "".join(element.itertext()).strip()
            if text:
                paragraphs.append(text)
    result = "\n".join(paragraphs).strip()
    return result[:max_chars] or "В ODT не найден текст для печати."


def read_rtf_text(path: Path, max_chars: int = 250_000) -> str:
    raw = path.read_bytes()[: max_chars * 6]
    text = raw.decode("cp1251", errors="replace")
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1251", errors="replace"), text)
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), text)
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\tab", "\t", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines()).strip()
    return text[:max_chars] or "В RTF не найден текст для печати."


def read_pdf_text(path: Path, max_chars: int = 250_000) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError("Для PDF не установлена библиотека pypdf") from exc
    try:
        reader = PdfReader(str(path))
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                pages.append(f"Страница {index}\n{page_text}")
            if sum(len(item) for item in pages) >= max_chars:
                break
    except Exception as exc:
        raise RuntimeError(f"Не удалось прочитать PDF: {exc}") from exc
    result = "\n\n".join(pages).strip()
    return result[:max_chars] or "В PDF не найден текст. Возможно, это скан без распознавания."


def render_pdf_pages(path: Path, job_id: str, max_pages: int = 50) -> list[str]:
    try:
        import fitz
    except Exception as exc:
        raise RuntimeError("Для PDF-предпросмотра не установлена библиотека PyMuPDF") from exc

    output_dir = FINAL_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_page in output_dir.glob("page-*.png"):
        old_page.unlink(missing_ok=True)

    try:
        document = fitz.open(str(path))
    except Exception as exc:
        raise RuntimeError(f"Не удалось открыть PDF: {exc}") from exc

    page_paths: list[str] = []
    try:
        if document.page_count > max_pages:
            raise RuntimeError(f"PDF слишком большой: {document.page_count} страниц. Максимум: {max_pages}.")
        matrix = fitz.Matrix(2.0, 2.0)
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            page_path = output_dir / f"page-{page_k�<�h��춻�q�^uwait printResponse.json();

    if (requireApproval) {
      assert.equal(submitted.job.status, "pending_approval");

      const forbidden = await fetch(
        `http://127.0.0.1:${port}/api/desktop/approve`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-PrintLantern-Desktop-Token": "wrong-token"
          },
          body: JSON.stringify({ job_id: uploaded.job.id })
        }
      );
      assert.equal(forbidden.status, 403);

      const rejected = await fetch(
        `http://127.0.0.1:${port}/api/desktop/reject`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-PrintLantern-Desktop-Token": token
          },
          body: JSON.stringify({ job_id: uploaded.job.id })
        }
      );
      assert.equal(rejected.status, 200);
      assert.equal((await rejected.json()).job.status, "cancelled");
      return;
    }

    assert.ok(["queued", "printing", "completed"].includes(submitted.job.status));
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      const jobs = await (
        await fetch(`http://127.0.0.1:${port}/api/jobs`)
      ).json();
      const job = jobs.jobs.find((item) => item.id === uploaded.job.id);
      if (job?.status === "completed") return;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error("Direct-print test job did not complete");
  } finally {
    child.kill();
  }
}

await runScenario({ port: 4881, requireApproval: true });
await runScenario({ port: 4882, requireApproval: false });
console.log("Backend approval and direct-print modes passed.");
