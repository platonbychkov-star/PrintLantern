import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

from PIL import Image
import PIL


test_data_dir = tempfile.TemporaryDirectory(prefix="printlantern-silent-print-")
os.environ["PRINTLANTERN_DATA_DIR"] = test_data_dir.name
server_path = Path(__file__).resolve().parents[1] / "backend" / "server.py"
spec = importlib.util.spec_from_file_location("printlantern_server", server_path)
server = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(server)


class FakePrinterDC:
    def __init__(self):
        self.pages = 0
        self.document_started = False
        self.deleted = False

    def CreatePrinterDC(self, printer_name):
        assert printer_name == "Test Printer"

    def GetDeviceCaps(self, cap):
        return 2400 if cap == 1 else 3400

    def StartDoc(self, name):
        assert name == "PrintLantern"
        self.document_started = True

    def StartPage(self):
        self.pages += 1

    def GetHandleOutput(self):
        return 123

    def EndPage(self):
        pass

    def EndDoc(self):
        self.document_started = False

    def AbortDoc(self):
        self.document_started = False

    def DeleteDC(self):
        self.deleted = True


class FakeDib:
    draws = []

    def __init__(self, image):
        assert image.mode == "RGB"

    def draw(self, handle, bounds):
        assert handle == 123
        self.draws.append(bounds)


fake_dc = FakePrinterDC()
sys.modules["win32con"] = types.SimpleNamespace(HORZRES=1, VERTRES=2)
sys.modules["win32print"] = types.SimpleNamespace(
    GetDefaultPrinter=lambda: "Unexpected Default Printer"
)
sys.modules["win32ui"] = types.SimpleNamespace(CreateDC=lambda: fake_dc)
fake_image_win = types.SimpleNamespace(Dib=FakeDib)
sys.modules["PIL.ImageWin"] = fake_image_win
PIL.ImageWin = fake_image_win

server.config["printer_name"] = "Test Printer"
with tempfile.TemporaryDirectory() as temp_dir:
    page_path = Path(temp_dir) / "page.png"
    Image.new("RGB", (1240, 1754), "white").save(page_path)
    result = server.run_prepared_pages_print_command([page_path], copies=2)

assert result.returncode == 0
assert result.args == ["windows-gdi", "Test Printer"]
assert fake_dc.pages == 2
assert len(FakeDib.draws) == 2
assert fake_dc.deleted
test_data_dir.cleanup()
print("Silent Windows GDI print path passed without launching an application.")
