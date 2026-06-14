#!/usr/bin/env python3
"""
rgb565_to_bmp.py

Parse a C/C++ header (or .c) file containing one or more RGB565 image dumps of
the form:

    const uint16_t  Foo_Width  = 80;
    const uint16_t  Foo_Height = 40;
    const unsigned short Foo[3200] PROGMEM = {
        0x0000, 0x0000, ... ,   // row 0
        ...
    };

and write each array out as a separate 24-bit Windows .bmp file in the SAME
directory as this script.

Pixel data is interpreted as 16-bit RGB565 values (one entry == one pixel).

BYTE ORDER (important):
    These "image2data" dumps are labelled "RGB565 Dump(little endian)". That
    means the 16-bit literal you see (e.g. 0x8E73) is the value AS STORED in a
    little-endian byte stream, i.e. the two bytes are swapped relative to the
    logical RGB565 color. If you decode the literal directly you get wrong
    colors (dark frame pixels turn neon yellow/green). So by default this
    script BYTE-SWAPS each value before decoding, which restores the correct
    colors. Pass --no-swap if your particular dump is already in logical order.

Usage:
    python rgb565_to_bmp.py                 # reads the default source file
    python rgb565_to_bmp.py myimages.h      # reads a file you name
    python rgb565_to_bmp.py a.h b.c ...      # reads several files
    python rgb565_to_bmp.py --no-swap f.h   # decode literals as-is (no swap)

By default it looks for any of these in the script directory:
    images.h, images.c, sprites.h, sprites.c, data.h, data.c, buttons.h, buttons.c
You can also just edit DEFAULT_SOURCE below.
"""

import os
import re
import sys
import struct

# If no file is given on the command line, the script searches its own folder
# for the first of these that exists. Edit/extend as you like.
DEFAULT_CANDIDATES = [
    "Button.h", "Button.c",
    "buttons.h", "buttons.c",
    "images.h", "images.c",
    "sprites.h", "sprites.c",
    "data.h", "data.c",
    "rgb565.h", "rgb565.c",
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Matches:  const unsigned short NAME[ COUNT ] [PROGMEM] = { ... };
ARRAY_RE = re.compile(
    r"const\s+(?:unsigned\s+short|uint16_t|u16|word)\s+"
    r"(\w+)\s*"               # 1: array name
    r"\[\s*(\d+)\s*\]\s*"     # 2: declared element count
    r"(?:PROGMEM\s*)?"        # optional PROGMEM
    r"=\s*\{(.*?)\}\s*;",     # 3: body between { and };
    re.DOTALL,
)

HEX_RE = re.compile(r"0[xX][0-9A-Fa-f]+")

# Matches a scalar dimension define, e.g.  const uint16_t But_Width = 80;
DIM_RE = re.compile(
    r"const\s+(?:unsigned\s+short|uint16_t|u16|word|int|unsigned\s+int)\s+"
    r"(\w+?)_(Width|Height)\s*=\s*(\d+)\s*;",
    re.IGNORECASE,
)


def rgb565_to_rgb888(value, swap=True):
    """
    Convert a single 16-bit RGB565 value to an (r, g, b) 8-bit tuple.

    If `swap` is True (default), the two bytes are swapped first, because these
    "little endian" dumps store the value byte-swapped relative to the logical
    RGB565 color.
    """
    value &= 0xFFFF
    if swap:
        value = ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)
    r5 = (value >> 11) & 0x1F
    g6 = (value >> 5) & 0x3F
    b5 = value & 0x1F
    # Scale up to 8 bits, replicating high bits into the low bits for accuracy.
    r = (r5 << 3) | (r5 >> 2)
    g = (g6 << 2) | (g6 >> 4)
    b = (b5 << 3) | (b5 >> 2)
    return r, g, b


def write_bmp(path, width, height, pixels):
    """
    Write a 24-bit uncompressed BMP.

    `pixels` is a flat list of (r, g, b) tuples in left-to-right, top-to-bottom
    order (row 0 = top of image), length must be width * height.
    """
    row_stride = width * 3
    padding = (4 - (row_stride % 4)) % 4
    padded_stride = row_stride + padding
    image_size = padded_stride * height
    file_size = 14 + 40 + image_size  # file header + DIB header + pixels

    with open(path, "wb") as f:
        # --- BMP file header (14 bytes) ---
        f.write(b"BM")
        f.write(struct.pack("<I", file_size))   # total file size
        f.write(struct.pack("<HH", 0, 0))        # reserved
        f.write(struct.pack("<I", 54))           # offset to pixel data

        # --- DIB header: BITMAPINFOHEADER (40 bytes) ---
        f.write(struct.pack("<I", 40))           # header size
        f.write(struct.pack("<i", width))        # width
        f.write(struct.pack("<i", height))       # height (positive => bottom-up)
        f.write(struct.pack("<H", 1))            # color planes
        f.write(struct.pack("<H", 24))           # bits per pixel
        f.write(struct.pack("<I", 0))            # compression = BI_RGB
        f.write(struct.pack("<I", image_size))   # raw image size
        f.write(struct.pack("<i", 2835))         # x pixels-per-meter (~72 dpi)
        f.write(struct.pack("<i", 2835))         # y pixels-per-meter
        f.write(struct.pack("<I", 0))            # palette colors
        f.write(struct.pack("<I", 0))            # important colors

        # --- Pixel data: BMP rows are stored bottom-to-top, channels as BGR ---
        pad = b"\x00" * padding
        for y in range(height - 1, -1, -1):
            base = y * width
            row = bytearray()
            for x in range(width):
                r, g, b = pixels[base + x]
                row += bytes((b, g, r))
            f.write(row)
            f.write(pad)


