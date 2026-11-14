"""Bounded, request-local compatibility for existing Library SVG references.

This is validation, not SVG sanitation, acquisition or a public image authority.
Only captured, completely validated bytes are issued; no mutable pathname is
rendered. Descriptor metadata checks detect observed changes, not atomic snapshots.
Unsupported platforms/files/grammar retain the existing placeholder.
"""

import base64
from dataclasses import dataclass
from decimal import Decimal
from itertools import islice
import os
import re
import stat
from types import MappingProxyType
from xml.parsers import expat

from app import config


_MAX_ROWS = 128
_MAX_ATTEMPTS = 32
_MAX_FILE_BYTES = 65_536
_MAX_READ_BYTES = 262_144
_MAX_URI_CHARS = 393_216
_MAX_ELEMENTS = 128
_MAX_DEPTH = 2
_MAX_ATTRIBUTES = 16
_MAX_ATTRIBUTE_CHARS = 4_096
_MAX_NUMBERS = 4_096
_MAX_VERTICES = 128
_MAX_COMPONENTS = 64
_SVG = "http://www.w3.org/2000/svg"
_SPACE = " \t\r\n"
_REFERENCE = re.compile(r"(?:covers/)?([A-Za-z0-9_-]{1,128}\.(?i:svg))", re.ASCII)
_DECIMAL = r"[+-]?(?:[0-9]+(?:\.[0-9]{0,6})?|\.[0-9]{1,6})"
_NUMBER = re.compile(_DECIMAL, re.ASCII)
_POINTS = re.compile(rf"{_DECIMAL}(?:(?:[ \t\r\n]+|[ \t\r\n]*,[ \t\r\n]*){_DECIMAL})*", re.ASCII)
_PAINT = re.compile(r"(?:none|\#[0-9a-fA-F]{3}|\#[0-9a-fA-F]{6})", re.ASCII)
_COMMON = frozenset(("fill", "stroke", "opacity", "fill-opacity", "stroke-opacity",
                     "stroke-width", "stroke-linecap", "stroke-linejoin"))
_GEOMETRY = MappingProxyType({
    "svg": (frozenset(("width", "height")), frozenset(("width", "height", "viewBox", "version", "preserveAspectRatio"))),
    "rect": (frozenset(("width", "height")), frozenset(("x", "y", "width", "height", "rx", "ry"))),
    "circle": (frozenset(("r",)), frozenset(("cx", "cy", "r"))),
    "ellipse": (frozenset(("rx", "ry")), frozenset(("cx", "cy", "rx", "ry"))),
    "line": (frozenset(), frozenset(("x1", "y1", "x2", "y2"))),
    "polyline": (frozenset(("points",)), frozenset(("points",))),
    "polygon": (frozenset(("points",)), frozenset(("points",))),
})


class _Rejected(ValueError):
    """Expected unsupported input; never rendered as a diagnostic."""


@dataclass(frozen=True, slots=True, init=False)
class ValidatedLocalSvg:
    """Issued internally, bound to a single exact item/reference and issuer.

    Markers prevent accidental metadata hydration, not hostile Python execution.
    No public constructor accepts bytes, a URI, HTML or a validation assertion.
    """

    item_id: int
    raw_cover_path: str
    captured_bytes: bytes
    _issuer: object

    def __init__(self):
        raise TypeError("Local SVG sources are issued by prepare_library_artwork")

    @property
    def image_src(self) -> str:
        return "data:image/svg+xml;base64," + base64.b64encode(self.captured_bytes).decode("ascii")


@dataclass(frozen=True, slots=True, init=False)
class PreparedLibraryArtwork:
    _sources: MappingProxyType
    _issuer: object

    def __init__(self):
        raise TypeError("Use prepare_library_artwork")

    def source_for(self, item_id, raw_cover_path) -> ValidatedLocalSvg | None:
        # Do not hash/compare/convert subclasses or arbitrary metadata objects.
        if not _binding(item_id, raw_cover_path):
            return None
        source = self._sources.get((item_id, raw_cover_path))
        if (type(source) is ValidatedLocalSvg and getattr(source, "_issuer", None) is self._issuer
                and type(source.item_id) is int and type(source.raw_cover_path) is str
                and source.item_id == item_id and source.raw_cover_path == raw_cover_path
                and type(source.captured_bytes) is bytes):
            return source
        return None


