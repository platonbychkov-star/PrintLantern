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
            page_path = output_dir / f"page-{page_index + 1:03d}.png"
            pixmap.save(str(page_path))
            page_paths.append(str(page_path))
    finally:
        document.close()
    if not page_paths:
        raise RuntimeError("В PDF не найдено страниц для предпросмотра.")
    return page_paths


def select_prepared_pages(paths: list[str], page_numbers: object) -> list[str]:
    if not paths:
        return []
    if not isinstance(page_numbers, list) or not page_numbers:
        return paths
    selected: list[str] = []
    seen: set[int] = set()
    total = len(paths)
    for raw in page_numbers:
        try:
            page_number = int(raw)
        except (TypeError, ValueError):
            raise ValueError("Некорректный номер страницы")
        if page_number < 1 or page_number > total:
            raise ValueError(f"Страница {page_number} вне диапазона 1-{total}")
        if page_number not in seen:
            selected.append(paths[page_number - 1])
            seen.add(page_number)
    return selected


def get_target_printer_name() -> str:
    configured = str(config.get("printer_name") or "").strip()
    if configured:
        return configured
    if os.name != "nt":
        return ""
    script = (
        "$p = Get-CimInstance Win32_Printer | "
        "Where-Object { $_.Default -eq $true } | Select-Object -First 1 -ExpandProperty Name; "
        "if (-not $p) { $p = Get-CimInstance Win32_Printer | "
        "Where-Object { $_.Name -notlike '*PDF*' -and $_.Name -notlike '*XPS*' } | "
        "Select-Object -First 1 -ExpandProperty Name }; "
        "Write-Output $p"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def update_job(job_id: str, **changes: object) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(changes)
        job["updated_at"] = utc_now()
        save_jobs()


def run_native_print_command(file_path: Path, copies: int = 1) -> subprocess.CompletedProcess:
    if os.name != "nt":
        raise RuntimeError("Печать поддержана только на Windows в этой версии")

    script = r"""
$ErrorActionPreference = 'Stop'
$file = [System.IO.Path]::GetFullPath($args[0])
if (-not (Test-Path -LiteralPath $file)) {
    throw "File not found: $file"
}
$process = Start-Process -FilePath $file -Verb Print -PassThru -WindowStyle Hidden
if ($null -ne $process) {
    Start-Sleep -Seconds 5
    try {
        if (-not $process.HasExited) {
            $process.CloseMainWindow() | Out-Null
        }
    } catch {}
}
"""
    result = None
    for _ in range(max(1, copies)):
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
                str(file_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )
    return result


def run_prepared_pages_print_command(page_paths: list[Path], copies: int = 1) -> subprocess.CompletedProcess:
    if os.name != "nt":
        raise RuntimeError("Печать поддержана только на Windows в этой версии")
    if not page_paths:
        raise RuntimeError("Нет подготовленных страниц для печати")

    import win32con
    import win32print
    import win32ui
    from PIL import Image, ImageWin

    printer_name = get_target_printer_name()
    if not printer_name:
        printer_name = win32print.GetDefaultPrinter()
    if not printer_name:
        raise RuntimeError("Не найден принтер для прямой печати")

    printer_dc = win32ui.CreateDC()
    document_started = False
    try:
        printer_dc.CreatePrinterDC(printer_name)
        printable_width = printer_dc.GetDeviceCaps(win32con.HORZRES)
        printable_height = printer_dc.GetDeviceCaps(win32con.VERTRES)
        if printable_width <= 0 or printable_height <= 0:
            raise RuntimeError("Принтер не сообщил размер области печати")

        printer_dc.StartDoc("PrintLantern")
        document_started = True
        for _copy in range(max(1, min(99, copies))):
            for page_path in page_paths:
                if not page_path.exists():
                    raise RuntimeError(f"Подготовленная страница не найдена: {page_path}")
                with Image.open(page_path) as source_image:
                    image = source_image.convert("RGB")
                    scale = min(
                        printable_width / image.width,
                        printable_height / image.height,
                    )
                    draw_width = max(1, round(image.width * scale))
                    draw_height = max(1, round(image.height * scale))
                    left = (printable_width - draw_width) // 2
                    top = (printable_height - draw_height) // 2
                    dib = ImageWin.Dib(image)
                    printer_dc.StartPage()
                    try:
                        dib.draw(
                            printer_dc.GetHandleOutput(),
                            (left, top, left + draw_width, top + draw_height),
                        )
                    finally:
                        printer_dc.EndPage()
        printer_dc.EndDoc()
        document_started = False
    except Exception:
        if document_started:
            try:
                printer_dc.AbortDoc()
            except Exception:
                pass
        raise
    finally:
        printer_dc.DeleteDC()

    return subprocess.CompletedProcess(
        args=["windows-gdi", printer_name],
        returncode=0,
        stdout="",
        stderr="",
    )


def print_worker() -> None:
    while True:
        job_id = print_queue.get()
        try:
            with jobs_lock:
                job = jobs.get(job_id)
                if not job or job.get("status") != "queued":
                    continue
                job["status"] = "printing"
                job["progress"] = 25
                job["message"] = "Открываю системную печать Windows"
                job["updated_at"] = utc_now()
                save_jobs()

            copies = int(job.get("copies", 1))
            prepared_paths = [Path(path) for path in job.get("prepared_paths", [])]
            file_path = Path(job["file_path"])
            update_job(job_id, progress=60, message=f"Отправляю в принтер, копий: {copies}")
            if TEST_NO_PRINT:
                update_job(
                    job_id,
                    status="completed",
                    progress=100,
                    message="Тестовый режим: прямой маршрут проверен без физической печати.",
                )
                continue
            if prepared_paths:
                result = run_prepared_pages_print_command(prepared_paths, copies)
            else:
                result = run_native_print_command(file_path, copies)
            message = "Передано в очередь печати Windows"
            if result.stderr.strip():
                message += f": {result.stderr.strip()[:300]}"
            update_job(job_id, status="completed", progress=100, message=message)
        except subprocess.TimeoutExpired:
            update_job(
                job_id,
                status="failed",
                progress=100,
                message="Windows не ответила на команду печати за 90 секунд",
            )
        except subprocess.CalledProcessError:
            update_job(
                job_id,
                status="failed",
                progress=100,
                message=(
                    "Windows не смогла отправить файл в печать. Если это PDF/DOCX, проверь программу "
                    "по умолчанию. Если это картинка или текст, используй A4-предпросмотр и кнопку печати оттуда."
                ),
            )
        except Exception as exc:
            update_job(job_id, status="failed", progress=100, message=friendly_status_message(str(exc), "failed"))
        finally:
            print_queue.task_done()


def start_worker() -> None:
    worker = threading.Thread(target=print_worker, name="print-worker", daemon=True)
    worker.start()


def cleanup_sessions() -> None:
    now = time.time()
    with sessions_lock:
        expired = [sid for sid, data in sessions.items() if data.get("expires", 0) < now]
        for sid in expired:
            sessions.pop(sid, None)
        if expired:
            save_sessions()


def get_client_ip(handler: BaseHTTPRequestHandler) -> str:
    return handler.client_address[0] if handler.client_address else "unknown"


def too_many_login_failures(ip: str) -> bool:
    now = time.time()
    failures = [stamp for stamp in login_failures.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    login_failures[ip] = failures
    return len(failures) >= MAX_LOGIN_FAILURES


def record_login_failure(ip: str) -> None:
    login_failures.setdefault(ip, []).append(time.time())


def clear_login_failures(ip: str) -> None:
    login_failures.pop(ip, None)


def create_session(username: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> tuple[str, str]:
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with sessions_lock:
        sessions[sid] = {
            "user": username,
            "csrf": csrf,
            "ttl": ttl_seconds,
            "expires": time.time() + ttl_seconds,
        }
        save_sessions()
    return sid, csrf


def get_session(handler: BaseHTTPRequestHandler) -> dict | None:
    cleanup_sessions()
    cookie_header = handler.headers.get("Cookie", "")
    cookie = SimpleCookie(cookie_header)
    morsel = cookie.get(SESSION_COOKIE)
    if not morsel:
        return None
    sid = morsel.value
    with sessions_lock:
        session = sessions.get(sid)
        if not session:
            return None
        ttl_seconds = int(session.get("ttl", SESSION_TTL_SECONDS))
        session["expires"] = time.time() + ttl_seconds
        save_sessions()
        return session


def remove_session(handler: BaseHTTPRequestHandler) -> None:
    cookie_header = handler.headers.get("Cookie", "")
    cookie = SimpleCookie(cookie_header)
    morsel = cookie.get(SESSION_COOKIE)
    if morsel:
        with sessions_lock:
            sessions.pop(morsel.value, None)
            save_sessions()


LOGIN_PAGE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PrintLantern · вход</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      background: #eef1f4;
      color: #20242a;
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; }
    main {
      width: min(420px, 100%);
      background: #fff;
      border: 1px solid #d6dce2;
      border-radius: 8px;
      padding: 28px;
      box-shadow: 0 20px 60px rgba(37, 48, 60, .14);
    }
    h1 { margin: 0 0 22px; font-size: 26px; line-height: 1.1; letter-spacing: 0; }
    label { display: block; font-weight: 650; font-size: 14px; margin: 14px 0 6px; }
    .remember {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 14px;
      color: #46525f;
      font-size: 14px;
      font-weight: 600;
    }
    .remember input {
      width: 18px;
      min-height: 18px;
      accent-color: #1f7a4f;
    }
    input {
      width: 100%;
      min-height: 44px;
      border: 1px solid #c8d0d8;
      border-radius: 6px;
      padding: 10px 12px;
      font-size: 16px;
      background: #fbfcfd;
    }
    button {
      width: 100%;
      min-height: 44px;
      margin-top: 18px;
      border: 0;
      border-radius: 6px;
      background: #1f7a4f;
      color: #fff;
      font-size: 16px;
      font-weight: 750;
      cursor: pointer;
    }
    .error { min-height: 20px; margin-top: 12px; color: #b42318; font-size: 14px; }
  </style>
</head>
<body>
  <main>
    <h1>PrintLantern</h1>
    <form method="post" action="/login" autocomplete="off">
      <label for="username">Логин</label>
      <input id="username" name="username" required autofocus>
      <label for="password">Пароль</label>
      <input id="password" name="password" type="password" required>
      <label class="remember" for="remember">
        <input id="remember" name="remember" type="checkbox" checked>
        Запомнить вход на этом устройстве
      </label>
      <button type="submit">Войти</button>
      <div class="error">__ERROR__</div>
    </form>
  </main>
</body>
</html>
"""


DASHBOARD_PAGE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PrintLantern</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      background: #edf1f4;
      color: #20242a;
      --line: #d7dde3;
      --muted: #657383;
      --green: #1f7a4f;
      --blue: #2368a2;
      --red: #b42318;
      --amber: #9a620b;
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; }
    header {
      min-height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 24px;
      background: #fff;
      border-bottom: 1px solid var(--line);
    }
    h1 { margin: 0; font-size: 22px; line-height: 1.15; letter-spacing: 0; }
    h2 { margin: 0 0 14px; font-size: 18px; letter-spacing: 0; }
    h3 { margin: 0 0 10px; font-size: 15px; letter-spacing: 0; }
    .userbar { display: flex; align-items: center; gap: 12px; color: var(--muted); font-size: 14px; }
    button {
      min-height: 38px;
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 8px 12px;
      font: inherit;
      font-weight: 750;
      cursor: pointer;
    }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .primary { background: var(--green); color: #fff; }
    .secondary { background: #fff; color: #20242a; border-color: #bcc6cf; }
    main {
      width: min(1280px, 100%);
      margin: 0 auto;
      padding: 18px;
      display: grid;
      grid-template-columns: 330px minmax(0, 1fr) 360px;
      gap: 16px;
    }
    section {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }
    label { display: block; font-weight: 750; font-size: 14px; margin: 12px 0 6px; }
    input[type=file], input[type=number], select {
      width: 100%;
      min-height: 42px;
      border: 1px solid #c8d0d8;
      border-radius: 6px;
      padding: 8px 10px;
      background: #fbfcfd;
      font: inherit;
    }
    input[type=file] { border-style: dashed; }
    input[type=range] { width: 100%; accent-color: var(--green); }
    .meta, .state, .hint { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .state { min-height: 20px; margin-top: 10px; }
    .upload-actions, .tool-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 12px; }
    .paste-target {
      min-height: 54px;
      margin-top: 10px;
      border: 1px dashed #9fb0bf;
      border-radius: 6px;
      padding: 12px;
      background: #f7f9fb;
      color: var(--muted);
      font-size: 14px;
      outline: none;
    }
    .paste-target:focus { border-color: var(--green); box-shadow: 0 0 0 3px rgba(30, 132, 73, .14); }
    .paste-target:empty::before { content: attr(data-placeholder); color: var(--muted); }
    .paste-target:not(:empty) { color: #20242a; }
    .tools { display: grid; gap: 12px; }
    .tool-group { border-top: 1px solid var(--line); padding-top: 12px; }
    .control-line { display: grid; grid-template-columns: 1fr 52px; gap: 10px; align-items: center; }
    .control-value { color: var(--muted); font-size: 13px; text-align: right; }
    .preview-shell {
      display: grid;
      place-items: start center;
      min-height: 620px;
      overflow: auto;
      background: #dde4ea;
      border-radius: 8px;
      padding: 18px;
    }
    .preview-stack { display: grid; gap: 18px; width: min(100%, 760px); justify-items: center; }
    canvas.page, img.page {
      width: min(100%, 520px);
      height: auto;
      background: #fff;
      border: 1px solid #b9c3cc;
      box-shadow: 0 14px 34px rgba(31, 42, 55, .18);
    }
    canvas.page {
      touch-action: none;
    }
    .placeholder {
      width: min(100%, 520px);
      min-height: 360px;
      border: 1px dashed #aeb9c4;
      border-radius: 8px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      padding: 24px;
      background: #f7f9fb;
    }
    iframe.pdf-preview {
      width: min(100%, 760px);
      height: 720px;
      border: 1px solid #b9c3cc;
      background: #fff;
    }
    .jobs-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 8px; }
    .jobs-count { color: var(--muted); font-size: 13px; }
    .job-list { display: grid; gap: 10px; max-height: 720px; overflow: auto; padding-right: 2px; }
    .job {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      display: grid;
      gap: 8px;
      background: #fbfcfd;
    }
    .job-top { display: grid; gap: 4px; }
    .job-name { font-weight: 800; overflow-wrap: anywhere; }
    .job-time { color: var(--muted); font-size: 12px; }
    .status-row { display: grid; gap: 7px; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
    .badge {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
      background: #e7edf3;
      color: #344251;
      white-space: nowrap;
    }
    .draft { background: #eef2f6; color: #405061; }
    .queued { background: #e9f1fb; color: var(--blue); }
    .pending_approval { background: #fff0d6; color: var(--amber); }
    .printing { background: #fff0d6; color: var(--amber); }
    .completed { background: #e4f5eb; color: var(--green); }
    .failed, .interrupted { background: #fde8e6; color: var(--red); }
    .cancelled { background: #eef2f6; color: #405061; }
    .bar { height: 8px; background: #e4e9ee; border-radius: 999px; overflow: hidden; }
    .fill { height: 100%; width: 0; background: var(--green); transition: width .2s ease; }
    .empty { color: var(--muted); border: 1px dashed var(--line); border-radius: 8px; padding: 28px; text-align: center; }
    @media (max-width: 1100px) {
      main { grid-template-columns: 330px minmax(0, 1fr); }
      .queue-panel { grid-column: 1 / -1; }
    }
    @media (max-width: 760px) {
      header { padding: 12px 16px; align-items: flex-start; flex-direction: column; }
      main { grid-template-columns: 1fr; padding: 12px; }
      .preview-shell { min-height: 420px; padding: 12px; }
      iframe.pdf-preview { height: 520px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>PrintLantern</h1>
    <div class="userbar"><span>Локальный доступ</span></div>
  </header>
  <main>
    <section>
      <h2>Файл</h2>
      <form id="uploadForm">
        <input type="hidden" name="csrf" value="__CSRF__">
        <label for="file">Загрузить</label>
        <input id="file" name="file" type="file" required>
        <div class="meta">Максимум: __MAX_UPLOAD_MB__ МБ. Принтер: __PRINTER_LABEL__.</div>
        <label for="orientation">Лист</label>
        <select id="orientation">
          <option value="portrait">A4 вертикально</option>
          <option value="landscape">A4 горизонтально</option>
        </select>
        <div class="upload-actions">
          <button class="primary" type="submit">Подготовить</button>
          <button class="secondary" id="pasteBtn" type="button">Вставить из буфера обмена</button>
        </div>
        <div class="paste-target" id="pasteTarget" contenteditable="true" data-placeholder="Нажми сюда и выбери «Вставить»" hidden></div>
        <div class="state" id="uploadState"></div>
      </form>
      <div class="tools" id="tools" hidden>
        <div class="tool-group">
          <h3>Печать</h3>
          <label for="copies">Копии</label>
          <input id="copies" type="number" min="1" max="99" value="1">
          <label for="pageRange">Страницы</label>
          <input id="pageRange" type="text" placeholder="Все, 2 или 1-3, 5">
          <div class="hint" id="pageRangeHint">Пусто = печатать все страницы.</div>
          <div class="tool-row">
            <button class="primary" id="printBtn" type="button">Отправить в принтер</button>
            <button class="secondary" id="resetBtn" type="button">Сброс</button>
          </div>
          <div class="state" id="printState"></div>
        </div>
        <div class="tool-group" id="imageTools">
          <h3>Изображение</h3>
          <label for="scale">Размер</label>
          <div class="control-line"><input id="scale" type="range" min="10" max="220" value="100"><span class="control-value" id="scaleValue">100%</span></div>
          <label for="rotation">Поворот</label>
          <div class="control-line"><input id="rotation" type="range" min="-180" max="180" value="0"><span class="control-value" id="rotationValue">0°</span></div>
          <label for="offsetX">Сдвиг по X</label>
          <div class="control-line"><input id="offsetX" type="range" min="-700" max="700" value="0"><span class="control-value" id="offsetXValue">0</span></div>
          <label for="offsetY">Сдвиг по Y</label>
          <div class="control-line"><input id="offsetY" type="range" min="-900" max="900" value="0"><span class="control-value" id="offsetYValue">0</span></div>
          <div class="tool-row">
            <button class="secondary" id="fitBtn" type="button">Вписать</button>
            <button class="secondary" id="fillBtn" type="button">Заполнить</button>
            <button class="secondary" id="centerBtn" type="button">Центр</button>
          </div>
          <div class="hint">На телефоне можно двигать картинку пальцем прямо по листу.</div>
        </div>
        <div class="tool-group" id="textTools" hidden>
          <h3>Текст</h3>
          <label for="fontSize">Размер текста</label>
          <div class="control-line"><input id="fontSize" type="range" min="16" max="72" step="2" value="30"><span class="control-value" id="fontSizeValue">30 px</span></div>
        </div>
        <div class="tool-group" id="nativeTools">
          <h3>Формат</h3>
          <div class="hint" id="nativeHint"></div>
        </div>
      </div>
    </section>
    <section>
      <h2>Предпросмотр A4</h2>
      <div class="preview-shell">
        <div class="preview-stack" id="previewStack">
          <div class="placeholder">Загрузи файл, и здесь появится лист перед печатью.</div>
        </div>
      </div>
    </section>
    <section class="queue-panel">
      <div class="jobs-head">
        <h2>Очередь</h2>
        <span class="jobs-count" id="jobsCount"></span>
      </div>
      <div class="job-list" id="jobList">
        <div class="empty">Заданий пока нет</div>
      </div>
    </section>
  </main>
  <script>
    const CSRF = __CSRF_JSON__;
    const A4 = {
      portrait: { width: 1240, height: 1754 },
      landscape: { width: 1754, height: 1240 }
    };
    const statusText = {
      draft: "Настройка",
      pending_approval: "Ждёт подтверждения на ноутбуке",
      queued: "В очереди",
      printing: "Печать",
      completed: "Готово",
      failed: "Ошибка",
      interrupted: "Остановлено",
      cancelled: "Отклонено на ноутбуке"
    };
    const state = {
      job: null,
      mode: null,
      image: null,
      text: "",
      scale: 100,
      rotation: 0,
      offsetX: 0,
      offsetY: 0,
      fontSize: 30,
      fitMode: "fit",
      dragging: false,
      dragStart: null,
      pages: [],
      previewToken: 0
    };
    const els = {
      uploadForm: document.getElementById("uploadForm"),
      file: document.getElementById("file"),
      uploadState: document.getElementById("uploadState"),
      pasteBtn: document.getElementById("pasteBtn"),
      pasteTarget: document.getElementById("pasteTarget"),
      tools: document.getElementById("tools"),
      imageTools: document.getElementById("imageTools"),
      textTools: document.getElementById("textTools"),
      nativeTools: document.getElementById("nativeTools"),
      nativeHint: document.getElementById("nativeHint"),
      previewStack: document.getElementById("previewStack"),
      copies: document.getElementById("copies"),
      orientation: document.getElementById("orientation"),
      printBtn: document.getElementById("printBtn"),
      pageRange: document.getElementById("pageRange"),
      pageRangeHint: document.getElementById("pageRangeHint"),
      resetBtn: document.getElementById("resetBtn"),
      printState: document.getElementById("printState"),
      scale: document.getElementById("scale"),
      rotation: document.getElementById("rotation"),
      offsetX: document.getElementById("offsetX"),
      offsetY: document.getElementById("offsetY"),
      fontSize: document.getElementById("fontSize"),
      scaleValue: document.getElementById("scaleValue"),
      rotationValue: document.getElementById("rotationValue"),
      offsetXValue: document.getElementById("offsetXValue"),
      offsetYValue: document.getElementById("offsetYValue"),
      fontSizeValue: document.getElementById("fontSizeValue")
    };

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }[ch]));
    }

    function formatDate(value) {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "";
      return date.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "medium" });
    }

    function activeSize() {
      return A4[els.orientation.value] || A4.portrait;
    }

    function newPageCanvas() {
      const size = activeSize();
      const canvas = document.createElement("canvas");
      canvas.className = "page";
      canvas.width = size.width;
      canvas.height = size.height;
      return canvas;
    }

    function pageContext(canvas) {
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      return ctx;
    }

    function syncControls() {
      els.scale.value = state.scale;
      els.rotation.value = state.rotation;
      els.offsetX.value = state.offsetX;
      els.offsetY.value = state.offsetY;
      els.fontSize.value = state.fontSize;
      els.scaleValue.textContent = `${state.scale}%`;
      els.rotationValue.textContent = `${state.rotation}°`;
      els.offsetXValue.textContent = String(state.offsetX);
      els.offsetYValue.textContent = String(state.offsetY);
      els.fontSizeValue.textContent = `${state.fontSize} px`;
    }

    function showPlaceholder(text) {
      els.previewStack.innerHTML = `<div class="placeholder">${escapeHtml(text)}</div>`;
      state.pages = [];
      updatePageRangeHint();
    }

    function updatePageRangeHint() {
      const total = getPageCount();
      els.pageRangeHint.textContent = total
        ? `Всего страниц: ${total}. Пусто = все, пример: 1-3, 5.`
        : "Пусто = печатать все страницы.";
    }

    function getPageCount() {
      if (!state.job) return 0;
      if (state.mode === "pdf_pages") return Number(state.job.prepared_count || 0);
      return state.pages.length || 0;
    }

    function parsePageRange(input, total) {
      const text = String(input || "").trim().toLowerCase();
      if (!text || text === "all" || text === "все") {
        return Array.from({ length: total }, (_, index) => index + 1);
      }
      const selected = new Set();
      for (const partRaw of text.split(",")) {
        const part = partRaw.trim();
        if (!part) continue;
        const range = part.match(new RegExp("^(\\\\d+)\\\\s*-\\\\s*(\\\\d+)$"));
        if (range) {
          let start = Number(range[1]);
          let end = Number(range[2]);
          if (start > end) [start, end] = [end, start];
          if (start < 1 || end > total) throw new Error(`Страницы должны быть от 1 до ${total}`);
          for (let page = start; page <= end; page += 1) selected.add(page);
          continue;
        }
        if (!new RegExp("^\\\\d+$").test(part)) throw new Error("Страницы пишутся так: 1-3, 5");
        const page = Number(part);
        if (page < 1 || page > total) throw new Error(`Страница ${page} вне диапазона 1-${total}`);
        selected.add(page);
      }
      if (!selected.size) throw new Error("Не выбраны страницы для печати");
      return Array.from(selected).sort((a, b) => a - b);
    }

    function drawImagePage() {
      if (!state.image) return;
      const canvas = newPageCanvas();
      const ctx = pageContext(canvas);
      const margin = 80;
      const areaW = canvas.width - margin * 2;
      const areaH = canvas.height - margin * 2;
      const fit = Math.min(areaW / state.image.width, areaH / state.image.height);
      const fill = Math.max(areaW / state.image.width, areaH / state.image.height);
      const base = state.fitMode === "fill" ? fill : fit;
      const scale = base * (state.scale / 100);
      const drawW = state.image.width * scale;
      const drawH = state.image.height * scale;
      ctx.save();
      ctx.translate(canvas.width / 2 + state.offsetX, canvas.height / 2 + state.offsetY);
      ctx.rotate(state.rotation * Math.PI / 180);
      ctx.drawImage(state.image, -drawW / 2, -drawH / 2, drawW, drawH);
      ctx.restore();
      state.pages = [canvas];
      els.previewStack.replaceChildren(canvas);
      attachCanvasDrag(canvas);
      updatePageRangeHint();
    }

    function wrapText(ctx, text, maxWidth) {
      const rows = [];
      const sourceLines = text.replace(/\\r\\n/g, "\\n").split("\\n");
      for (const source of sourceLines) {
        const words = source.match(new RegExp("\\\\S+|\\\\s+", "g")) || [];
        let line = "";
        if (!words.length) {
          rows.push("");
          continue;
        }
        for (const word of words) {
          const next = line + word;
          if (ctx.measureText(next).width > maxWidth && line.trim()) {
            rows.push(line.trimEnd());
            line = word.trimStart();
          } else {
            line = next;
          }
        }
        rows.push(line.trimEnd());
      }
      return rows;
    }

    function drawTextPages() {
      const probe = newPageCanvas();
      const probeCtx = probe.getContext("2d");
      const fontSize = state.fontSize;
      const lineHeight = Math.round(fontSize * 1.4);
      const margin = 92;
      probeCtx.font = `${fontSize}px Segoe UI, Arial, sans-serif`;
      const lines = wrapText(probeCtx, state.text || "", probe.width - margin * 2);
      const pages = [];
      let canvas = newPageCanvas();
      let ctx = pageContext(canvas);
      ctx.fillStyle = "#111827";
      ctx.font = `${fontSize}px Segoe UI, Arial, sans-serif`;
      let y = margin;
      for (const line of lines) {
        if (y + lineHeight > canvas.height - margin) {
          pages.push(canvas);
          canvas = newPageCanvas();
          ctx = pageContext(canvas);
          ctx.fillStyle = "#111827";
          ctx.font = `${fontSize}px Segoe UI, Arial, sans-serif`;
          y = margin;
        }
        ctx.fillText(line, margin, y);
        y += lineHeight;
      }
      pages.push(canvas);
      state.pages = pages;
      els.previewStack.replaceChildren(...pages);
      updatePageRangeHint();
    }

    function renderPreview() {
      const previewToken = ++state.previewToken;
      syncControls();
      if (!state.job) {
        showPlaceholder("Загрузи файл, и здесь появится лист перед печатью.");
        return;
      }
      if (state.mode === "image") drawImagePage();
      else if (state.mode === "text") drawTextPages();
      else if (state.mode === "pdf_pages") drawPreparedPages(previewToken);
      else showPlaceholder("Для этого формата предпросмотр зависит от программы Windows. Можно выбрать копии и отправить в печать.");
    }

    function loadPreviewImage(src) {
      return new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error("Не удалось загрузить страницу PDF"));
        image.src = src;
      });
    }

    async function drawPreparedPages(previewToken) {
      const count = Number(state.job.prepared_count || 0);
      if (!count) {
        showPlaceholder("PDF загружен, но страницы предпросмотра не найдены.");
        return;
      }

      els.printBtn.disabled = true;
      els.printBtn.textContent = "Готовлю PDF в A4...";
      const canvases = [];
      try {
        for (let index = 1; index <= count; index += 1) {
          const src = `/prepared/${encodeURIComponent(state.job.id)}/page-${String(index).padStart(3, "0")}.png`;
          const image = await loadPreviewImage(src);
          if (previewToken !== state.previewToken) return;

          const canvas = newPageCanvas();
          const ctx = pageContext(canvas);
          const margin = 80;
          const areaW = canvas.width - margin * 2;
          const areaH = canvas.height - margin * 2;
          const sourceLandscape = image.width > image.height;
          const targetLandscape = canvas.width > canvas.height;
          const rotate = sourceLandscape !== targetLandscape;
          const orientedW = rotate ? image.height : image.width;
          const orientedH = rotate ? image.width : image.height;
          const scale = Math.min(areaW / orientedW, areaH / orientedH);

          ctx.save();
          ctx.translate(canvas.width / 2, canvas.height / 2);
          if (rotate) ctx.rotate(Math.PI / 2);
          ctx.drawImage(
            image,
            -(image.width * scale) / 2,
            -(image.height * scale) / 2,
            image.width * scale,
            image.height * scale
          );
          ctx.restore();
          canvases.push(canvas);
        }
      } catch (error) {
        if (previewToken === state.previewToken) {
          showPlaceholder(error.message);
          els.printBtn.textContent = "PDF не подготовлен";
        }
        return;
      }

      if (previewToken !== state.previewToken) return;
      state.pages = canvases;
      els.previewStack.replaceChildren(...canvases);
      els.printBtn.disabled = false;
      els.printBtn.textContent = "Отправить в принтер";
      updatePageRangeHint();
    }

    function resetImageLayout() {
      state.scale = 100;
      state.rotation = 0;
      state.offsetX = 0;
      state.offsetY = 0;
      state.fitMode = "fit";
      renderPreview();
    }

    function attachCanvasDrag(canvas) {
      canvas.addEventListener("pointerdown", event => {
        if (state.mode !== "image") return;
        state.dragging = true;
        canvas.setPointerCapture(event.pointerId);
        state.dragStart = { x: event.clientX, y: event.clientY, ox: state.offsetX, oy: state.offsetY };
      });
      canvas.addEventListener("pointermove", event => {
        if (!state.dragging || !state.dragStart) return;
        const rect = canvas.getBoundingClientRect();
        const factorX = canvas.width / rect.width;
        const factorY = canvas.height / rect.height;
        state.offsetX = Math.round(state.dragStart.ox + (event.clientX - state.dragStart.x) * factorX);
        state.offsetY = Math.round(state.dragStart.oy + (event.clientY - state.dragStart.y) * factorY);
        state.offsetX = Math.max(-700, Math.min(700, state.offsetX));
        state.offsetY = Math.max(-900, Math.min(900, state.offsetY));
        renderPreview();
      });
      canvas.addEventListener("pointerup", () => { state.dragging = false; });
      canvas.addEventListener("pointercancel", () => { state.dragging = false; });
    }

    async function activateJob(job) {
      state.job = job;
      state.mode = job.preview_mode;
      state.image = null;
      state.text = "";
      state.fontSize = 30;
      resetImageLayout();
      els.tools.hidden = false;
      els.printState.textContent = "";
      els.printBtn.disabled = false;
      els.printBtn.textContent = "Отправить в принтер";
      els.pageRange.value = "";
      els.imageTools.hidden = state.mode !== "image";
      els.textTools.hidden = state.mode !== "text";
      els.nativeTools.hidden = state.mode === "image" || state.mode === "text" || state.mode === "pdf_pages";
      if (state.mode === "image") {
        els.nativeHint.textContent = "";
        const image = new Image();
        image.onload = () => {
          state.image = image;
          renderPreview();
        };
        image.onerror = () => showPlaceholder("Браузер не смог показать эту картинку. Можно отправить файл через обычную Windows-печать.");
        image.src = `/file/${encodeURIComponent(job.id)}`;
      } else if (state.mode === "text") {
        const response = await fetch(`/text/${encodeURIComponent(job.id)}`, { credentials: "same-origin" });
        const data = await response.json();
        state.text = data.text || "";
        renderPreview();
      } else if (state.mode === "pdf_pages") {
        renderPreview();
      } else {
        els.nativeHint.textContent = "Файл сохранен на ноутбуке. Автоматическая печать сейчас надежно работает для картинок и текста; этот формат лучше открыть на ноутбуке вручную.";
        els.printBtn.disabled = true;
        els.printBtn.textContent = "Для этого формата недоступно";
        renderPreview();
      }
    }

    async function loadJobs() {
      const response = await fetch("/api/jobs", { credentials: "same-origin" });
      if (response.status === 401) {
        location.reload();
        return;
      }
      const data = await response.json();
      state.jobs = data.jobs;
      const list = document.getElementById("jobList");
      const count = document.getElementById("jobsCount");
      count.textContent = data.jobs.length ? `${data.jobs.length}` : "";
      if (!data.jobs.length) {
        list.innerHTML = '<div class="empty">Заданий пока нет</div>';
        return;
      }
      list.innerHTML = data.jobs.map(job => {
        const status = statusText[job.status] || job.status;
        const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
        const canOpen = job.preview_mode && ["draft", "failed", "interrupted", "cancelled"].includes(job.status);
        return `
          <article class="job">
            <div class="job-top">
              <div class="job-name">${escapeHtml(job.original_name)}</div>
              <time class="job-time">${formatDate(job.created_at)}</time>
            </div>
            <div class="status-row">
              <span class="badge ${escapeHtml(job.status)}">${escapeHtml(status)}</span>
              <span>${escapeHtml(job.message || "")}</span>
            </div>
            <div class="bar" aria-label="${progress}%"><div class="fill" style="width:${progress}%"></div></div>
            ${canOpen ? `<button class="secondary open-job" type="button" data-job-id="${escapeHtml(job.id)}">Открыть</button>` : ""}
          </article>
        `;
      }).join("");
    }

    function uploadButton() {
      return els.uploadForm.querySelector('button[type="submit"]');
    }

    function clipboardStamp() {
      const now = new Date();
      const pad = value => String(value).padStart(2, "0");
      return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    }

    function extensionFromMime(type) {
      const mime = String(type || "").toLowerCase().split(";")[0];
      const known = {
        "application/pdf": ".pdf",
        "application/rtf": ".rtf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "text/markdown": ".md",
        "text/html": ".html",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tif",
        "image/heic": ".heic",
        "image/heif": ".heif",
        "image/svg+xml": ".svg"
      };
      if (known[mime]) return known[mime];
      if (mime.startsWith("image/")) return `.${mime.slice(6).replace(/[^a-z0-9]/g, "") || "png"}`;
      return ".txt";
    }

    function fileFromBlob(blob, name) {
      if (blob instanceof File && blob.name) return blob;
      try {
        return new File([blob], name, { type: blob.type || "application/octet-stream" });
      } catch {
        blob.name = name;
        return blob;
      }
    }

    function textToClipboardFile(text) {
      return fileFromBlob(
        new Blob([text], { type: "text/plain;charset=utf-8" }),
        `clipboard-text-${clipboardStamp()}.txt`
      );
    }

    function blobToClipboardFile(blob, index) {
      const ext = extensionFromMime(blob.type);
      return fileFromBlob(blob, `clipboard-${clipboardStamp()}-${index}${ext}`);
    }

    async function uploadFiles(files) {
      const picked = Array.from(files || []).filter(file => file && Number(file.size || 0) > 0);
      if (!picked.length) throw new Error("В буфере обмена не найден файл для загрузки");

      const submitButton = uploadButton();
      submitButton.disabled = true;
      els.pasteBtn.disabled = true;
      els.pasteTarget.hidden = true;
      let lastJob = null;
      try {
        for (let index = 0; index < picked.length; index += 1) {
          const file = picked[index];
          els.uploadState.textContent = picked.length > 1
            ? `Загружаю файл ${index + 1} из ${picked.length}...`
            : "Загружаю файл...";
          const form = new FormData();
          form.append("csrf", CSRF);
          form.append("file", file, file.name || `clipboard-${clipboardStamp()}${extensionFromMime(file.type)}`);
          const response = await fetch("/upload", {
            method: "POST",
            credentials: "same-origin",
            body: form
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "Не удалось загрузить файл");
          lastJob = data.job;
        }
        els.uploadForm.reset();
        els.uploadState.textContent = picked.length > 1 ? "Файлы готовы к настройке." : "Файл готов к настройке.";
        if (lastJob) await activateJob(lastJob);
        await loadJobs();
      } catch (error) {
        els.uploadState.textContent = error.message;
      } finally {
        submitButton.disabled = false;
        els.pasteBtn.disabled = false;
      }
    }

    function readDataTransferText(item) {
      return new Promise(resolve => item.getAsString(resolve));
    }

    async function fileFromSource(src, index) {
      if (!src || !(src.startsWith("data:") || src.startsWith("blob:"))) return null;
      const response = await fetch(src);
      const blob = await response.blob();
      return blobToClipboardFile(blob, index);
    }

    async function filesFromHtml(htmlText) {
      const documentCopy = new DOMParser().parseFromString(htmlText || "", "text/html");
      const images = Array.from(documentCopy.querySelectorAll("img"))
        .map(image => image.getAttribute("src") || "")
        .filter(src => src.startsWith("data:") || src.startsWith("blob:"));
      const files = [];
      for (const src of images) {
        const file = await fileFromSource(src, files.length + 1);
        if (file) files.push(file);
      }
      return files;
    }

    async function filesFromPasteEvent(event, includeText) {
      const data = event.clipboardData;
      if (!data) return [];
      const files = Array.from(data.files || []);
      const textItems = [];
      for (const item of Array.from(data.items || [])) {
        if (item.kind === "file") {
          const file = item.getAsFile();
          if (file && !files.includes(file)) files.push(file);
        } else if (includeText && item.kind === "string") {
          textItems.push(item);
        }
      }
      if (files.length) return files;
      if (!includeText) return [];

      for (const item of textItems) {
        if (item.type === "text/html") {
          const htmlText = await readDataTransferText(item);
          const htmlFiles = await filesFromHtml(htmlText);
          if (htmlFiles.length) return htmlFiles;
        }
      }
      for (const item of textItems) {
        if (item.type === "text/plain") {
          const text = await readDataTransferText(item);
          if (text.trim()) return [textToClipboardFile(text)];
        }
      }
      return [];
    }

    async function filesFromClipboardRead() {
      const files = [];
      if (navigator.clipboard && navigator.clipboard.read) {
        const items = await navigator.clipboard.read();
        for (const item of items) {
          const type = item.types.find(value => !value.startsWith("text/")) || item.types[0];
          if (!type) continue;
          if (type === "text/plain") {
            const blob = await item.getType(type);
            const text = await blob.text();
            if (text.trim()) files.push(textToClipboardFile(text));
            continue;
          }
          if (type === "text/html") {
            const blob = await item.getType(type);
            files.push(...await filesFromHtml(await blob.text()));
            continue;
          }
          const blob = await item.getType(type);
          files.push(blobToClipboardFile(blob, files.length + 1));
        }
      }
      if (!files.length && navigator.clipboard && navigator.clipboard.readText) {
        const text = await navigator.clipboard.readText();
        if (text.trim()) files.push(textToClipboardFile(text));
      }
      return files;
    }

    async function filesFromPasteTarget(includeText) {
      const files = [];
      const images = Array.from(els.pasteTarget.querySelectorAll("img"));
      for (const image of images) {
        const src = image.currentSrc || image.src || image.getAttribute("src") || "";
        const file = await fileFromSource(src, files.length + 1);
        if (file) files.push(file);
      }
      if (files.length || !includeText) return files;

      const text = els.pasteTarget.innerText || els.pasteTarget.textContent || "";
      const cleaned = text.replace(/\u00a0/g, " ").trim();
      if (cleaned) return [textToClipboardFile(cleaned)];
      return [];
    }

    function showManualPasteTarget() {
      els.pasteTarget.hidden = false;
      els.pasteTarget.replaceChildren();
      els.pasteTarget.focus();
      els.uploadState.textContent = "Нажми в поле и выбери «Вставить». После вставки файл загрузится сам.";
    }

    els.uploadForm.addEventListener("submit", async event => {
      event.preventDefault();
      if (!els.file.files.length) {
        els.uploadState.textContent = "Файл не выбран";
        return;
      }
      await uploadFiles(els.file.files);
    });

    els.pasteBtn.addEventListener("click", async () => {
      els.uploadState.textContent = "Читаю буфер обмена...";
      els.pasteBtn.disabled = true;
      try {
        const files = await filesFromClipboardRead();
        if (!files.length) {
          showManualPasteTarget();
          return;
        }
        await uploadFiles(files);
      } catch {
        showManualPasteTarget();
      } finally {
        els.pasteBtn.disabled = false;
      }
    });

    document.addEventListener("paste", async event => {
      const targetIsPasteBox = event.target === els.pasteTarget;
      const hasReadableText = event.clipboardData &&
        Array.from(event.clipboardData.items || []).some(item => item.kind === "string");
      const hasFile = event.clipboardData && (
        event.clipboardData.files.length ||
        Array.from(event.clipboardData.items || []).some(item => item.kind === "file")
      );
      if (!targetIsPasteBox && !hasFile) return;
      if (targetIsPasteBox && !hasFile && !hasReadableText) return;
      event.preventDefault();
      try {
        const files = await filesFromPasteEvent(event, targetIsPasteBox);
        await uploadFiles(files);
      } catch (error) {
        els.uploadState.textContent = error.message;
      }
    });

    let pasteTargetTimer = null;
    els.pasteTarget.addEventListener("input", () => {
      clearTimeout(pasteTargetTimer);
      pasteTargetTimer = setTimeout(async () => {
        try {
          const files = await filesFromPasteTarget(true);
          if (!files.length) return;
          await uploadFiles(files);
          els.pasteTarget.replaceChildren();
        } catch (error) {
          els.uploadState.textContent = error.message;
        }
      }, 250);
    });

    async function confirmPrint() {
      if (!state.job) return;
      els.printBtn.disabled = true;
      els.printState.textContent = "Готовлю печать...";
      try {
        const payload = {
          csrf: CSRF,
          job_id: state.job.id,
          copies: Number(els.copies.value || 1),
          page_numbers: [],
          pages: []
        };
        const total = getPageCount();
        if (total > 0) {
          payload.page_numbers = parsePageRange(els.pageRange.value, total);
        }
        if (state.mode === "pdf_pages" && state.pages.length !== total) {
          throw new Error("Подожди, пока PDF подготовится в выбранной ориентации A4");
        }
        if (state.mode === "image" || state.mode === "text" || state.mode === "pdf_pages") {
          const selected = new Set(payload.page_numbers);
          payload.pages = state.pages
            .filter((_, index) => selected.has(index + 1))
            .map(canvas => canvas.toDataURL("image/png"));
        }
        const response = await fetch("/print", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Не удалось отправить в печать");
        els.printState.textContent = data.job?.status === "pending_approval"
          ? "Отправлено на ноутбук. Печать ждёт подтверждения."
          : "Отправлено в очередь.";
        await loadJobs();
      } catch (error) {
        els.printState.textContent = error.message;
      } finally {
        els.printBtn.disabled = false;
      }
    }

    [els.scale, els.rotation, els.offsetX, els.offsetY].forEach(input => {
      input.addEventListener("input", () => {
        state.scale = Number(els.scale.value);
        state.rotation = Number(els.rotation.value);
        state.offsetX = Number(els.offsetX.value);
        state.offsetY = Number(els.offsetY.value);
        renderPreview();
      });
    });
    els.fontSize.addEventListener("input", () => {
      state.fontSize = Number(els.fontSize.value);
      renderPreview();
    });
    els.orientation.addEventListener("change", renderPreview);
    document.getElementById("fitBtn").addEventListener("click", () => { state.fitMode = "fit"; state.scale = 100; renderPreview(); });
    document.getElementById("fillBtn").addEventListener("click", () => { state.fitMode = "fill"; state.scale = 100; renderPreview(); });
    document.getElementById("centerBtn").addEventListener("click", () => { state.offsetX = 0; state.offsetY = 0; renderPreview(); });
    els.resetBtn.addEventListener("click", () => {
      if (state.mode === "text") state.fontSize = 30;
      resetImageLayout();
    });
    els.printBtn.addEventListener("click", confirmPrint);
    document.getElementById("jobList").addEventListener("click", event => {
      const button = event.target.closest(".open-job");
      if (!button) return;
      const job = (state.jobs || []).find(item => item.id === button.dataset.jobId);
      if (job) activateJob(job);
    });

    loadJobs();
    setInterval(loadJobs, 1800);
  </script>
</body>
</html>
"""


def render_login_page(error: str = "") -> str:
    return LOGIN_PAGE.replace("__ERROR__", html.escape(error))


def render_dashboard_page(session: dict) -> str:
    printer = str(config.get("printer_name") or "по умолчанию")
    replacements = {
        "__USERNAME__": html.escape(str(session.get("user", ""))),
        "__CSRF__": html.escape(str(session.get("csrf", ""))),
        "__CSRF_JSON__": json.dumps(session.get("csrf", "")),
        "__MAX_UPLOAD_MB__": html.escape(str(int(config.get("max_upload_mb", 80)))),
        "__PRINTER_LABEL__": html.escape(printer),
    }
    body = DASHBOARD_PAGE
    for placeholder, value in replacements.items():
        body = body.replace(placeholder, value)
    return body


def auth_enabled() -> bool:
    return bool(config.get("auth_enabled", False))


def anonymous_session() -> dict:
    return {"user": "admin", "csrf": "no-auth"}


class PrintPortalHandler(BaseHTTPRequestHandler):
    server_version = "PrintPortal/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_text(self, status: HTTPStatus, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, status: HTTPStatus, payload: dict) -> None:
        self.send_text(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def redirect(self, target: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def read_form(self, max_bytes: int = 20_000) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > max_bytes:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[0] if values else "" for key, values in parsed.items()}

    def read_json(self, max_bytes: int | None = None) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        limit = max_bytes or (get_max_upload_bytes() * 2)
        if length <= 0 or length > limit:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def require_session(self) -> dict | None:
        if not auth_enabled():
            return anonymous_session()
        session = get_session(self)
        if not session:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Нужен вход"})
            return None
        return session

    def csrf_ok(self, session: dict, form_value: str = "") -> bool:
        if not auth_enabled():
            return True
        expected = session.get("csrf", "")
        supplied = self.headers.get("X-CSRF-Token", "") or form_value
        return bool(expected) and hmac.compare_digest(expected, supplied)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "app": APP_NAME,
                    "require_desktop_approval": bool(
                        config.get("require_desktop_approval")
                    ),
                },
            )
            return
        if parsed.path == "/api/jobs":
            if not self.require_session():
                return
            with jobs_lock:
                payload = [public_job(job) for job in sorted(jobs.values(), key=lambda item: item.get("created_at", ""), reverse=True)]
            self.send_json(HTTPStatus.OK, {"jobs": payload})
            return
        if parsed.path.startswith("/file/"):
            self.handle_file(parsed.path.removeprefix("/file/"))
            return
        if parsed.path.startswith("/text/"):
            self.handle_text(parsed.path.removeprefix("/text/"))
            return
        if parsed.path.startswith("/prepared/"):
            self.handle_prepared(parsed.path.removeprefix("/prepared/"))
            return
        if parsed.path != "/":
            self.send_text(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
            return

        if not auth_enabled():
            self.send_text(HTTPStatus.OK, render_dashboard_page(anonymous_session()))
            return
        session = get_session(self)
        if not session:
            self.send_text(HTTPStatus.OK, render_login_page())
            return
        self.send_text(HTTPStatus.OK, render_dashboard_page(session))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            if not auth_enabled():
                self.redirect("/")
                return
            self.handle_login()
            return
        if parsed.path == "/logout":
            if not auth_enabled():
                self.redirect("/")
                return
            self.handle_logout()
            return
        if parsed.path == "/upload":
            self.handle_upload()
            return
        if parsed.path == "/print":
            self.handle_print_confirm()
            return
        if parsed.path == "/api/desktop/approve":
            self.handle_desktop_decision("approve")
            return
        if parsed.path == "/api/desktop/reject":
            self.handle_desktop_decision("reject")
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def handle_desktop_decision(self, decision: str) -> None:
        supplied = self.headers.get("X-PrintLantern-Desktop-Token", "")
        client_ip = str(self.client_address[0])
        if (
            client_ip not in {"127.0.0.1", "::1"}
            or not DESKTOP_API_TOKEN
            or not hmac.compare_digest(supplied, DESKTOP_API_TOKEN)
        ):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Desktop authorization required"})
            return

        payload = self.read_json(max_bytes=20_000)
        job_id = str(payload.get("job_id", ""))
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Задание не найдено"})
                return
            if job.get("status") != "pending_approval":
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Задание уже обработано или не ждёт подтверждения"},
                )
                return
            if decision == "approve":
                job["status"] = "queued"
                job["progress"] = 5
                job["message"] = "Подтверждено на ноутбуке. Ожидает отправки в принтер."
            else:
                job["status"] = "cancelled"
                job["progress"] = 0
                job["message"] = "Печать отклонена на ноутбуке."
            job["updated_at"] = utc_now()
            save_jobs()
            response_job = public_job(job)

        if decision == "approve":
            print_queue.put(job_id)
        self.send_json(HTTPStatus.OK, {"ok": True, "job": response_job})

    def get_authorized_job(self, job_id: str) -> dict | None:
        if not self.require_session():
            return None
        with jobs_lock:
            job = jobs.get(job_id)
        if not job:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Задание не найдено"})
            return None
        return job

    def handle_file(self, job_id: str) -> None:
        job = self.get_authorized_job(job_id)
        if not job:
            return
        path = Path(job.get("file_path", ""))
        if not path.exists() or UPLOAD_DIR.resolve() not in path.resolve().parents:
            self.send_text(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if Path(path.name).suffix.lower() in {".html", ".htm", ".xml", ".svg"}:
            content_type = "text/plain; charset=utf-8"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def handle_text(self, job_id: str) -> None:
        job = self.get_authorized_job(job_id)
        if not job:
            return
        if job.get("preview_mode") != "text":
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Это не текстовый файл"})
            return
        path = Path(job.get("file_path", ""))
        if not path.exists() or UPLOAD_DIR.resolve() not in path.resolve().parents:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Файл не найден"})
            return
        self.send_json(HTTPStatus.OK, {"text": read_text_file(path)})

    def handle_prepared(self, path_info: str) -> None:
        parts = path_info.split("/")
        if len(parts) != 2:
            self.send_text(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
            return
        job_id, page_name = parts
        job = self.get_authorized_job(job_id)
        if not job:
            return
        if not re.fullmatch(r"page-\d{3}\.png", page_name):
            self.send_text(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
            return
        path = FINAL_DIR / job_id / page_name
        try:
            resolved = path.resolve()
        except OSError:
            self.send_text(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
            return
        if not resolved.exists() or FINAL_DIR.resolve() not in resolved.parents:
            self.send_text(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
            return
        data = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def handle_login(self) -> None:
        ip = get_client_ip(self)
        if too_many_login_failures(ip):
            self.send_text(
                HTTPStatus.TOO_MANY_REQUESTS,
                render_login_page("Слишком много попыток. Подожди 15 минут."),
            )
            return

        form = self.read_form()
        username = form.get("username", "")
        password = form.get("password", "")
        remember = form.get("remember", "") == "on"
        expected_user = str(config.get("admin_user", ""))
        expected_hash = str(config.get("admin_password_hash", ""))
        if username == expected_user and verify_password(password, expected_hash):
            clear_login_failures(ip)
            ttl_seconds = SESSION_TTL_SECONDS if remember else SHORT_SESSION_TTL_SECONDS
            sid, _csrf = create_session(username, ttl_seconds)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={sid}; Path=/; Max-Age={ttl_seconds}; HttpOnly; SameSite=Strict",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        record_login_failure(ip)
        self.send_text(HTTPStatus.UNAUTHORIZED, render_login_page("Неверный логин или пароль"))

    def handle_logout(self) -> None:
        session = get_session(self)
        if session and not self.csrf_ok(session):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Неверный CSRF-токен"})
            return
        remove_session(self)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def handle_upload(self) -> None:
        session = self.require_session()
        if not session:
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Некорректный размер файла"})
            return
        if length <= 0 or length > get_max_upload_bytes():
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Файл слишком большой"})
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Нужна multipart-загрузка"})
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(length),
            },
        )
        csrf = form.getfirst("csrf", "")
        if not self.csrf_ok(session, csrf):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Неверный CSRF-токен"})
            return
        file_item = form["file"] if "file" in form else None
        if file_item is None or not getattr(file_item, "filename", ""):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Файл не выбран"})
            return

        original_name = sanitize_filename(file_item.filename)
        allowed, ext = extension_allowed(original_name)
        if not allowed:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": f"Этот тип файла нельзя печатать: {ext or 'без расширения'}"})
            return

        job_id = uuid.uuid4().hex
        stored_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{job_id}{ext}"
        target = UPLOAD_DIR / stored_name
        with target.open("wb") as out:
            shutil.copyfileobj(file_item.file, out, length=1024 * 1024)
        size = target.stat().st_size
        if size <= 0:
            target.unlink(missing_ok=True)
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Пустой файл"})
            return

        preview_mode = preview_mode_for_extension(ext)
        prepared_paths: list[str] = []
        message = "Файл загружен. Настрой предпросмотр и подтверди печать."
        try:
            if preview_mode == "pdf_pages":
                prepared_paths = render_pdf_pages(target, job_id)
                message = "PDF преобразован в страницы. Проверь предпросмотр и подтверди печать."
        except Exception as exc:
            target.unlink(missing_ok=True)
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        now = utc_now()
        job = {
            "id": job_id,
            "original_name": original_name,
            "stored_name": stored_name,
            "file_path": str(target),
            "size": size,
            "ext": ext,
            "preview_mode": preview_mode,
            "copies": 1,
            "prepared_paths": prepared_paths,
            "status": "draft",
            "progress": 0,
            "message": message,
            "created_at": now,
            "updated_at": now,
            "created_by": session.get("user", "admin"),
        }
        with jobs_lock:
            jobs[job_id] = job
            save_jobs()
        self.send_json(HTTPStatus.CREATED, {"ok": True, "job": public_job(job)})

    def handle_print_confirm(self) -> None:
        session = self.require_session()
        if not session:
            return
        payload = self.read_json()
        if not self.csrf_ok(session, str(payload.get("csrf", ""))):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Неверный CSRF-токен"})
            return

        job_id = str(payload.get("job_id", ""))
        page_numbers_raw = payload.get("page_numbers", [])
        try:
            copies = max(1, min(99, int(payload.get("copies", 1) or 1)))
        except (TypeError, ValueError):
            copies = 1
        pages = payload.get("pages", [])
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Задание не найдено"})
                return
            if job.get("status") not in {"draft", "failed", "interrupted", "cancelled"}:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Это задание уже отправлено в печать"})
                return

        prepared_paths: list[str] = []
        if isinstance(pages, list) and pages:
            if len(pages) > 50:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Слишком много страниц для одной печати"})
                return
            for index, data_url in enumerate(pages, start=1):
                if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Предпросмотр должен быть PNG-страницей"})
                    return
                try:
                    page_bytes = base64.b64decode(data_url.split(",", 1)[1], validate=True)
                except Exception:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Не удалось прочитать подготовленную страницу"})
                    return
                if len(page_bytes) > 20 * 1024 * 1024:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Одна из страниц слишком большая"})
                    return
                page_path = FINAL_DIR / f"{job_id}-page-{index:03d}.png"
                with page_path.open("wb") as fh:
                    fh.write(page_bytes)
                prepared_paths.append(str(page_path))
        else:
            with jobs_lock:
                existing_paths = list(jobs[job_id].get("prepared_paths", []) or [])
            try:
                prepared_paths = select_prepared_pages(existing_paths, page_numbers_raw)
            except ValueError as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

        if not prepared_paths:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Нет выбранных страниц для печати"})
            return

        with jobs_lock:
            job = jobs[job_id]
            job["copies"] = copies
            job["prepared_paths"] = prepared_paths
            requires_approval = bool(config.get("require_desktop_approval"))
            job["status"] = "pending_approval" if requires_approval else "queued"
            job["progress"] = 5
            job["message"] = (
                "Ждёт подтверждения печати на ноутбуке."
                if requires_approval
                else "Подтверждено. Ожидает отправки в принтер."
            )
            job["updated_at"] = utc_now()
            save_jobs()
        if not requires_approval:
            print_queue.put(job_id)
        self.send_json(HTTPStatus.OK, {"ok": True, "job": public_job(job)})


def main() -> None:
    ensure_dirs()
    load_jobs()
    load_sessions()
    start_worker()
    host = str(config.get("host", "0.0.0.0"))
    port = int(config.get("port", 8088))
    server = ThreadingHTTPServer((host, port), PrintPortalHandler)
    print(f"{APP_NAME} is running on http://127.0.0.1:{port}")
    print(f"Listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