def guess_dimensions(name, count, dims):
    """
    Figure out width/height for an array.

    Priority:
      1. Exact NAME_Width / NAME_Height defines.
      2. Any single Width/Height pair found in the file (these dumps share one).
      3. Fall back to a square-ish factorization of `count`.
    """
    # 1. exact prefix match
    if name in dims and "width" in dims[name] and "height" in dims[name]:
        w, h = dims[name]["width"], dims[name]["height"]
        if w * h == count:
            return w, h

    # 2. any global pair (common when one set of dims applies to all arrays)
    for d in dims.values():
        if "width" in d and "height" in d:
            w, h = d["width"], d["height"]
            if w * h == count:
                return w, h

    # 3. last resort: factor near the square root
    root = int(count ** 0.5)
    for w in range(root, 0, -1):
        if count % w == 0:
            return w, count // w
    return count, 1  # degenerate single-row fallback


def parse_dimensions(text):
    """Collect NAME_Width / NAME_Height defines from the source text."""
    dims = {}
    for m in DIM_RE.finditer(text):
        prefix, which, value = m.group(1), m.group(2).lower(), int(m.group(3))
        dims.setdefault(prefix, {})[which] = value
    return dims


def process_file(src_path, swap=True):
    with open(src_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    dims = parse_dimensions(text)
    arrays = ARRAY_RE.findall(text)

    if not arrays:
        print(f"  No RGB565 arrays found in {src_path}")
        return 0

    written = 0
    for name, declared_count, body in arrays:
        declared_count = int(declared_count)
        values = [int(h, 16) for h in HEX_RE.findall(body)]

        if not values:
            print(f"  [skip] {name}: no hex values parsed")
            continue
        if len(values) != declared_count:
            print(f"  [warn] {name}: declared {declared_count} but found "
                  f"{len(values)} values; using {len(values)}")

        count = len(values)
        width, height = guess_dimensions(name, count, dims)

        if width * height != count:
            print(f"  [warn] {name}: {count} pixels do not fit {width}x{height}; "
                  f"truncating/padding to match")
            # pad with black or truncate so the BMP is still valid
            if count < width * height:
                values += [0] * (width * height - count)
            else:
                values = values[:width * height]

        pixels = [rgb565_to_rgb888(v, swap=swap) for v in values]

        # Diagnostic: show a representative non-black pixel so you can confirm
        # the colors in the terminal (e.g. blue) without opening the file.
        sample_raw = next((v for v in values if v != 0x0000), values[0])
        sr, sg, sb = rgb565_to_rgb888(sample_raw, swap=swap)
        hue = ("blue" if sb >= sr and sb >= sg and sb > 40 else
               "green" if sg >= sr and sg >= sb else
               "red/brown" if sr >= sg and sr >= sb else "mixed")

        out_name = f"{name}.bmp"
        out_path = os.path.join(SCRIPT_DIR, out_name)
        write_bmp(out_path, width, height, pixels)
        print(f"  [ok]   {out_name}  ({width}x{height})  "
              f"sample 0x{sample_raw:04X} -> RGB({sr},{sg},{sb}) [{hue}]")
        written += 1

    return written


def resolve_sources(argv):
    """Decide which source file(s) to read (ignores flags like --no-swap)."""
    args = [a for a in argv[1:] if not a.startswith("-")]
    if args:
        # Use the paths given on the command line (relative to CWD).
        return list(args)

    # No args: search the script directory for a known source file.
    found = []
    for cand in DEFAULT_CANDIDATES:
        p = os.path.join(SCRIPT_DIR, cand)
        if os.path.isfile(p):
            found.append(p)
    return found


def resolve_one(name):
    """
    Return an existing path for a requested source name, or None.

    Tries, in order:
      1. the name as given (absolute, or relative to the current directory),
      2. the same name inside the script's own directory,
      3. a case-insensitive match inside the script's directory
         (handles e.g. 'Button.h' vs 'button.h' on case-sensitive systems).
    """
    if os.path.isfile(name):
        return name

    in_script_dir = os.path.join(SCRIPT_DIR, os.path.basename(name))
    if os.path.isfile(in_script_dir):
        return in_script_dir

    # case-insensitive scan of the script directory
    target = os.path.basename(name).lower()
    try:
        for entry in os.listdir(SCRIPT_DIR):
            if entry.lower() == target:
                return os.path.join(SCRIPT_DIR, entry)
    except OSError:
        pass
    return None


def main():
    swap = True
    if "--no-swap" in sys.argv:
        swap = False
    # (--swap is the default; accepted explicitly for clarity)

    requested = resolve_sources(sys.argv)

    if not requested:
        print("No source file given and none of the default names were found "
              "in the script directory.")
        print("Run it like:  python rgb565_to_bmp.py yourfile.h")
        print("Default names searched:", ", ".join(DEFAULT_CANDIDATES))
        sys.exit(1)

    print(f"Byte-swap: {'ON (little-endian dump)' if swap else 'OFF'}")
    print(f"Script directory: {SCRIPT_DIR}")
    print(f"Current directory: {os.getcwd()}")
    total = 0
    for src in requested:
        resolved = resolve_one(src)
        if resolved is None:
            print(f"File not found: {src}")
            print(f"  (looked in current dir and in {SCRIPT_DIR})")
            # Help the user see what *is* there:
            try:
                hdrs = [f for f in os.listdir(SCRIPT_DIR)
                        if f.lower().endswith((".h", ".c", ".hpp", ".cpp"))]
                if hdrs:
                    print("  Source files found next to the script:",
                          ", ".join(sorted(hdrs)))
            except OSError:
                pass
            continue
        print(f"Processing {resolved} ...")
        total += process_file(resolved, swap=swap)

    print(f"\nDone. Wrote {total} BMP file(s) to: {SCRIPT_DIR}")


if __name__ == "__main__":
    main()