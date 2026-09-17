from __future__ import annotations

"""Render branding/repo_icon.svg faithfully into a multi-resolution Windows ICO.

Canonical source is branding/repo_icon.svg. Current repository icons are
simplified to a background <rect> plus a single vectorized <path> (fill-rule
evenodd) rather than the older <text>/<circle> branding, so this generator
parses real SVG geometry instead of assuming text elements.

Renderer preference:
  1. cairosvg (real SVG renderer) when installed,
  2. system SVG rasterizers when available (rsvg-convert, resvg, magick),
  3. built-in path-capable fallback (rect + M/L/H/V/Z path parsing with
     evenodd scanline fill, Pillow only, no extra dependencies).

Usage:
  python scripts/generate_app_icon.py branding/repo_icon.svg branding/app_icon.ico
  python scripts/generate_app_icon.py branding/repo_icon.svg wb.ico --png branding/app_icon.png
"""

import argparse
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
RENDER_SIZE = 1024


def _parse_color(value: str, default=(0, 0, 0, 255)):
    if value is None:
        return default
    value = value.strip()
    if value.startswith("#"):
        hexpart = value[1:]
        if len(hexpart) == 3:
            hexpart = "".join(c * 2 for c in hexpart)
        if len(hexpart) == 6:
            return (int(hexpart[0:2], 16), int(hexpart[2:4], 16), int(hexpart[4:6], 16), 255)
        if len(hexpart) == 8:
            return (int(hexpart[0:2], 16), int(hexpart[2:4], 16), int(hexpart[4:6], 16), int(hexpart[6:8], 16))
        return default
    named = {
        "black": (0, 0, 0, 255),
        "white": (255, 255, 255, 255),
        "none": (0, 0, 0, 0),
        "transparent": (0, 0, 0, 0),
    }
    return named.get(value.lower(), default)


_TOKEN_RE = re.compile(r"[MmLlHhVvZzCcQqSsTtAa]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _tokenize_path(d: str):
    return _TOKEN_RE.findall(d)


def _parse_subpaths(d: str):
    """Parse an SVG path into a list of subpaths (each a list of (x, y)).

    Supports M/L/H/V/Z (absolute + relative) exactly; curve commands
    (C/S/Q/T/A) are approximated by straight lines to their endpoints so
    glyph outlines still rasterize faithfully.
    """
    toks = _tokenize_path(d)
    subpaths: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = 0.0
    sx = sy = 0.0
    cmd = None
    i = 0
    n = len(toks)
    curve_steps = {"C": 6, "c": 6, "S": 4, "s": 4, "Q": 4, "q": 4, "T": 2, "t": 2, "A": 7, "a": 7}

    def is_cmd(t):
        return len(t) == 1 and t in "MmLlHhVvZzCcQqSsTtAa"

    while i < n:
        t = toks[i]
        if is_cmd(t):
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    subpaths.append(cur)
                    cur = []
                x, y = sx, sy
                cmd = None
                continue
            if cmd in "Mm":
                first = True
                while i < n and not is_cmd(toks[i]):
                    if i + 1 >= n:
                        break
                    try:
                        nx = float(toks[i])
                        ny = float(toks[i + 1])
                    except ValueError:
                        break
                    i += 2
                    if cmd == "m":
                        nx += x
                        ny += y
                    x, y = nx, ny
                    if first:
                        sx, sy = x, y
                        cur = [(x, y)]
                        first = False
                    else:
                        cur.append((x, y))
                cmd = "l" if cmd == "m" else "L"
                continue
            continue
        if cmd is None:
            i += 1
            continue
        if cmd in "Ll":
            consumed = False
            while i < n and not is_cmd(toks[i]):
                if i + 1 >= n:
                    break
                try:
                    nx = float(toks[i])
                    ny = float(toks[i + 1])
                except ValueError:
                    break
                i += 2
                if cmd == "l":
                    nx += x
                    ny += y
                x, y = nx, ny
                cur.append((x, y))
                consumed = True
            if not consumed:
                i += 1
        elif cmd in "Hh":
            consumed = False
            while i < n and not is_cmd(toks[i]):
                try:
                    nx = float(toks[i])
                except ValueError:
                    break
                i += 1
                if cmd == "h":
                    nx += x
                x = nx
                cur.append((x, y))
                consumed = True
            if not consumed:
                i += 1
        elif cmd in "Vv":
            consumed = False
            while i < n and not is_cmd(toks[i]):
                try:
                    ny = float(toks[i])
                except ValueError:
                    break
                i += 1
                if cmd == "v":
                    ny += y
                y = ny
                cur.append((x, y))
                consumed = True
            if not consumed:
                i += 1
        elif cmd in curve_steps:
            steps = curve_steps[cmd]
            consumed = False
            while i < n and not is_cmd(toks[i]):
                if i + steps > n:
                    break
                chunk = toks[i : i + steps]
                if any(is_cmd(c) for c in chunk):
                    break
                try:
                    vals = [float(c) for c in chunk]
                except ValueError:
                    break
                if cmd in "Cc":
                    ex, ey = vals[4], vals[5]
                    if cmd == "c":
                        ex += x
                        ey += y
                    x, y = ex, ey
                    cur.append((x, y))
                elif cmd in "SsQqTt":
                    ex, ey = vals[-2], vals[-1]
                    if cmd.islower():
                        ex += x
                        ey += y
                    x, y = ex, ey
                    cur.append((x, y))
                elif cmd in "Aa":
                    ex, ey = vals[5], vals[6]
                    if cmd == "a":
                        ex += x
                        ey += y
                    x, y = ex, ey
                    cur.append((x, y))
                i += steps
                consumed = True
            if not consumed:
                i += 1
        else:
            i += 1
    if cur:
        subpaths.append(cur)
    return subpaths


def _builtin_render(svg_path: Path, size: int) -> Image.Image:
    text = svg_path.read_text(encoding="utf-8")
    match = re.search(r'viewBox="([\d.\-+\sEe]+)"', text)
    if match:
        vx, vy, vw, vh = map(float, match.group(1).split())
    else:
        vx, vy, vw, vh = 0.0, 0.0, 510.0, 510.0
    root = ET.fromstring(text)
    scale = size / max(vw, vh)
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)
    paths: list[tuple[tuple, list]] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "rect":
            rx = float(el.get("x", "0"))
            ry = float(el.get("y", "0"))
            rw = float(el.get("width", str(vw)))
            rh = float(el.get("height", str(vh)))
            fill = _parse_color(el.get("fill", "#000000"))
            if fill[3] == 0:
                continue
            x0 = int(round((rx - vx) * scale))
            y0 = int(round((ry - vy) * scale))
            x1 = int(round((rx + rw - vx) * scale))
            y1 = int(round((ry + rh - vy) * scale))
            draw.rectangle([x0, y0, x1, y1], fill=fill)
        elif tag == "path":
            fill = _parse_color(el.get("fill", "#000000"))
            if fill[3] == 0:
                continue
            subs = _parse_subpaths(el.get("d", ""))
            pix_subs = [[((px - vx) * scale, (py - vy) * scale) for px, py in sp] for sp in subs]
            paths.append((fill, pix_subs))
    for fill, pix_subs in paths:
        overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        edges = []
        for sp in pix_subs:
            count = len(sp)
            for k in range(count):
                x0, y0 = sp[k]
                x1, y1 = sp[(k + 1) % count]
                if y0 == y1:
                    continue
                edges.append((x0, y0, x1, y1))
        for yy in range(size):
            yc = yy + 0.5
            xs = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= yc < y1) or (y1 <= yc < y0):
                    t = (yc - y0) / (y1 - y0)
                    xs.append(x0 + t * (x1 - x0))
            if not xs:
                continue
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                xa = max(0, int(xs[k] + 0.5))
                xb = min(size, int(xs[k + 1] + 0.5))
                if xb > xa:
                    odraw.line([(xa, yy), (xb - 1, yy)], fill=fill)
        base = Image.alpha_composite(base, overlay)
    return base


