"""The brand files (variant C, tools/make_brand.py) and the app icons built from them: every size is
drawn for its own pixels (nothing enlarged), the .icns carries the 1024 rep, 16 and 32 px use the
simplified art."""

import profile_home  # noqa: F401
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "assets" / "brand"
sys.path.insert(0, str(ROOT / "tools"))
import build_macos  # noqa: E402
import build_windows  # noqa: E402

SIZES = {"yoursidedoor-icon-1024.png": 1024, "yoursidedoor-icon-16.png": 16, "yoursidedoor-icon-32.png": 32,
         "yoursidedoor-icon-win-256.png": 256, "yoursidedoor-icon-win-16.png": 16,
         "yoursidedoor-icon-win-20.png": 20, "yoursidedoor-icon-win-24.png": 24,
         "yoursidedoor-icon-win-32.png": 32, "yoursidedoor-apple-touch-180.png": 180,
         "yoursidedoor-tray-template.png": 44}


def same(a, b):
    with Image.open(a) as first, Image.open(b) as second:
        return ImageChops.difference(first.convert("RGBA"), second.convert("RGBA")).getbbox() is None


class BrandFilesTest(unittest.TestCase):
    def test_each_file_has_its_size(self):
        for name, size in SIZES.items():
            with Image.open(BRAND / name) as picture:
                self.assertEqual(picture.size, (size, size), name)
        for name in ("yoursidedoor-logo.png", "yoursidedoor-logo-on-dark.png"):
            with Image.open(BRAND / name) as picture:
                self.assertEqual(picture.size, (1004, 240), name)
        self.assertFalse((BRAND / "yoursidedoor-icon-512.png").exists())

    def test_the_build_tools_read_the_masters(self):
        self.assertEqual(build_macos.ICON, BRAND / "yoursidedoor-icon-1024.png")
        self.assertEqual(build_windows.ICON, BRAND / "yoursidedoor-icon-win-256.png")
        for path in (*build_macos.SMALL_ICONS.values(), *build_windows.SMALL_ICONS.values()):
            self.assertTrue(path.is_file(), path)


@unittest.skipUnless(shutil.which("iconutil") and shutil.which("sips"), "macOS iconutil and sips")
class IcnsTest(unittest.TestCase):
    def test_the_icns_has_every_size_up_to_1024_and_small_art_at_16_and_32(self):
        with tempfile.TemporaryDirectory() as folder:
            icns = build_macos.make_icns(build_macos.ICON, folder)
            data = icns.read_bytes()
            chunks, at = {}, 8
            while at < len(data):
                kind, length = struct.unpack(">4sI", data[at:at + 8])
                chunks[kind] = data[at + 8:at + length]
                at += length
            self.assertIn(b"ic10", chunks)  # 512@2x
            with tempfile.NamedTemporaryFile(suffix=".png") as rep:
                rep.write(chunks[b"ic10"])
                rep.flush()
                with Image.open(rep.name) as picture:
                    self.assertEqual(picture.size, (1024, 1024))
            iconset = Path(folder) / "sidedoor.iconset"
            self.assertTrue(same(iconset / "icon_512x512@2x.png", build_macos.ICON))
            for name, small in (("icon_16x16.png", 16), ("icon_16x16@2x.png", 32), ("icon_32x32.png", 32)):
                self.assertTrue(same(iconset / name, build_macos.SMALL_ICONS[small]), name)


class IcoTest(unittest.TestCase):
    def test_the_ico_has_every_size_and_small_art_at_16_to_32(self):
        with tempfile.TemporaryDirectory() as folder:
            ico = build_windows.make_ico(build_windows.ICON, folder)
            with Image.open(ico) as image:
                self.assertEqual(image.ico.sizes(), {(size, size) for size in build_windows.ICO_SIZES})
                for size, path in build_windows.SMALL_ICONS.items():
                    with Image.open(path) as small:
                        self.assertIsNone(ImageChops.difference(image.ico.getimage((size, size)).convert("RGBA"),
                                                                small.convert("RGBA")).getbbox(), size)


if __name__ == "__main__":
    unittest.main()