def _binding(item_id, raw_cover_path):
    return (type(item_id) is int and 0 < item_id <= 2**63 - 1
            and type(raw_cover_path) is str)


def _rendered_rows(context):
    """Lazy native catalogue order, including members hidden inside details."""
    kind = context.get("result_kind")
    if type(kind) is not str:
        return
    if kind == "items":
        yield from context["items"]
    elif kind == "series":
        for series in context["series_results"]:
            yield series["representative"]
    elif kind == "groups":
        for group in context["groups"]:
            representative = group["representative"]
            yield representative
            for member in group["members"]:
                member_id, representative_id = member["id"], representative["id"]
                if (type(member_id) is not int or type(representative_id) is not int
                        or member_id != representative_id):
                    yield member


def _open_root():
    # Every required flag/capability is mandatory: never substitute a zero flag.
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
    if any(type(getattr(os, name, None)) is not int or not getattr(os, name) for name in required):
        raise _Rejected()
    if os.open not in os.supports_dir_fd:
        raise _Rejected()
    path = os.fspath(config.COVERS_DIR)  # configuration is resolved only at call time
    if type(path) is not str or "\0" in path:
        raise _Rejected()
    components = [part for part in path.split("/") if part and part != "."]
    if len(components) > _MAX_COMPONENTS or ".." in components:
        raise _Rejected()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/" if path.startswith("/") else ".", flags)
    try:
        for component in components:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _metadata(status):
    return (status.st_dev, status.st_ino, status.st_mode, status.st_nlink,
            status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def _capture(root, filename, budget):
    descriptor = os.open(filename, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=root)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not 0 <= before.st_size <= _MAX_FILE_BYTES):
            raise _Rejected()
        content = bytearray()
        while True:
            # A full-size file still needs an EOF probe; charge overflow bytes,
            # rejected files and short reads against the same request budget.
            amount = min(8192, _MAX_FILE_BYTES + 1 - len(content), budget[0])
            if amount <= 0:
                raise _Rejected()
            chunk = os.read(descriptor, amount)
            budget[0] -= len(chunk)
            content.extend(chunk)
            if len(content) > _MAX_FILE_BYTES:
                raise _Rejected()
            if not chunk:
                break
        after = os.fstat(descriptor)
        if len(content) != before.st_size or _metadata(before) != _metadata(after):
            raise _Rejected()
        return bytes(content)
    finally:
        os.close(descriptor)