def _cairosvg_render(svg_path: Path, size: int) -> Image.Image | None:
    try:
        import cairosvg  # type: ignore
    except Exception:
        return None
    try:
        import io

        data = cairosvg.svg2png(url=str(svg_path), output_width=size, output_height=size)
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


def _external_render(svg_path: Path, size: int) -> Image.Image | None:
    for tool, args in (
        ("rsvg-convert", ["-w", str(size), "-h", str(size), "-o"]),
        ("resvg", ["-w", str(size), "-h", str(size), "-o"]),
    ):
        exe = shutil.which(tool)
        if not exe:
            continue
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            if tool == "rsvg-convert":
                result = subprocess.run([exe, "-w", str(size), "-h", str(size), "-o", tmp_path, str(svg_path)], capture_output=True, timeout=60)
            else:
                result = subprocess.run([exe, "-w", str(size), "-h", str(size), str(svg_path), tmp_path], capture_output=True, timeout=60)
            if result.returncode == 0 and Path(tmp_path).exists():
                return Image.open(tmp_path).convert("RGBA")
        except Exception:
            pass
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            result = subprocess.run([magick, str(svg_path), "-resize", f"{size}x{size}", tmp_path], capture_output=True, timeout=60)
            if result.returncode == 0 and Path(tmp_path).exists():
                return Image.open(tmp_path).convert("RGBA")
        except Exception:
            pass
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
    return None


def render(svg_path: Path, size: int = RENDER_SIZE) -> Image.Image:
    image = _cairosvg_render(svg_path, size)
    if image is not None:
        return image
    image = _external_render(svg_path, size)
    if image is not None:
        if image.size != (size, size):
            image = image.resize((size, size), Image.LANCZOS)
        return image.convert("RGBA")
    return _builtin_render(svg_path, size)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a multi-resolution Windows ICO from the canonical repository SVG")
    parser.add_argument("source_svg", type=Path)
    parser.add_argument("output_ico", type=Path)
    parser.add_argument("--png", type=Path, default=None, help="Optional extra PNG output path")
    parser.add_argument("--size", type=int, default=RENDER_SIZE)
    args = parser.parse_args()

    image = render(args.source_svg, args.size)
    args.output_ico.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output_ico, format="ICO", sizes=SIZES)
    png_path = args.png or args.output_ico.with_suffix(".png")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    preview = image.resize((512, 512), Image.LANCZOS) if image.size != (512, 512) else image
    preview.save(png_path, format="PNG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
