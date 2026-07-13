"""Generate desktop application icons from one square source PNG."""

import argparse
import binascii
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Iterable, Tuple


PNG_ICON_SIZES: Tuple[Tuple[str, int], ...] = (
    ("32x32.png", 32),
    ("128x128.png", 128),
    ("128x128@2x.png", 256),
    ("icon.png", 1024),
    ("Square30x30Logo.png", 30),
    ("Square44x44Logo.png", 44),
    ("Square71x71Logo.png", 71),
    ("Square89x89Logo.png", 89),
    ("Square107x107Logo.png", 107),
    ("Square142x142Logo.png", 142),
    ("Square150x150Logo.png", 150),
    ("Square284x284Logo.png", 284),
    ("Square310x310Logo.png", 310),
    ("StoreLogo.png", 50),
)

ICNS_ICONSET_SIZES: Tuple[Tuple[str, int], ...] = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)

ICO_SIZES: Tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)


def main() -> None:
    """Parse arguments and generate the desktop icon asset set.

    Args:
        None.

    Returns:
        None.

    Raises:
        SystemExit: If required commands or source files are missing.

    Side effects:
        Writes icon files under the configured output directory.
    """

    args = _parse_args()
    source = args.source.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    _validate_inputs(source)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_copy = output_dir / "icon-source.png"
    if source != source_copy:
        shutil.copyfile(source, source_copy)
    for filename, size in PNG_ICON_SIZES:
        _resize_icon_png(source, output_dir / filename, size)

    _generate_icns(source, output_dir / "icon.icns")
    _generate_ico(source, output_dir / "icon.ico")


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        None.

    Returns:
        Parsed argument namespace.

    Raises:
        SystemExit: If arguments are invalid.

    Side effects:
        Reads process command-line arguments.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        type=Path,
        help="Square source PNG path. Existing icon files in the output directory are overwritten.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("apps/desktop/src-tauri/icons"),
        help="Output icon directory.",
    )
    return parser.parse_args()


def _validate_inputs(source: Path) -> None:
    """Validate source image and required macOS commands.

    Args:
        source: Source PNG image path.

    Returns:
        None.

    Raises:
        SystemExit: If the source or required commands are missing.

    Side effects:
        Checks local filesystem and PATH.
    """

    for command in ("sips", "iconutil"):
        if shutil.which(command) is None:
            raise SystemExit(f"required command not found: {command}")
    if not source.is_file():
        raise SystemExit(f"source image not found: {source}")
    width, height = _read_png_size(source)
    if width != height:
        raise SystemExit(f"source image must be square, got {width}x{height}: {source}")


def _read_png_size(source: Path) -> Tuple[int, int]:
    """Read PNG dimensions with the macOS image metadata tool.

    Args:
        source: Source PNG image path.

    Returns:
        Width and height in pixels.

    Raises:
        subprocess.CalledProcessError: If ``sips`` cannot inspect the image.
        ValueError: If width or height cannot be parsed from ``sips`` output.

    Side effects:
        Runs the local ``sips`` command.
    """

    result = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(source)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    width = _parse_sips_dimension(result.stdout, "pixelWidth")
    height = _parse_sips_dimension(result.stdout, "pixelHeight")
    return width, height


def _parse_sips_dimension(output: str, key: str) -> int:
    """Parse one integer dimension from ``sips`` output.

    Args:
        output: Text output from ``sips -g``.
        key: Dimension key to parse, such as ``pixelWidth``.

    Returns:
        Parsed integer dimension.

    Raises:
        ValueError: If the requested key is missing or not an integer.

    Side effects:
        None.
    """

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            return int(stripped.split(":", 1)[1].strip())
    raise ValueError(f"missing {key} in sips output")


def _resize_icon_png(source: Path, destination: Path, size: int) -> None:
    """Resize a source PNG and apply rounded icon framing.

    Args:
        source: Source PNG image path.
        destination: Destination PNG path.
        size: Target width and height in pixels.

    Returns:
        None.

    Raises:
        subprocess.CalledProcessError: If ``sips`` fails.
        ValueError: If PNG post-processing cannot parse the resized file.

    Side effects:
        Writes a rounded PNG icon file.
    """

    _resize_png(source, destination, size)
    _apply_rounded_border(destination)