def _validate_svg(content):
    if not content or not content.isascii() or b"&" in content:
        raise _Rejected()
    parser = expat.ParserCreate(namespace_separator="|")
    depth = elements = namespaces = numbers = 0

    def reject(*args):
        raise _Rejected()

    def number(text, low, high, *, positive=False):
        nonlocal numbers
        numbers += 1
        text = text.strip(_SPACE)
        if numbers > _MAX_NUMBERS or len(text) > 24 or not _NUMBER.fullmatch(text):
            raise _Rejected()
        value = Decimal(text)
        if not low <= value <= high or (positive and value == 0):
            raise _Rejected()

    def namespace(prefix, uri):
        nonlocal namespaces
        namespaces += 1
        if depth != 0 or namespaces != 1 or prefix is not None or uri != _SVG:
            raise _Rejected()

    def start(expanded, attrs):
        nonlocal depth, elements
        depth += 1
        elements += 1
        if (depth > _MAX_DEPTH or elements > _MAX_ELEMENTS or len(attrs) > _MAX_ATTRIBUTES
                or namespaces != 1 or not expanded.startswith(_SVG + "|")):
            raise _Rejected()
        name = expanded[len(_SVG) + 1:]
        if name not in _GEOMETRY or (name == "svg") != (depth == 1):
            raise _Rejected()
        required, allowed = _GEOMETRY[name]
        if not required <= attrs.keys() or not attrs.keys() <= allowed | _COMMON:
            raise _Rejected()
        for key, value in attrs.items():
            if len(value) > _MAX_ATTRIBUTE_CHARS:
                raise _Rejected()
            if key in ("fill", "stroke"):
                if not _PAINT.fullmatch(value):
                    raise _Rejected()
            elif key in ("opacity", "fill-opacity", "stroke-opacity"):
                number(value, 0, 1)
            elif key == "stroke-width":
                number(value, 0, 64)
            elif key == "stroke-linecap":
                if value not in ("butt", "round", "square"):
                    raise _Rejected()
            elif key == "stroke-linejoin":
                if value not in ("miter", "round", "bevel"):
                    raise _Rejected()
            elif key == "version":
                if value != "1.1":
                    raise _Rejected()
            elif key == "preserveAspectRatio":
                if value != "xMidYMid meet":
                    raise _Rejected()
            elif key == "viewBox":
                tokens = re.split(r"[ \t\r\n]+", value.strip(_SPACE))
                if len(tokens) != 4:
                    raise _Rejected()
                for index, token in enumerate(tokens):
                    number(token, -4096 if index < 2 else 1, 4096)
            elif key == "points":
                text = value.strip(_SPACE)
                if not _POINTS.fullmatch(text):
                    raise _Rejected()
                tokens = _NUMBER.findall(text)
                minimum = 4 if name == "polyline" else 6
                if len(tokens) % 2 or not minimum <= len(tokens) <= _MAX_VERTICES * 2:
                    raise _Rejected()
                for token in tokens:
                    number(token, -4096, 4096)
            elif key in ("width", "height", "r", "rx", "ry"):
                number(value, 1 if name == "svg" else 0, 4096, positive=True)
            else:  # Only the element-specific coordinate names remain.
                number(value, -4096, 4096)

    def end(name):
        nonlocal depth
        depth -= 1

    def characters(text):
        if text.strip(_SPACE):
            raise _Rejected()

    parser.StartNamespaceDeclHandler = namespace
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.XmlDeclHandler = reject
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.SkippedEntityHandler = reject
    parser.ProcessingInstructionHandler = reject
    parser.CommentHandler = reject
    parser.StartCdataSectionHandler = reject
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.Parse(content, True)
    if depth != 0 or elements == 0 or namespaces != 1:
        raise _Rejected()


def prepare_library_artwork(page_context) -> PreparedLibraryArtwork:
    """Sole public I/O entry; call once in an owned, drained framework worker.

    The native paginated catalogue context is not modified or traversed past the
    row/attempt limits. Each emitted occurrence (even identical file bytes for
    different items) pays its full URI cost. No SQL, providers or output cache.
    """
    issuer, sources = object(), {}
    root = None
    attempts = uri_chars = 0
    budget = [_MAX_READ_BYTES]
    try:
        for item in islice(_rendered_rows(page_context), _MAX_ROWS):
            item_id, raw = item.get("id"), item.get("cover_path")
            if not _binding(item_id, raw):
                continue
            match = _REFERENCE.fullmatch(raw)
            if not match:
                continue
            attempts += 1
            try:
                if root is None:
                    root = _open_root()
                content = _capture(root, match[1], budget)
                _validate_svg(content)
                cost = len("data:image/svg+xml;base64,") + 4 * ((len(content) + 2) // 3)
                if uri_chars + cost <= _MAX_URI_CHARS:
                    source = object.__new__(ValidatedLocalSvg)
                    for name, value in (("item_id", item_id), ("raw_cover_path", raw),
                                        ("captured_bytes", content), ("_issuer", issuer)):
                        object.__setattr__(source, name, value)
                    sources[(item_id, raw)] = source
                    uri_chars += cost
            except (OSError, NotImplementedError, _Rejected, expat.ExpatError):
                pass  # Expected unsupported input, never a raw SVG URL fallback.
            if attempts == _MAX_ATTEMPTS or budget[0] == 0:
                break
    finally:
        if root is not None:
            os.close(root)
    prepared = object.__new__(PreparedLibraryArtwork)
    object.__setattr__(prepared, "_issuer", issuer)
    object.__setattr__(prepared, "_sources", MappingProxyType(sources))
    return prepared
