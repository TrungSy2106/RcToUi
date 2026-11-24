"""Export parsed RC objects to Qt; read binary source resources when needed."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
import base64
import codecs
import ctypes
import json
import math
import os
import posixpath
import re
import struct
import sys
import xml.etree.ElementTree as ET

from RcToUI import ConversionError, IncompleteConversionError, diagnostic


ATOMS = {0x80: "Button", 0x81: "Edit", 0x82: "Static", 0x83: "ListBox",
         0x84: "ScrollBar", 0x85: "ComboBox"}
STANDARD_IDS = {1: "IDOK", 2: "IDCANCEL", 3: "IDABORT", 4: "IDRETRY",
                5: "IDIGNORE", 6: "IDYES", 7: "IDNO", 8: "IDCLOSE", 9: "IDHELP"}
STATIC_ICON_SIZE = (32, 32)  # SS_ICON system-large size in 96-DPI logical pixels.
SS_REALSIZECONTROL, SS_CENTERIMAGE, SS_REALSIZEIMAGE = 0x40, 0x200, 0x800
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def conversion_decision(property, value, origin, reason):
    """Describe a conversion rule without treating inferred values as source."""
    return {"property": property, "value": value, "origin": origin, "reason": reason}


def icon_frame_depth(frame):
    """Old ICO directories can omit bit depth; read it from the image header."""
    if frame[5]:
        return frame[5]
    data = frame[6]
    if len(data) >= 16 and struct.unpack_from("<I", data)[0] in {40, 108, 124}:
        return struct.unpack_from("<H", data, 14)[0]
    if len(data) >= 26 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data[24] * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(data[25], 1)
    return (frame[2] - 1).bit_length() if frame[2] else 0


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ConversionError(f"truncated resource at byte {self.pos}, need {size} bytes")
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def unpack(self, fmt):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))

    def word(self):
        return self.unpack("H")[0]

@dataclass
class Resource:
    type: object
    name: object
    language: int
    data: object
    ribbon: object = None


@dataclass
class NativeControl:
    identifier: str
    numeric_id: object
    native_class: object
    text: object
    rect: tuple
    style: int
    exstyle: int
    help_id: int = 0
    creation_data: bytes = b""
    qt_type: str = ""
    kind: str = ""
    name: str = ""
    properties: dict = field(default_factory=dict)
    items: list = field(default_factory=list)
    children: list = field(default_factory=list)
    font: tuple = ()
    language: int = 0
    menu: object = ""
    decisions: list = field(default_factory=list)


def windows_scale(dialog):
    """Ask MapDialogRect using a hidden, control-free template of this font.

    Use a 96-DPI thread context so output is Qt logical pixels, independent of
    the monitor DPI and of the Python executable's DPI-awareness manifest.
    """
    if os.name != "nt":
        raise ConversionError("font-based DLU conversion requires Windows; pass --scale explicitly")
    from ctypes import wintypes as wt
    user, kernel = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32")
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    callback = callback_type(lambda *_: 0)
    user.CreateDialogIndirectParamW.argtypes = (wt.HINSTANCE, ctypes.c_void_p, wt.HWND, callback_type, wt.LPARAM)
    user.CreateDialogIndirectParamW.restype = wt.HWND
    user.MapDialogRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
    user.DestroyWindow.argtypes = (wt.HWND,)
    kernel.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
    kernel.GetModuleHandleW.restype = wt.HMODULE
    style = 0x80000000 | (dialog.style & 0x48)
    if dialog.font:
        style |= 0x40
    blob = struct.pack("<HHIIIHhhhhHHH", 1, 0xFFFF, 0, 0, style, 0, 0, 0, 10, 10, 0, 0, 0)
    if dialog.font:
        pointsize, family, weight, italic, charset = dialog.font
        blob += struct.pack("<HHBB", pointsize, weight, italic, charset) + family.encode("utf-16-le") + b"\0\0"
    context = None
    if hasattr(user, "SetThreadDpiAwarenessContext"):
        user.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        context = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-1))
    handle = None
    try:
        buffer = ctypes.create_string_buffer(blob)
        handle = user.CreateDialogIndirectParamW(kernel.GetModuleHandleW(None), buffer, None, callback, 0)
        rect = wt.RECT(0, 0, 4, 8)
        if not handle or not user.MapDialogRect(handle, ctypes.byref(rect)):
            raise ConversionError(f"MapDialogRect failed for font {dialog.font}: {ctypes.get_last_error()}")
        return rect.right / 4, rect.bottom / 8
    finally:
        if handle:
            user.DestroyWindow(handle)
        if context:
            user.SetThreadDpiAwarenessContext(context)


def rounded(value):
    # Match MulDiv's nearest-integer conversion, including negative positions.
    return math.floor(value + 0.5) if value >= 0 else -math.floor(-value + 0.5)


def safe_name(value):
    name = re.sub(r"[^A-Za-z0-9_]", "_", str(value))
    return "widget_" + name if not name or name[0].isdigit() else name


def unique_name(requested, used):
    base = safe_name(requested)
    name, suffix = base, 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def prop(widget, name, tag, value, dynamic=False):
    text = "true" if value is True else "false" if value is False else str(value)
    if INVALID_XML.search(text):
        raise ConversionError("resource contains text XML cannot represent")
    previous = widget.find(f"property[@name='{name}']")
    if previous is not None:widget.remove(previous)
    attributes = {"name": name}
    if dynamic:
        attributes["stdset"] = "0"
    item = ET.SubElement(widget, "property", attributes)
    child = ET.SubElement(item, tag)
    if tag == "string" and dynamic:child.set("notr", "true")
    child.text = text
    return child


def serialize(root):
    ET.indent(root, space="  ")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True).replace(b"\r", b"&#13;") + b"\n"
    try:
        ET.fromstring(data)
    except ET.ParseError as exc:
        raise ConversionError(f"resource contains text XML cannot represent: {exc}") from exc
    return data


def set_icon(widget, qrc, path):
    icon = prop(widget, "icon", "iconset", "")
    icon.set("resource", qrc)
    ET.SubElement(icon, "normaloff").text = path


def set_size(widget, name, width, height):
    size = prop(widget, name, "size", "")
    ET.SubElement(size, "width").text = str(width)
    ET.SubElement(size, "height").text = str(height)


def report_value(value):
    if isinstance(value, bytes):
        return {"data_base64": base64.b64encode(value).decode("ascii")}
    return asdict(value)


def map_control(control, warn, overrides):
    native, style = str(control.native_class).lower(), control.style
    properties = control.properties

    def setp(name, tag, value):
        properties[name] = (tag, value)

    if native == "button":
        button_type = style & 0xF
        control.qt_type = {2: "QCheckBox", 3: "QCheckBox", 4: "QRadioButton",
                           5: "QCheckBox", 6: "QCheckBox", 7: "QGroupBox", 9: "QRadioButton"}.get(button_type, "QPushButton")
        control.kind = {"QGroupBox": "GROUPBOX", "QCheckBox": "CHECKBOX", "QRadioButton": "RADIOBUTTON"}.get(control.qt_type, "DEFPUSHBUTTON" if button_type == 1 else "PUSHBUTTON")
        if control.qt_type == "QPushButton":
            setp("default", "bool", button_type == 1)
            setp("autoDefault", "bool", True)
        if button_type in {5, 6}:
            setp("tristate", "bool", True)
        if control.qt_type == "QRadioButton":
            setp("autoExclusive", "bool", False)
        if style & 0x8000 and control.qt_type == "QPushButton":
            setp("flat", "bool", True)
        if button_type in {8, 11} or style & 0x1000:
            warn("BUTTON_CUSTOM_PAINT", control, "Owner-draw/pushlike button needs custom Qt painting or behavior.")
        if button_type >= 12:
            warn("BUTTON_SPECIAL_TYPE", control, "Split/command-link button requires a specialized Qt widget.")
        if button_type in {2, 4, 5}:
            warn("BUTTON_MANUAL_STATE", control, "Manual Win32 check/radio state is application-controlled; port its click handler.")
        if style & (0x20 | 0x2000):
            warn("BUTTON_TEXT_LAYOUT", control, "BS_LEFTTEXT/BS_MULTILINE requires explicit Qt text/indicator layout.")
    elif native in {"edit", "richedit", "richedit20a", "richedit20w", "richedit50w", "richedit50a"}:
        control.kind = "EDITTEXT"
        rich = native != "edit"
        control.qt_type = "QTextEdit" if rich else "QPlainTextEdit" if style & 4 else "QLineEdit"
        setp("readOnly", "bool", bool(style & 0x800))
        if control.qt_type == "QLineEdit":
            if style & 0x20:
                setp("echoMode", "enum", "QLineEdit::Password")
            setp("alignment", "set", "Qt::AlignRight" if style & 2 else "Qt::AlignHCenter" if style & 1 else "Qt::AlignLeft")
            setp("frame", "bool", bool(style & 0x800000 or control.exstyle & 0x200))
            if style & 0x2000:
                setp("inputMethodHints", "set", "Qt::ImhDigitsOnly")
                warn("EDIT_NUMBER_VALIDATOR", control, "ES_NUMBER requires a Qt validator; inputMethodHints alone does not reject pasted text.")
        else:
            setp("lineWrapMode", "enum", control.qt_type + ("::NoWrap" if style & 0x80 else "::WidgetWidth"))
        if style & 0x18:
            warn("EDIT_CASE_CONVERSION", control, "ES_UPPERCASE/ES_LOWERCASE needs an input handler.")
        if rich:
            warn("RICHEDIT_RUNTIME", control, "RichEdit formatting, OLE and notifications require application code.")
    elif native == "static":
        static_type = style & 0x1F
        control.kind = "CTEXT" if static_type == 1 else "RTEXT" if static_type == 2 else "LTEXT"
        if static_type in {4, 5, 6, 7, 8, 9, 16, 17, 18}:
            control.qt_type = "QFrame"
            if static_type in {16, 17, 18}:
                setp("frameShape", "enum", {16: "QFrame::HLine", 17: "QFrame::VLine", 18: "QFrame::Box"}[static_type])
                setp("frameShadow", "enum", "QFrame::Sunken")
            else:
                shade = {4: 0, 5: 128, 6: 255, 7: 0, 8: 128, 9: 255}[static_type]
                color = f"rgb({shade}, {shade}, {shade})"
                setp("styleSheet", "string", f"background-color: {color};" if static_type <= 6 else f"border: 1px solid {color};")
        else:
            control.qt_type = "QLabel"
            setp("textFormat", "enum", "Qt::PlainText")
            setp("wordWrap", "bool", static_type not in {11, 12})
            horizontal = "Qt::AlignHCenter" if static_type == 1 else "Qt::AlignRight" if static_type == 2 else "Qt::AlignLeft"
            setp("alignment", "set", horizontal + ("|Qt::AlignVCenter" if style & 0x200 else "|Qt::AlignTop"))
            if static_type in {3, 14}:
                control.kind = "ICON" if static_type == 3 else "BITMAP"
            elif static_type not in {0, 1, 2, 11, 12}:
                warn("STATIC_CUSTOM_PAINT", control, f"Static type {static_type} requires custom painting.")
    elif native == "combobox":
        control.qt_type, control.kind = "QComboBox", "COMBOBOX"
        setp("editable", "bool", style & 3 != 3)
        if style & 3 == 1:
            warn("COMBO_SIMPLE", control, "CBS_SIMPLE has a permanently visible list; QComboBox uses a popup.")
        if style & (0x10 | 0x20):
            warn("COMBO_OWNERDRAW", control, "Owner-draw combo requires a Qt delegate.")
        if style & 0x100:
            warn("COMBO_SORT", control, "CBS_SORT needs sorting of items added later by application code.")
    elif native == "listbox":
        control.qt_type, control.kind = "QListWidget", "LISTBOX"
        setp("selectionMode", "enum", "QAbstractItemView::ExtendedSelection" if style & 0x800 else "QAbstractItemView::MultiSelection" if style & 8 else "QAbstractItemView::SingleSelection")
        setp("sortingEnabled", "bool", bool(style & 2))
        if style & 0x30:
            warn("LIST_OWNERDRAW", control, "Owner-draw list requires a Qt delegate.")
        if style & 0x200:
            warn("LIST_MULTICOLUMN", control, "LBS_MULTICOLUMN layout requires application code.")
    elif native == "syslistview32":
        control.qt_type = "QTreeView" if style & 3 == 1 else "QListView"
        control.kind = "LISTBOX"
        setp("selectionMode", "enum", "QAbstractItemView::SingleSelection" if style & 4 else "QAbstractItemView::ExtendedSelection")
        setp("editTriggers", "set", "QAbstractItemView::EditKeyPressed" if style & 0x200 else "QAbstractItemView::NoEditTriggers")
        if control.qt_type == "QTreeView":
            setp("rootIsDecorated", "bool", False)
            setp("headerHidden", "bool", bool(style & 0x4000))
        else:
            setp("viewMode", "enum", "QListView::IconMode" if style & 3 in {0, 2} else "QListView::ListMode")
        if style & 0x1400:
            warn("LISTVIEW_OWNER_DATA", control, "Owner-draw/owner-data list view requires a Qt model or delegate.")
    elif native == "systreeview32":
        control.qt_type, control.kind = "QTreeView", "TREEVIEW"
        setp("headerHidden", "bool", True)
        setp("rootIsDecorated", "bool", bool(style & 1))
        setp("editTriggers", "set", "QAbstractItemView::EditKeyPressed" if style & 8 else "QAbstractItemView::NoEditTriggers")
        if style & 0x100:
            warn("TREE_CHECKBOX_MODEL", control, "TVS_CHECKBOXES requires check states in the Qt item model.")
    elif native == "msctls_trackbar32":
        control.qt_type, control.kind = "QSlider", "SLIDER"
        vertical = bool(style & 2)
        setp("orientation", "enum", "Qt::Vertical" if vertical else "Qt::Horizontal")
        ticks = "NoTicks" if style & 0x10 else "TicksBothSides" if style & 8 else "TicksAbove" if style & 4 else "TicksBelow"
        setp("tickPosition", "enum", "QSlider::" + ticks)
        setp("maximum", "number", 100)
    elif native == "msctls_progress32":
        control.qt_type, control.kind = "QProgressBar", "PROGRESS"
        setp("orientation", "enum", "Qt::Vertical" if style & 4 else "Qt::Horizontal")
        setp("textVisible", "bool", False)
        setp("value", "number", 0)
        if style & 8:
            setp("minimum", "number", 0)
            setp("maximum", "number", 0)
    elif native == "scrollbar":
        control.qt_type, control.kind = ("QSizeGrip", "SIZEGRIP") if style & 0x18 else ("QScrollBar", "SCROLLBAR")
        if control.qt_type == "QScrollBar":
            setp("orientation", "enum", "Qt::Vertical" if style & 1 else "Qt::Horizontal")
    elif native == "msctls_updown32":
        control.qt_type, control.kind = "QSpinBox", "SPIN"
        setp("maximum", "number", 100)
        setp("wrapping", "bool", bool(style & 1))
        warn("SPIN_BUDDY", control, "Native up-down buddy coupling and arrow-only geometry need application code.")
    elif native == "systabcontrol32":
        control.qt_type, control.kind = "QTabWidget", "TAB"
        position = ("East" if style & 2 else "West") if style & 0x80 else ("South" if style & 2 else "North")
        setp("tabPosition", "enum", "QTabWidget::" + position)
        if style & 0x300:
            warn("TAB_BUTTON_STYLE", control, "Button-style/multiselect tabs require custom Qt behavior.")
    elif native == "sysdatetimepick32":
        control.qt_type, control.kind = ("QTimeEdit", "TIME") if style & 9 == 9 else ("QDateEdit", "DATE")
        if control.qt_type == "QDateEdit":
            setp("calendarPopup", "bool", not bool(style & 1))
        if style & 2:
            warn("DATE_NONE", control, "DTS_SHOWNONE nullable date needs custom Qt behavior.")
    elif native == "sysmonthcal32":
        control.qt_type, control.kind = "QCalendarWidget", "CALENDAR"
        if style & 2:
            warn("CALENDAR_MULTISELECT", control, "QCalendarWidget does not support MCS_MULTISELECT date ranges.")
    elif native == "syslink":
        control.qt_type, control.kind = "QLabel", "LINK"
        setp("textFormat", "enum", "Qt::RichText")
        setp("textInteractionFlags", "set", "Qt::TextBrowserInteraction")
        setp("openExternalLinks", "bool", False)
    else:
        control.qt_type, control.kind = overrides.get(native, "QWidget"), "CUSTOM"
        if native not in overrides:
            warn("CUSTOM_CONTROL", control, f"Native class {control.native_class!r} retained as QWidget; supply --widget-map or implement a Qt widget.")
    if native in overrides:
        requested = overrides[native]
        if requested != control.qt_type:
            control.properties.clear()
        control.qt_type = requested
    if control.creation_data:
        warn("CONTROL_CREATION_DATA", control, "Control-specific creation data retained as RcCreationData; Qt initialization code is required.")
    setp("enabled", "bool", not bool(style & 0x08000000))
    if not style & 0x10000000:
        setp("visible", "bool", False)
    focus = "Qt::StrongFocus" if style & 0x10000 else "Qt::NoFocus" if control.qt_type in {"QLabel", "QFrame", "QGroupBox", "QWidget", "QProgressBar", "QSizeGrip"} else "Qt::ClickFocus"
    setp("focusPolicy", "enum", focus)
    if control.exstyle & (0x400000 | 0x2000):
        setp("layoutDirection", "enum", "Qt::RightToLeft")


def read_dlginit(resource, dialog, encoding, warn):
    reader = Reader(resource.data)
    by_id = defaultdict(list)
    for control in dialog.children:
        by_id[control.numeric_id].append(control)
    while reader.pos < len(reader.data):
        identifier = reader.word()
        if not identifier:
            return
        message, length = reader.unpack("HI")
        data = reader.take(length)
        controls = by_id[identifier]
        if len(controls) != 1:
            warn("DLGINIT_TARGET", dialog, f"DLGINIT target {identifier} is missing or ambiguous.")
            continue
        control = controls[0]
        if message in {0x143, 0x180, 0x401, 0x403} and control.qt_type in {"QComboBox", "QListWidget"}:
            try:
                value = data.rstrip(b"\0").decode(encoding)
            except UnicodeDecodeError as exc:
                raise ConversionError(f"DLGINIT {identifier}: cannot decode with {encoding}; use --dlginit-encoding") from exc
            control.items.append(value)
        else:
            warn("DLGINIT_MESSAGE", control, f"DLGINIT message 0x{message:04x} needs application code; raw resource is preserved in the report.")
    raise ConversionError(f"DLGINIT {resource.name} is missing its terminating zero ID")


class Assets:
    def __init__(self, resources, stem, warn):
        self.resources, self.warn = resources, warn
        self.icon_folder = "assets/icons"
        self.image_folder = "assets/images"
        self.qrc_name = "resources.qrc"
        self.ui_qrc_name = "../../" + self.qrc_name
        self.resource_prefix = "/rc/" + safe_name(stem)
        self.files = {}
        self.contents = {}
        self.bitmaps = {}

    def url(self, relative):
        return ":" + self.resource_prefix + "/" + relative

    def add(self, relative, data):
        key = (posixpath.splitext(relative)[1].lower(), data)
        relative = self.contents.setdefault(key, relative)
        self.files[relative] = data
        return self.url(relative)

    def bitmap(self, resource):
        from rc_commands import decode_dib
        data = resource.data[14:] if resource.data.startswith(b"BM") else resource.data
        if data not in self.bitmaps:
            self.bitmaps[data] = decode_dib(data)
        return self.bitmaps[data]

    def prune(self, outputs):
        used = set()
        for filename, root in outputs.items():
            if not filename.endswith(".ui"):continue
            nodes = list(root.iter())
            used.update(node.text for node in nodes if node.text)
            if any(node.get("resource") == self.ui_qrc_name for node in nodes):
                resources = ET.Element("resources")
                ET.SubElement(resources,"include",location=self.ui_qrc_name)
                index = next((i for i,node in enumerate(root) if node.tag in {"connections","buttongroups"}),len(root))
                root.insert(index,resources)
        self.files = {name: data for name, data in self.files.items() if self.url(name) in used}
        self.contents.clear()
        self.bitmaps.clear()

    def find(self, resource_type, identifier, language):
        choices = [item for item in self.resources if item.type == resource_type and item.name == identifier]
        if not choices:
            return None
        return next((item for item in choices if item.language == language),
                    next((item for item in choices if item.language == 0), choices[0]))

    def image(self, control, language, target_size=None):
        icon = control.kind == "ICON" or str(control.native_class).lower() == "button" and control.style & 0x40
        bitmap = control.kind == "BITMAP" or str(control.native_class).lower() == "button" and control.style & 0x80
        if not icon and not bitmap:
            return None
        identifier = control.text
        if isinstance(identifier, str) and re.fullmatch(r"#?\d+", identifier):
            identifier = int(identifier.lstrip("#"))
        resource = self.find(14 if icon else 2, identifier, language)
        if resource is None:
            self.warn("IMAGE_MISSING", control, f"Image resource {identifier!r} was not found; reference is preserved as RcTextResource.")
            return None
        if resource.language not in {0, language}:
            self.warn("IMAGE_LANGUAGE", control, f"Using image language 0x{resource.language:04x}; verify language fallback.")
        variant = ""
        try:
            if icon:
                count = len(resource.data)
                if not count:
                    raise ConversionError("invalid icon group")
                images = []
                for width, height, colors, zero, planes, bits, size, image_id in resource.data:
                    image = self.find(3, image_id, resource.language)
                    if image is None or len(image.data) != size:
                        raise ConversionError(f"missing/truncated icon image {image_id}")
                    images.append((width, height, colors, zero, planes, bits, image.data))
                if control.qt_type == "QLabel":
                    fit = control.style & SS_REALSIZECONTROL and not control.style & SS_CENTERIMAGE
                    if fit and target_size is not None:
                        requested, origin = target_size, "source"
                        reason = "SS_REALSIZECONTROL fits the RC control rectangle converted to logical pixels."
                    elif control.style & SS_REALSIZEIMAGE:
                        requested = (images[0][0] or 256, images[0][1] or 256)
                        origin = "source"
                        reason = "SS_REALSIZEIMAGE uses the original resource dimensions; equal-size frames may improve color depth."
                    else:
                        requested, origin = STATIC_ICON_SIZE, "platform_default"
                        reason = "SS_ICON uses the system-large icon size at the exporter's 96-DPI baseline; RC width/height do not select its frame."
                    best = min(range(count), key=lambda i: (
                        ((images[i][0] or 256) - requested[0]) ** 2 + ((images[i][1] or 256) - requested[1]) ** 2,
                        -icon_frame_depth(images[i])))
                    images.insert(0, images.pop(best))
                    variant = f"_frame_{best}" if best else ""
                    selected = (images[0][0] or 256, images[0][1] or 256)
                    control.decisions.append(conversion_decision("icon_target_size", requested, origin, reason))
                    control.decisions.append(conversion_decision("icon_frame", {
                        "index": best, "size": selected, "depth": icon_frame_depth(images[0]), "frames_retained": count},
                        "source" if selected == requested else "approximation",
                        "Best color depth at the requested size." if selected == requested else
                        "The ICO has no exact-size frame; the nearest available frame is used."))
                else:
                    control.decisions.append(conversion_decision("icon_frame", None, "qt_runtime",
                        "QIcon selects a frame at display time; the original ICO frame order is retained."))
                entries, blobs, offset = [], [], 6 + 16 * count
                for width, height, colors, zero, planes, bits, blob in images:
                    size = len(blob)
                    entries.append(struct.pack("<BBBBHHII", width, height, colors, zero, planes, bits, size, offset))
                    blobs.append(blob)
                    offset += size
                data = struct.pack("<HHH", 0, 1, count) + b"".join(entries + blobs)
                extension = "ico"
            else:
                dib = resource.data
                reader = Reader(dib)
                header_size = reader.unpack("I")[0]
                if header_size not in {40, 108, 124} or len(dib) < header_size:
                    raise ConversionError(f"unsupported bitmap header {header_size}")
                bit_count = struct.unpack_from("<H", dib, 14)[0]
                compression = struct.unpack_from("<I", dib, 16)[0]
                colors = struct.unpack_from("<I", dib, 32)[0]
                if compression not in {0, 3, 6}:
                    raise ConversionError(f"unsupported bitmap compression {compression}")
                palette = (colors or (1 << bit_count if bit_count <= 8 else 0)) * 4
                masks = (12 if compression == 3 else 16 if compression == 6 else 0) if header_size == 40 else 0
                offset = 14 + header_size + palette + masks
                if offset > len(dib) + 14:
                    raise ConversionError("truncated bitmap palette")
                data = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib
                extension = "bmp"
        except (ConversionError, struct.error) as exc:
            self.warn("IMAGE_FORMAT", control, str(exc))
            return None
        # Equal frame selections share an asset; different selections cannot
        # overwrite one another even when controls reference the same ICO.
        filename = f"{14 if icon else 2}_{safe_name(identifier)}_{resource.language:04x}{variant}.{extension}"
        relative = f"{self.icon_folder if icon else self.image_folder}/{filename}"
        return self.add(relative, data)

    def qrc(self):
        root = ET.Element("RCC")
        group = ET.SubElement(root, "qresource", prefix=self.resource_prefix)
        for filename in sorted(self.files):
            ET.SubElement(group, "file").text = filename
        return serialize(root)


def emit_dialog(dialog, scale, assets, warn, commands=None):
    root = ET.Element("ui", version="4.0")
    ET.SubElement(root, "class").text = dialog.name
    child_dialog = bool(dialog.style & (0x40000000 | 0x400))
    widget = ET.SubElement(root, "widget", {"class": "QWidget" if child_dialog else "QDialog", "name": dialog.name})

    def geometry(parent, control, combo_height=False):
        x, y, w, h = control.rect
        if combo_height:
            # Win32 combo cy includes the dropdown. Qt geometry is the closed
            # editor; retain full source dimensions in RcRectDlu.
            line = (dialog.font[0] * 96 / 72 if dialog.font else 13) + 8
            h = min(h * scale[1], line) / scale[1]
        pixels = tuple(rounded(value) for value in (x * scale[0], y * scale[1], w * scale[0], h * scale[1]))
        rectangle = prop(parent, "geometry", "rect", "")
        for key, value in zip(("x", "y", "width", "height"), pixels):
            ET.SubElement(rectangle, key).text = str(value)
        return rectangle, pixels[2:]

    def native_properties(parent, control):
        for name, value in (("RcId", control.numeric_id), ("RcClass", control.native_class),
                            ("RcStyle", f"0x{control.style:08x}"), ("RcExStyle", f"0x{control.exstyle:08x}"),
                            ("RcHelpId", control.help_id), ("RcRectDlu", ",".join(map(str, control.rect)))):
            prop(parent, name, "string", value, True)
        if control.creation_data:
            prop(parent, "RcCreationData", "string", control.creation_data.hex(), True)

    geometry(widget, dialog)
    native_properties(widget, dialog)
    prop(widget, "RcLanguage", "string", f"0x{dialog.language:04x}", True)
    if dialog.text:
        prop(widget, "windowTitle", "string", dialog.text)
    if dialog.font:
        pointsize, family, weight, italic, charset = dialog.font
        # Windows aliases are registry mappings; use their current real family
        # for Qt, which does not resolve the MS Shell Dlg aliases itself.
        if os.name == "nt" and family.lower() in {"ms shell dlg", "ms shell dlg 2"}:
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\FontSubstitutes") as key:
                    family = winreg.QueryValueEx(key, family)[0]
            except OSError:
                warn("FONT_ALIAS", dialog, f"Unable to resolve font alias {family!r}.")
        font = prop(widget, "font", "font", "")
        weights = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Normal", 500: "Medium",
                   600: "DemiBold", 700: "Bold", 800: "ExtraBold", 900: "Black"}
        mapped_weight = min(weights, key=lambda value: abs(value - (weight or 400)))
        if weight and mapped_weight != weight:
            warn("FONT_WEIGHT", dialog, f"Nonstandard font weight {weight} approximated as {weights[mapped_weight]}.")
        for key, value in (("family", family), ("pointsize", pointsize), ("fontweight", weights[mapped_weight]), ("italic", "true" if italic else "false")):
            ET.SubElement(font, key).text = str(value)
        prop(widget, "RcFont", "string", json.dumps(dialog.font, ensure_ascii=False), True)
    if dialog.exstyle & (0x400000 | 0x2000):
        prop(widget, "layoutDirection", "enum", "Qt::RightToLeft")
    if not child_dialog:
        flags = ["Qt::Dialog", "Qt::CustomizeWindowHint"]
        for bit, flag in ((0xC00000, "Qt::WindowTitleHint"), (0x80000, "Qt::WindowSystemMenuHint"),
                          (0x20000, "Qt::WindowMinimizeButtonHint"), (0x10000, "Qt::WindowMaximizeButtonHint")):
            if dialog.style & bit:
                flags.append(flag)
        if dialog.style & 0x80000:
            flags.append("Qt::WindowCloseButtonHint")
        prop(widget, "windowFlags", "set", "|".join(flags))
        if not dialog.style & 0x40000:
            for name in ("minimumSize", "maximumSize"):
                set_size(widget, name, rounded(dialog.rect[2] * scale[0]), rounded(dialog.rect[3] * scale[1]))
    if dialog.menu:
        prop(widget, "RcMenu", "string", dialog.menu, True)
    if dialog.native_class:
        warn("DIALOG_CLASS", dialog, f"Custom dialog class {dialog.native_class!r} requires application code.")

    tabstops, button_groups, connections = [], defaultdict(list), []
    group_index = 0
    for index, control in enumerate(dialog.children):
        if control.style & 0x20000:
            group_index += 1
        child = ET.SubElement(widget, "widget", {"class": control.qt_type, "name": control.name})
        rectangle, image_size = geometry(child, control, control.qt_type == "QComboBox" and control.style & 3 != 1)
        native_properties(child, control)
        for name, (tag, value) in control.properties.items():
            prop(child, name, tag, value)
        image = assets.image(control, dialog.language, image_size)
        if image:
            if control.qt_type in {"QPushButton", "QCheckBox", "QRadioButton"}:
                set_icon(child, assets.ui_qrc_name, image)
            elif control.qt_type == "QLabel":
                pixmap = prop(child, "pixmap", "pixmap", image)
                pixmap.set("resource", assets.ui_qrc_name)
                if control.kind == "ICON":
                    target = next(d["value"] for d in control.decisions if d["property"] == "icon_target_size")
                    centered = bool(control.style & SS_CENTERIMAGE)
                    fit = bool(control.style & SS_REALSIZECONTROL) and not centered
                    if not centered and not fit:
                        rectangle.find("width").text, rectangle.find("height").text = map(str, target)
                    prop(child, "alignment", "set", "Qt::AlignCenter" if centered else "Qt::AlignLeft|Qt::AlignTop")
                    prop(child, "scaledContents", "bool", not centered)
            else:
                warn("IMAGE_TARGET", control, f"Cannot apply image to {control.qt_type} automatically.")
        image_control = control.kind in {"ICON", "BITMAP"} or str(control.native_class).lower() == "button" and control.style & 0xC0
        if image_control:
            prop(child, "RcTextResource", "string", control.text, True)
        elif control.text != "":
            text = str(control.text)
            if control.qt_type in {"QTextEdit", "QPlainTextEdit"}:
                text_property, dynamic = "plainText", False
            elif control.qt_type == "QGroupBox":
                text_property, dynamic = "title", False
            else:
                text_property = "text"
                dynamic = control.qt_type not in {"QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QLineEdit"}
            prop(child, text_property, "string", text, dynamic)
        if control.qt_type == "QLabel" and isinstance(control.text, str) and "&" in control.text and not control.style & 0x80:
            buddy = next((item for item in dialog.children[index + 1:] if item.style & 0x10000), None)
            if buddy:
                prop(child, "buddy", "cstring", buddy.name)
        if control.qt_type == "QRadioButton" and control.style & 0xF == 9:
            group = f"{dialog.name}_radioGroup_{group_index}"
            button_groups[group].append(control.name)
            attribute = ET.SubElement(child, "attribute", name="buttonGroup")
            ET.SubElement(attribute, "string", notr="true").text = group
        if control.style & 0x10000:
            tabstops.append(control.name)
        for item in control.items:
            entry = ET.SubElement(child, "item")
            prop(entry, "text", "string", item)
        if not child_dialog and control.qt_type == "QPushButton" and control.numeric_id in {1, 2}:
            connections.append((control.name, "accept()" if control.numeric_id == 1 else "reject()"))
    if tabstops:
        tabs = ET.SubElement(root, "tabstops")
        for name in tabstops:
            ET.SubElement(tabs, "tabstop").text = name
    if connections:
        connection_list = ET.SubElement(root, "connections")
        for sender, slot in connections:
            connection = ET.SubElement(connection_list, "connection")
            for key, value in (("sender", sender), ("signal", "clicked()"), ("receiver", dialog.name), ("slot", slot)):
                ET.SubElement(connection, key).text = value
    if button_groups:
        groups = ET.SubElement(root, "buttongroups")
        for name in button_groups:
            group = ET.SubElement(groups, "buttongroup", name=name)
            prop(group, "exclusive", "bool", True)
    if dialog.menu and commands is not None:
        commands.attach_dialog(root, dialog)
    return root


def convert_mfc(rc_path, output_dir=None, encoding=None, *,
                include_dirs=(), defines=(), undefines=(), scale=None, language=None,
                strict=False, widget_map=(), dlginit_encoding=None,
                ribbon_resources=None, ribbon_target=None, source_language=None, separate_languages=False):
    path = Path(rc_path).resolve()
    if scale is not None and (not math.isfinite(scale) or scale <= 0):
        raise ConversionError("--scale must be a positive finite number")
    if language is not None and not 0 <= language <= 0xFFFF:
        raise ConversionError("--language must fit in 16 bits")
    if source_language is not None and not 0 <= source_language <= 0xFFFF:
        raise ConversionError("--source-language must fit in 16 bits")
    if path.suffix.lower() not in {".rc", ".rc2", ".mfcribbon-ms"}:
        raise ConversionError(f"unsupported input {path.suffix!r}; expected .rc, .rc2 or .mfcribbon-ms")
    overrides = {}
    source_warnings=[];source_metadata=None
    def load_script(source):
        nonlocal source_metadata
        if source.suffix.lower() not in {".rc", ".rc2"}:
            raise ConversionError(f"unsupported RC source {source.suffix!r}; expected .rc or .rc2")
        from rc_parser import parse_project
        try:
            parsed,ids,warnings,source_metadata=parse_project(source,include_dirs,defines,undefines,encoding)
        except (ConversionError,OSError,UnicodeError,LookupError) as exc:
            match=re.match(r"^(.*?):(\d+):\s*(.*)",str(exc),re.S)
            file,line,message=(match[1],int(match[2]),match[3]) if match else (str(source),1,str(exc))
            source_warnings.append(diagnostic("error","RC_SOURCE",file,line,source.stem,"SOURCE",message))
            source_metadata={"resource_origins":[],"errors":list(source_warnings)}
            return [],{}
        source_warnings.extend(warnings)
        return parsed,ids
    supported = {"QWidget", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox", "QLineEdit", "QPlainTextEdit",
                 "QTextEdit", "QLabel", "QFrame", "QComboBox", "QListWidget", "QListView", "QTreeView", "QSlider",
                 "QProgressBar", "QScrollBar", "QSizeGrip", "QSpinBox", "QTabWidget", "QDateEdit", "QTimeEdit", "QCalendarWidget"}
    for item in widget_map:
        native, separator, qt_type = item.partition("=")
        if not separator or not native or qt_type not in supported:
            raise ConversionError(f"invalid widget mapping {item!r}; use NATIVE=QT with a supported built-in widget")
        overrides[native.lower()] = qt_type
    from mfc_ribbon import is_ribbon, standalone, xml_symbols, RibbonResources
    if ribbon_resources is not None and path.suffix.lower() != ".mfcribbon-ms":
        raise ConversionError("--ribbon-resources is only used with a standalone .mfcribbon-ms input")
    if path.suffix.lower() == ".mfcribbon-ms":
        ribbon = standalone(path, language if language is not None else 0)
        if ribbon_resources is not None:
            bundle = Path(ribbon_resources).resolve()
            resources, symbols = load_script(bundle)
            matching = [item for item in resources if is_ribbon(item) and item.data == ribbon.data
                        and (language is None or item.language in {0, language})]
            resources = [item for item in resources if not is_ribbon(item)] + (matching or [ribbon])
        else:
            resources, symbols = [ribbon], {}
    else:
        resources, symbols = load_script(path)
    known_names = {name: value for value, names in symbols.items() for name in names}
    ribbon_ids = xml_symbols(item.ribbon[0] for item in resources
                            if is_ribbon(item) and (language is None or item.language in {0, language}))
    for value, names in ribbon_ids.items():
        for name in names:
            if name not in known_names or known_names[name] == value:
                symbols.setdefault(value, [])
                if name not in symbols[value]:
                    symbols[value].append(name)

    def name_for(value, prefix):
        if isinstance(value, str):
            return value
        choices = sorted(symbols.get(value, []))
        return next((item for item in choices if item.startswith(prefix)),
                    "IDC_STATIC" if value == -1 else choices[0] if choices else STANDARD_IDS.get(value, prefix + str(value)))

    errors = [item for item in source_warnings if item.get("severity")=="error"]
    warnings = [item for item in source_warnings if item.get("severity")!="error"]
    origins = source_metadata.get("resource_origins",[]) if source_metadata is not None else [
        {"file":str(path),"line":1,"type":r.type,"name":r.name,"language":r.language} for r in resources]
    active_resource = None
    def warn(code, control, message):
        severe=code in {"IMAGE_MISSING","IMAGE_FORMAT","TOOLBAR_BITMAP_FORMAT","RIBBON_IMAGE","RIBBON_UNRESOLVED_ID","RESOURCE_EXPORT"}
        origin=next((item for item in origins if active_resource is not None and
                     (item["type"],item["name"],item["language"])==(active_resource.type,active_resource.name,active_resource.language)),{})
        if not origin and code.startswith("RIBBON"):
            origin=next((item for item in origins if item["type"] in {28,"RT_RIBBON_XML","RIBBON_XML"}),{})
        item=diagnostic("error" if severe else "warning",code,origin.get("file",str(path)),origin.get("line",1),
                        origin.get("name",control.identifier if control else path.stem),origin.get("type"),message,
                        control.identifier if control else None)
        (errors if severe else warnings).append(item)

    assets = Assets(resources, path.stem, warn)
    from rc_commands import CommandResources
    commands = CommandResources(resources, name_for, assets, warn, language)
    ribbons = RibbonResources(resources, symbols, assets, warn, language, ribbon_target, ribbon_ids)
    if ribbons.frames:
        commands.ribbon = ribbons
    selected = [item for item in resources if item.type == 5 and (language is None or item.language == language)]
    if not selected and not commands.families() and not commands.strings and not errors:
        errors.append(diagnostic("error","NO_RESOURCES",path,1,path.stem,"SOURCE",
                                 "no supported resources found for the requested language"))
    dialogs = [item.data for item in selected]
    filenames, outputs, report_dialogs = set(), {}, []
    counts = Counter(item.numeric_id for item in dialogs)
    font_scales = {}
    if dlginit_encoding is None:
        # DLGINIT byte strings are independent of the RC source's encoding.
        dlginit_encoding = "mbcs" if os.name == "nt" else "cp1252"
    codecs.lookup(dlginit_encoding)
    for active_resource, dialog in zip(selected, dialogs):
        used_names = set()
        for control in (dialog, *dialog.children):
            control.identifier = name_for(control.numeric_id, "IDD_" if control is dialog else "IDC_")
            control.name = unique_name(control.identifier, used_names)
        for control in dialog.children:
            map_control(control, warn, overrides)
        initializer = assets.find(240, dialog.numeric_id, dialog.language)
        if initializer:
            if initializer.language not in {0, dialog.language}:
                warn("DLGINIT_LANGUAGE", dialog, f"Using DLGINIT language 0x{initializer.language:04x}; verify language fallback.")
            read_dlginit(initializer, dialog, dlginit_encoding, warn)
        if scale is not None:factors=(scale,scale)
        elif os.name=="nt":
            key = (dialog.font, dialog.style & 0x48)
            if key not in font_scales:font_scales[key] = windows_scale(dialog)
            factors = font_scales[key]
        else:
            factors=(1.5,1.5)
            warn("RC_DLU_APPROXIMATION",dialog,"Windows font metrics are unavailable; DLU uses 1.5 logical pixels. Pass --scale to select a different factor.")
        dialog.decisions.append(conversion_decision("dlu_scale", factors,
            "user_override" if scale is not None else "platform_default" if os.name == "nt" else "approximation",
            "Explicit --scale." if scale is not None else "Windows font metrics at 96 DPI." if os.name == "nt" else
            "Windows font metrics are unavailable; a uniform scale is used."))
        filename = "ui/dialogs/" + safe_name(dialog.identifier) + (f"_{dialog.language:04x}" if counts[dialog.numeric_id] > 1 else "") + ".ui"
        if filename.lower() in filenames:
            raise ConversionError(f"duplicate UI filename: {filename}")
        filenames.add(filename.lower())
        try:
            outputs[filename] = emit_dialog(dialog, factors, assets, warn, commands)
        except (ConversionError,UnicodeError,struct.error) as exc:
            warn("RESOURCE_EXPORT",dialog,str(exc))
            continue
        report_dialogs.append({"resource": dialog.numeric_id, "identifier": dialog.identifier,
                               "language": dialog.language, "file": filename, "scale": factors, "decisions": dialog.decisions,
                               "control_count": len(dialog.children),
                               "controls": [{"id": item.numeric_id, "identifier": item.identifier, "name": item.name,
                                             "native_class": item.native_class, "qt_class": item.qt_type,
                                             "rect_dlu": item.rect, "style": item.style, "exstyle": item.exstyle,
                                             "help_id": item.help_id, "text": item.text,
                                             "creation_data_hex": item.creation_data.hex(), "decisions": item.decisions} for item in dialog.children]})
    active_resource=None
    outputs.update(commands.export_frames(outputs))
    string_outputs = {"strings/"+name:data for name,data in commands.export_strings(path.stem, source_language).items()}
    outputs.update(string_outputs)
    from rc_i18n import merge_ui_languages
    translations = merge_ui_languages(outputs, report_dialogs, commands, path.stem,
                                     separate=separate_languages, source_language=source_language)
    ui_names = list(dict.fromkeys(record["file"] for record in report_dialogs + commands.forms))
    for variant in translations["separate_forms"]:
        origin = next((o for o in origins if o["name"] == variant["resource"] and o["language"] == variant["language"]
                       and (o["type"] == 5 if variant["kind"] == "dialog" else o["type"] in {4,9,28,241})), {})
        warnings.append(diagnostic("warning", "I18N_VARIANT", origin.get("file",path), origin.get("line",1),
                                   variant["resource"], origin.get("type",variant["kind"]),
                                   f"Language {variant['language']:#06x} stays in {variant['file']}: {variant['reason']}"))
    outside = sorted({str(item.type) for item in resources if item.type not in {0, 2, 3, 4, 5, 6, 9, 14, 240, 241}
                      and not is_ribbon(item) and (item.type, item.name, item.language) not in ribbons.used_images})
    if outside:
        warn("NON_DIALOG_RESOURCES", None, "Unsupported resource types are retained in the report: " + ", ".join(outside))
    if strict and (warnings or errors):
        raise ConversionError("strict conversion stopped before writing output:\n" + "\n".join(
            f"{item['severity']} [{item['code']}]: {item['message']}" for item in errors+warnings))
    report = {"source": str(path), "backend": "python-rc" if source_metadata is not None else "ribbon-xml", "dialogs": report_dialogs,
              "command_forms": commands.forms, "dialog_menus": commands.dialog_bindings,
              "converted_command_resources": commands.decoded, "string_files": list(string_outputs),
              "translations": translations,
              "warnings": warnings, "notes": ["MFC C++ event handlers, DDX/DDV, models and dynamic layout are not stored in RC and must be ported separately.",
                                               "Command resources sharing an ID are grouped in a QMainWindow. Different resource IDs are exported separately because RC does not record their runtime association.",
                                               "Connect the primary QAction for each command ID; alternate menu labels and extra shortcuts forward trigger() to that action.",
                                               "Resource images use a generated QRC. Compile that QRC with Qt rcc/pyside6-rcc for application use."],
              "resources": [{"type": item.type, "name": item.name, "language": item.language,
                             **({"data_base64": base64.b64encode(item.data).decode("ascii")} if isinstance(item.data, bytes)
                                else {"parsed": item.data})} for item in resources]}
    report["decision_origins"] = {
        "source": "Explicit RC/XML/image data or direct unit conversion.",
        "platform_default": "A Windows/MFC convention used when source data omits a value.",
        "approximation": "A converter inference or Qt representation that is not an exact source value.",
        "user_override": "An explicit converter option.",
        "qt_runtime": "Selection is delegated to Qt at display time."}
    failed={}
    for item in errors:
        key=(item["file"],item.get("resource_line",item["line"]),item["resource_type"],item["resource_id"])
        entry=failed.setdefault(key,{"file":key[0],"line":key[1],"type":key[2],"resource_id":key[3],"errors":[]})
        entry["errors"].append(item)
    successful=[]
    for origin in origins:
        if language is not None and origin["language"] not in {0,language}:continue
        key=(origin["file"],origin["line"],origin["type"],origin["name"])
        if key not in failed:
            successful.append({**origin,"resource_id":origin["name"]})
    status=("partial" if outputs else "failed") if errors else "success"
    report.update(status=status,errors=errors,successful_resources=successful,failed_resources=list(failed.values()),
                  exported_ui_files=ui_names,
                  summary={"successful_resources":len(successful),"failed_resources":len(failed),
                           "errors":len(errors),"warnings":len(warnings),"ui_files":len(ui_names)})
    if ribbons.frames:
        for ribbon in ribbons.report:
            frame=next((form for form in commands.forms if form.get("ribbon")==ribbon["resource"] and form["language"]==ribbon["language"]),None)
            if frame is not None:ribbon["file"]=frame["file"]
        report["ribbons"] = ribbons.report
    if source_metadata is not None:report["rc_project"]=source_metadata
    assets.prune(outputs)
    outputs = {name: serialize(data) if isinstance(data, ET.Element) else data for name, data in outputs.items()}
    outputs.update(assets.files)
    outputs[assets.qrc_name] = assets.qrc()
    report_name = "conversion_report.json"
    report["output_files"] = sorted([*outputs,report_name])
    outputs[report_name] = (json.dumps(report, ensure_ascii=False, indent=2, default=report_value) + "\n").encode("utf-8")
    destination = Path(output_dir).resolve() if output_dir else path.parent/(path.stem+"_qt")
    # No destination mutation occurs until parsing, serialization
    # and strict-mode checks have all succeeded.
    for relative, data in outputs.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for item in errors+warnings:
        print(f"{item['severity'].title()} [{item['code']}]: {item['message']}", file=sys.stderr)
    print(f"Report: {destination / report_name}", file=sys.stderr)
    result=[destination / filename for filename in ui_names]
    if errors:raise IncompleteConversionError(status,result,destination/report_name)
    return result