def _resize_png(source: Path, destination: Path, size: int) -> None:
    """Resize a source PNG into one square PNG file without post-processing.

    Args:
        source: Source PNG image path.
        destination: Destination PNG path.
        size: Target width and height in pixels.

    Returns:
        None.

    Raises:
        subprocess.CalledProcessError: If ``sips`` fails.

    Side effects:
        Writes a PNG file.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "sips",
            "--resampleHeightWidth",
            str(size),
            str(size),
            str(source),
            "--out",
            str(destination),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _generate_icns(source: Path, destination: Path) -> None:
    """Generate a macOS ICNS file through an iconset directory.

    Args:
        source: Source PNG image path.
        destination: Destination ICNS path.

    Returns:
        None.

    Raises:
        subprocess.CalledProcessError: If ``sips`` or ``iconutil`` fails.

    Side effects:
        Creates temporary files and writes an ICNS file.
    """

    with tempfile.TemporaryDirectory() as temp_dir:
        iconset = Path(temp_dir) / "AppIcon.iconset"
        iconset.mkdir()
        for filename, size in ICNS_ICONSET_SIZES:
            _resize_icon_png(source, iconset / filename, size)
        subprocess.run(
            [
                "iconutil",
                "--convert",
                "icns",
                "--output",
                str(destination),
                str(iconset),
            ],
            check=True,
        )


def _generate_ico(source: Path, destination: Path) -> None:
    """Generate a Windows ICO file containing PNG image entries.

    Args:
        source: Source PNG image path.
        destination: Destination ICO path.

    Returns:
        None.

    Raises:
        subprocess.CalledProcessError: If PNG resizing fails.

    Side effects:
        Creates temporary PNG files and writes an ICO file.
    """

    with tempfile.TemporaryDirectory() as temp_dir:
        png_paths = []
        for size in ICO_SIZES:
            path = Path(temp_dir) / f"icon-{size}.png"
            _resize_icon_png(source, path, size)
            png_paths.append((size, path))
        _write_png_ico(destination, png_paths)


def _write_png_ico(destination: Path, png_paths: Iterable[Tuple[int, Path]]) -> None:
    """Write an ICO container that stores PNG payloads.

    Args:
        destination: Destination ICO path.
        png_paths: Iterable of icon sizes and PNG payload paths.

    Returns:
        None.

    Raises:
        OSError: If reading PNG payloads or writing the ICO fails.

    Side effects:
        Writes an ICO file.
    """

    entries = [(size, path.read_bytes()) for size, path in png_paths]
    offset = 6 + 16 * len(entries)
    directory = bytearray()
    payload = bytearray()
    for size, data in entries:
        width = 0 if size == 256 else size
        height = 0 if size == 256 else size
        directory.extend(
            struct.pack(
                "<BBBBHHII",
                width,
                height,
                0,
                0,
                1,
                32,
                len(data),
                offset,
            )
        )
        payload.extend(data)
        offset += len(data)

    destination.write_bytes(
        struct.pack("<HHH", 0, 1, len(entries)) + bytes(directory) + bytes(payload)
    )


def _apply_rounded_border(path: Path) -> None:
    """Apply rounded clipping and a subtle border to one PNG icon.

    Args:
        path: PNG file to update in place.

    Returns:
        None.

    Raises:
        ValueError: If the PNG cannot be decoded as an 8-bit RGB/RGBA image.
        OSError: If reading or writing the image fails.

    Side effects:
        Rewrites the PNG file.
    """

    width, height, pixels = _read_png_rgba(path)
    if width != height:
        raise ValueError(f"icon must be square, got {width}x{height}: {path}")
    border_width = max(1.0, width * 0.018)
    radius = width * 0.19
    border_color = (224, 228, 235, 255)
    updated = bytearray(len(pixels))
    samples = (0.2, 0.5, 0.8)

    for y in range(height):
        for x in range(width):
            inside_samples = 0
            border_samples = 0
            for sy in samples:
                for sx in samples:
                    inside, distance = _rounded_rect_distance(
                        x + sx,
                        y + sy,
                        width,
                        height,
                        radius,
                    )
                    if inside:
                        inside_samples += 1
                        if distance <= border_width:
                            border_samples += 1
            coverage = inside_samples / 9.0
            border_mix = border_samples / 9.0
            offset = (y * width + x) * 4
            red, green, blue, alpha = pixels[offset : offset + 4]
            red = int(red * (1.0 - border_mix) + border_color[0] * border_mix)
            green = int(green * (1.0 - border_mix) + border_color[1] * border_mix)
            blue = int(blue * (1.0 - border_mix) + border_color[2] * border_mix)
            alpha = int(alpha * coverage)
            updated[offset : offset + 4] = bytes((red, green, blue, alpha))

    _write_png_rgba(path, width, height, bytes(updated))


def _rounded_rect_distance(
    x: float,
    y: float,
    width: int,
    height: int,
    radius: float,
) -> Tuple[bool, float]:
    """Return whether a point is inside a rounded rectangle and its edge distance.

    Args:
        x: Point x coordinate.
        y: Point y coordinate.
        width: Rectangle width.
        height: Rectangle height.
        radius: Corner radius.

    Returns:
        Tuple of inside flag and approximate distance to the nearest outer edge.

    Raises:
        None.

    Side effects:
        None.
    """

    if x < radius and y < radius:
        distance = radius - ((x - radius) ** 2 + (y - radius) ** 2) ** 0.5
        return distance >= 0, distance
    if x > width - radius and y < radius:
        distance = radius - ((x - (width - radius)) ** 2 + (y - radius) ** 2) ** 0.5
        return distance >= 0, distance
    if x < radius and y > height - radius:
        distance = radius - ((x - radius) ** 2 + (y - (height - radius)) ** 2) ** 0.5
        return distance >= 0, distance
    if x > width - radius and y > height - radius:
        distance = radius - (
            (x - (width - radius)) ** 2 + (y - (height - radius)) ** 2
        ) ** 0.5
        return distance >= 0, distance
    return True, min(x, y, width - x, height - y)


def _read_png_rgba(path: Path) -> Tuple[int, int, bytes]:
    """Read an 8-bit PNG file as RGBA bytes.

    Args:
        path: PNG file path.

    Returns:
        Width, height, and RGBA pixel bytes.

    Raises:
        ValueError: If the PNG format is unsupported.
        OSError: If reading the file fails.

    Side effects:
        Reads a PNG file.
    """

    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"not a PNG file: {path}")
    chunks = list(_iter_png_chunks(data))
    ihdr = next((payload for name, payload in chunks if name == b"IHDR"), None)
    if ihdr is None:
        raise ValueError(f"missing IHDR: {path}")
    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB",
        ihdr,
    )
    if bit_depth != 8 or color_type not in {2, 6} or compression != 0 or filter_method != 0:
        raise ValueError(f"unsupported PNG format: {path}")
    if interlace != 0:
        raise ValueError(f"interlaced PNG is unsupported: {path}")
    channels = 4 if color_type == 6 else 3
    compressed = b"".join(payload for name, payload in chunks if name == b"IDAT")
    raw = zlib.decompress(compressed)
    stride = width * channels
    rows = _unfilter_png_rows(raw, width, height, channels)
    rgba = bytearray(width * height * 4)
    for y, row in enumerate(rows):
        for x in range(width):
            source_offset = x * channels
            target_offset = (y * width + x) * 4
            rgba[target_offset] = row[source_offset]
            rgba[target_offset + 1] = row[source_offset + 1]
            rgba[target_offset + 2] = row[source_offset + 2]
            rgba[target_offset + 3] = row[source_offset + 3] if channels == 4 else 255
        if len(row) != stride:
            raise ValueError(f"invalid PNG row length: {path}")
    return width, height, bytes(rgba)


def _iter_png_chunks(data: bytes) -> Iterable[Tuple[bytes, bytes]]:
    """Iterate over PNG chunks.

    Args:
        data: Full PNG file bytes.

    Yields:
        Chunk type and chunk payload.

    Raises:
        ValueError: If a chunk is truncated.

    Side effects:
        None.
    """

    offset = 8
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("truncated PNG chunk header")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        name = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        if payload_end + 4 > len(data):
            raise ValueError("truncated PNG chunk payload")
        yield name, data[payload_start:payload_end]
        offset = payload_end + 4
        if name == b"IEND":
            return


def _unfilter_png_rows(
    raw: bytes,
    width: int,
    height: int,
    channels: int,
) -> Tuple[bytes, ...]:
    """Reverse PNG scanline filters.

    Args:
        raw: Decompressed PNG scanline bytes.
        width: Image width in pixels.
        height: Image height in pixels.
        channels: Number of color channels per pixel.

    Returns:
        Tuple of unfiltered row bytes.

    Raises:
        ValueError: If a PNG row uses an unsupported filter or is truncated.

    Side effects:
        None.
    """

    stride = width * channels
    rows = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        if offset + stride + 1 > len(raw):
            raise ValueError("truncated PNG scanline")
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset : offset + stride])
        offset += stride
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            up_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + up) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                row[index] = (row[index] + _paeth_predictor(left, up, up_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"unsupported PNG filter: {filter_type}")
        rows.append(bytes(row))
        previous = row
    return tuple(rows)


def _paeth_predictor(left: int, up: int, up_left: int) -> int:
    """Return the PNG Paeth predictor value.

    Args:
        left: Left byte value.
        up: Up byte value.
        up_left: Upper-left byte value.

    Returns:
        Predicted byte value.

    Raises:
        None.

    Side effects:
        None.
    """

    prediction = left + up - up_left
    left_distance = abs(prediction - left)
    up_distance = abs(prediction - up)
    up_left_distance = abs(prediction - up_left)
    if left_distance <= up_distance and left_distance <= up_left_distance:
        return left
    if up_distance <= up_left_distance:
        return up
    return up_left


def _write_png_rgba(path: Path, width: int, height: int, pixels: bytes) -> None:
    """Write RGBA bytes as an 8-bit PNG file.

    Args:
        path: Destination PNG path.
        width: Image width.
        height: Image height.
        pixels: RGBA pixel bytes.

    Returns:
        None.

    Raises:
        ValueError: If pixel data length is invalid.
        OSError: If writing the file fails.

    Side effects:
        Writes a PNG file.
    """

    stride = width * 4
    if len(pixels) != stride * height:
        raise ValueError("invalid RGBA pixel length")
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        start = y * stride
        raw.extend(pixels[start : start + stride])
    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png.extend(_png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)))
    png.extend(_png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9)))
    png.extend(_png_chunk(b"IEND", b""))
    path.write_bytes(bytes(png))


def _png_chunk(name: bytes, payload: bytes) -> bytes:
    """Build one PNG chunk.

    Args:
        name: Four-byte PNG chunk type.
        payload: Chunk payload bytes.

    Returns:
        Encoded PNG chunk bytes.

    Raises:
        None.

    Side effects:
        None.
    """

    checksum = binascii.crc32(name + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", checksum)


if __name__ == "__main__":
    main()
