"""Convert Win32 MENU/MENUEX, ACCELERATORS, MFC TOOLBAR and STRINGTABLE.

Resource families sharing an ID become one QMainWindow with shared QActions.
No command handlers or associations between different resource IDs are guessed.
"""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
import json
import locale
import struct
import zlib
import xml.etree.ElementTree as ET

from mfc_rc import (ConversionError, Reader, conversion_decision, prop, safe_name,
                    serialize, set_icon, set_size, unique_name)


@dataclass
class MenuItem:
    command: int
    text: str
    type: int
    state: int
    help_id: int = 0
    popup: bool = False
    children: list = field(default_factory=list)


# Windows virtual-key values -> Qt PortableText key names. OEM punctuation is
# layout-dependent and is deliberately reported rather than silently guessed.
VIRTUAL_KEYS = {0x08: "Backspace", 0x09: "Tab", 0x0C: "Clear", 0x0D: "Return",
                0x13: "Pause", 0x14: "CapsLock", 0x1B: "Esc", 0x20: "Space",
                0x21: "PgUp", 0x22: "PgDown", 0x23: "End", 0x24: "Home",
                0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
                0x29: "Select", 0x2A: "Print", 0x2B: "Execute", 0x2C: "Print",
                0x2D: "Ins", 0x2E: "Del", 0x2F: "Help", 0x5D: "Menu",
                0x6A: "Num+*", 0x6B: "Num++", 0x6D: "Num+-", 0x6E: "Num+.", 0x6F: "Num+/",
                0x90: "NumLock", 0x91: "ScrollLock", 0xAD: "Volume Mute",
                0xAE: "Volume Down", 0xAF: "Volume Up", 0xB0: "Media Next",
                0xB1: "Media Previous", 0xB2: "Media Stop", 0xB3: "Toggle Media Play/Pause"}
VIRTUAL_KEYS.update({key: chr(key) for key in range(0x30, 0x3A)})
VIRTUAL_KEYS.update({key: chr(key) for key in range(0x41, 0x5B)})
VIRTUAL_KEYS.update({key: f"F{key - 0x6F}" for key in range(0x70, 0x88)})
VIRTUAL_KEYS.update({key: f"Num+{key - 0x60}" for key in range(0x60, 0x6A)})


def shortcut_text(entry):
    flags, key = entry["flags"], entry["key"]
    modifiers = [name for bit, name in ((8, "Ctrl"), (16, "Alt"), (4, "Shift")) if flags & bit]
    if flags & 1:
        text = VIRTUAL_KEYS.get(key)
        if text is None:
            return None
    elif key in {8, 9, 13, 27, 32}:
        text = {8: "Backspace", 9: "Tab", 13: "Return", 27: "Esc", 32: "Space"}[key]
    elif 1 <= key <= 26:
        text = chr(64 + key)
        if "Ctrl" not in modifiers:
            modifiers.insert(0, "Ctrl")
    elif 33 <= key <= 0x7E:
        text = chr(key)
        if text.isalpha():
            # An ASCII uppercase character requires Shift unless Ctrl/Alt is
            # explicitly specified. Qt letters use uppercase portable spelling.
            if text.isupper() and not modifiers:
                modifiers.append("Shift")
            text = text.upper()
    else:
        return None
    return "+".join([*modifiers, text])


def parse_string_block(resource):
    if not isinstance(resource.name, int) or not 1 <= resource.name <= 4096:
        raise ConversionError(f"string table: invalid block ID {resource.name!r}")
    reader, values = Reader(resource.data), {}
    for offset in range(16):
        size = reader.word()
        raw = reader.take(size * 2)
        try:
            text = raw.decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise ConversionError(f"string table {resource.name}: invalid UTF-16") from exc
        if size:
            values[(resource.name - 1) * 16 + offset] = text
    if any(reader.data[reader.pos:]):
        raise ConversionError(f"string table {resource.name}: unexpected trailing data")
    return values


def decode_dib(data, *, validate_only=False):
    """Decode uncompressed Windows DIB strips without a Qt/Pillow dependency."""
    if len(data) < 40:
        raise ConversionError("truncated toolbar bitmap")
    header, width, signed_height, planes, bits, compression, _, _, _, colors, _ = struct.unpack_from("<IiiHHIIiiII", data)
    if header not in {40, 108, 124} or width <= 0 or not signed_height or planes != 1 or bits not in {1, 4, 8, 16, 24, 32} or compression not in {0, 3, 6}:
        raise ConversionError("unsupported toolbar DIB format")
    if compression and bits not in {16, 32}:
        raise ConversionError("invalid toolbar bitmap bitfields")
    height = abs(signed_height)
    reader = Reader(data)
    reader.take(header)
    if compression:
        mask_count = 4 if compression == 6 or header >= 108 else 3
        masks = struct.unpack_from("<" + "I" * mask_count, data, 40) if header >= 108 else reader.unpack("I" * mask_count)
    else:
        masks = (0x7C00, 0x3E0, 0x1F) if bits == 16 else (0xFF0000, 0xFF00, 0xFF)
    palette_size = colors or (1 << bits if bits <= 8 else 0)
    if palette_size > 256:
        raise ConversionError("toolbar palette exceeds 256 colors")
    palette = []
    for _ in range(palette_size):
        blue, green, red, _ = reader.unpack("BBBB")
        palette.append((red, green, blue, 255))
    stride = ((width * bits + 31) // 32) * 4
    pixels = reader.take(stride * height)
    # Validation must also cover unused BMPs, without allocating/decoding RGBA.
    if validate_only and (bits > 8 or len(palette) == 1 << bits):
        return
    rgba = None if validate_only else bytearray(width * height * 4)

    def channel(value, mask):
        if not mask:
            return 255
        shift = (mask & -mask).bit_length() - 1
        maximum = mask >> shift
        return ((value & mask) >> shift) * 255 // maximum

    any_alpha = bits == 32 and compression == 0 and any(pixels[row * stride + column * 4 + 3] for row in range(height) for column in range(width))
    for y in range(height):
        row = pixels[(height - 1 - y if signed_height > 0 else y) * stride:][:stride]
        for x in range(width):
            if bits <= 8:
                index = row[x] if bits == 8 else (row[x // 2] >> (4 if x % 2 == 0 else 0)) & 15 if bits == 4 else (row[x // 8] >> (7 - x % 8)) & 1
                if index >= len(palette):
                    raise ConversionError("toolbar bitmap references a missing palette color")
                if validate_only:
                    continue
                color = palette[index]
            elif bits == 24:
                blue, green, red = row[x * 3:x * 3 + 3]
                color = (red, green, blue, 255)
            else:
                value = int.from_bytes(row[x * (bits // 8):(x + 1) * (bits // 8)], "little")
                alpha = channel(value, masks[3]) if len(masks) == 4 else row[x * 4 + 3] if any_alpha else 255
                color = (*[channel(value, mask) for mask in masks[:3]], alpha)
            # MFC's classic RGB(192,192,192) toolbar background is transparent.
            if color[:3] == (192, 192, 192):
                color = (*color[:3], 0)
            offset = (y * width + x) * 4
            rgba[offset:offset + 4] = bytes(color)
    return None if validate_only else (width, height, bytes(rgba))


def png_crop(width, height, rgba, x, y, crop_width, crop_height):
    if min(x, y) < 0 or x + crop_width > width or y + crop_height > height:
        raise ConversionError("toolbar bitmap strip is smaller than its declared button images")

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\0" + rgba[((y + row) * width + x) * 4:((y + row) * width + x + crop_width) * 4] for row in range(crop_height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", crop_width, crop_height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class CommandResources:
    def __init__(self, resources, name_for, assets, warn, language=None):
        self.name_for, self.assets, self.warn = name_for, assets, warn
        self.language = language
        self.ribbon = None
        self.menus, self.toolbars, self.accelerators = {}, {}, {}
        self.strings = defaultdict(dict)
        self.forms = []
        self.dialog_bindings = []
        self.decoded = []
        for resource in resources:
            if language is not None and resource.language not in {language, 0}:
                continue
            key = (resource.name, resource.language)
            if resource.type == 4:
                self.menus[key] = resource.data
                value = {**self.menus[key], "items": [asdict(item) for item in self.menus[key]["items"]]}
            elif resource.type == 9:
                self.accelerators[key] = resource.data
                value = self.accelerators[key]
            elif resource.type == 241:
                self.toolbars[key] = resource.data
                value = self.toolbars[key]
            elif resource.type == 6:
                value = resource.data
                duplicate = self.strings[resource.language].keys() & value.keys()
                if duplicate:
                    raise ConversionError(f"duplicate string IDs in language {resource.language:#x}: {sorted(duplicate)}")
                self.strings[resource.language].update(value)
            else:
                continue
            self.decoded.append({"type": resource.type, "resource": resource.name,
                                 "language": resource.language, "value": value})

    def select(self, collection, identifier, language):
        return collection.get((identifier, language), collection.get((identifier, 0)))

    def string(self, identifier, language):
        return self.strings.get(language, {}).get(identifier, self.strings.get(0, {}).get(identifier))

    def toolbar_icons(self, identifier, language, toolbar):
        if not any(toolbar["items"]):
            return {}
        resource = self.assets.find(2, identifier, language)
        if resource is None:
            self.warn("TOOLBAR_BITMAP_MISSING", None, f"Toolbar {identifier!r} has no bitmap strip with the same resource ID.")
            return {}
        if resource.language not in {0, language}:
            self.warn("TOOLBAR_BITMAP_LANGUAGE", None, f"Toolbar {identifier!r} uses bitmap language 0x{resource.language:04x}; verify the fallback.")
        try:
            width, height, rgba = self.assets.bitmap(resource)
            pending, index = [], 0
            for position, command in enumerate(toolbar["items"]):
                if not command:
                    continue
                data = png_crop(width, height, rgba, index * toolbar["width"], 0, toolbar["width"], toolbar["height"])
                relative = f"{self.assets.icon_folder}/toolbar_{safe_name(identifier)}_{language:04x}_{position}.png"
                pending.append((position, relative, data))
                index += 1
            return {position: self.assets.add(relative, data) for position, relative, data in pending}
        except (ConversionError, struct.error) as exc:
            self.warn("TOOLBAR_BITMAP_FORMAT", None, f"Toolbar {identifier!r}: {exc}")
            return {}

    def families(self):
        keys = set(self.menus) | set(self.toolbars) | set(self.accelerators)
        if self.ribbon is not None:
            keys.update(self.ribbon.frames)
        if self.language is not None:
            keys = {(identifier, self.language) for identifier, _ in keys}
        # A neutral resource supplements each localized member of its family.
        localized = {identifier for identifier, language in keys if language}
        return sorted((key for key in keys if key[1] or key[0] not in localized), key=lambda key: (str(key[0]), key[1]))

    def export_frames(self, reserved=()):
        keys = self.families()
        counts = Counter(identifier for identifier, _ in keys)
        outputs = {}
        used = {name.rsplit("/",1)[-1].lower() for name in reserved}
        for identifier, language in keys:
            name = safe_name(self.name_for(identifier, "IDR_"))
            filename = name + (f"_{language:04x}" if counts[identifier] > 1 else "") + ".ui"
            if filename.lower() in used:
                name += "_commands"
                filename = name + (f"_{language:04x}" if counts[identifier] > 1 else "") + ".ui"
            if filename.lower() in used:
                raise ConversionError(f"duplicate command form filename: {filename}")
            used.add(filename.lower())
            root = ET.Element("ui", version="4.0")
            ET.SubElement(root, "class").text = name
            window = ET.SubElement(root, "widget", {"class": "QMainWindow", "name": name})
            title = self.string(identifier, language)
            prop(window, "windowTitle", "string", title.split("\n", 1)[0] or name if title else name)
            if title is not None:
                prop(window, "RcStringTableText", "string", title, True)
            prop(window, "RcResourceId", "string", identifier, True)
            prop(window, "RcLanguage", "string", f"0x{language:04x}", True)
            central = ET.SubElement(window, "widget", {"class": "QWidget", "name": name + "_centralWidget"})
            builder = CommandUi(self, root, window, language)
            menu = self.select(self.menus, identifier, language)
            toolbar = self.select(self.toolbars, identifier, language)
            accelerators = self.select(self.accelerators, identifier, language) or []
            builder.collect_menu(menu)
            builder.collect_accelerators(accelerators)
            if menu:
                builder.menu_bar(window, menu)
            if toolbar:
                builder.toolbar(window, identifier, toolbar)
            ribbon_catalog = self.ribbon
            ribbon = ribbon_catalog.select(identifier, language) if ribbon_catalog is not None else None
            if ribbon is not None:
                ribbon_catalog.render(ribbon, builder, central)
            if menu or toolbar:
                ET.SubElement(window, "widget", {"class": "QStatusBar", "name": builder.unique("statusbar")})
            builder.finish()
            filename = f"ui/{'ribbons' if ribbon is not None else 'frames'}/{filename}"
            outputs[filename] = root
            self.forms.append({"resource": identifier, "language": language, "file": filename,
                               "menu": menu is not None, "toolbar": toolbar is not None,
                               "accelerators": len(accelerators), "actions": builder.action_report(),
                               "decisions": [conversion_decision("initial_size", None, "qt_runtime",
                                   "RC does not store the runtime frame size; Qt uses its content size hint.")]})
            if ribbon is not None:
                self.forms[-1]["ribbon"] = ribbon.resource.name
        return outputs

    def attach_dialog(self, root, dialog):
        menu = self.select(self.menus, dialog.menu, dialog.language)
        if menu is None:
            self.warn("DIALOG_MENU_MISSING", dialog, f"Menu resource {dialog.menu!r} was not found in the dialog language or neutral language.")
            return
        widget = root.find("widget")
        builder = CommandUi(self, root, widget, dialog.language)
        builder.collect_menu(menu)
        accelerators = self.select(self.accelerators, dialog.menu, dialog.language) or []
        builder.collect_accelerators(accelerators)
        # A layout keeps the menu above a content widget without moving any
        # source control relative to the dialog's original client coordinates.
        source_children = list(widget.findall("widget"))
        layout = ET.SubElement(widget, "layout", {"class": "QVBoxLayout", "name": builder.unique("rcMenuLayout")})
        for key in ("leftMargin", "topMargin", "rightMargin", "bottomMargin", "spacing"):
            prop(layout, key, "number", 0)
        fixed = not dialog.style & 0x40000
        prop(layout, "sizeConstraint", "enum", "QLayout::SetFixedSize" if fixed else "QLayout::SetMinimumSize")
        menu_item = ET.SubElement(layout, "item")
        builder.menu_bar(menu_item, menu)
        content = ET.SubElement(ET.SubElement(layout, "item"), "widget", {"class": "QWidget", "name": builder.unique("rcDialogContent")})
        source_rect = widget.find("property[@name='geometry']/rect")
        width, height = source_rect.findtext("width"), source_rect.findtext("height")
        for name in (("minimumSize", "maximumSize") if fixed else ("minimumSize",)):
            set_size(content, name, width, height)
        for source_child in source_children:
            widget.remove(source_child)
            content.append(source_child)
        for size in list(widget.findall("property")):
            if size.get("name") in {"minimumSize", "maximumSize"}:
                widget.remove(size)
        source_rect.find("height").text = str(int(height) + 24)
        builder.finish()
        self.dialog_bindings.append({"dialog": dialog.identifier, "language": dialog.language,
                                     "menu": dialog.menu, "actions": builder.action_report()})

    def export_strings(self, stem, source_language=None):
        if not self.strings:
            return {}
        from rc_i18n import preferred_language
        source_language = preferred_language(self.strings, source_language)
        identifier_names = {identifier: self.name_for(identifier, "IDS_")
                            for values in self.strings.values() for identifier in values}
        source = {}
        for values in self.strings.values():
            for identifier, value in values.items():
                source.setdefault(identifier, value)
        source.update(self.strings.get(0, {}))
        source.update(self.strings[source_language])
        bundle = {"context": safe_name(stem) + "_RcStringTable", "source_language": source_language,
                  "source_strings": {str(identifier): value for identifier, value in sorted(source.items())},
                  "identifiers": {str(identifier): name for identifier, name in sorted(identifier_names.items())},
                  "languages": {f"0x{language:04x}": {str(identifier): value for identifier, value in sorted(values.items())}
                                for language, values in sorted(self.strings.items())}}
        outputs = {safe_name(stem) + "_strings.json": (json.dumps(bundle, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}
        source_code = locale.windows_locale.get(source_language, "C")
        for language, values in sorted(self.strings.items()):
            code = locale.windows_locale.get(language, "C")
            ts = ET.Element("TS", version="2.1", language=code, sourcelanguage=source_code)
            context = ET.SubElement(ts, "context")
            ET.SubElement(context, "name").text = bundle["context"]
            for identifier, value in sorted(values.items()):
                message = ET.SubElement(context, "message")
                ET.SubElement(message, "source").text = source.get(identifier, value)
                # Resource identity disambiguates identical text used by
                # different IDs, which may translate differently.
                ET.SubElement(message, "comment").text = str(identifier)
                ET.SubElement(message, "extracomment").text = identifier_names[identifier]
                ET.SubElement(message, "translation").text = value
            filename = f"{safe_name(stem)}_strings_{language:04x}.ts"
            outputs[filename] = serialize(ts)
        # A small toolkit-neutral lookup also supports Qt translation by a
        # caller-supplied QCoreApplication.translate callable (PySide or PyQt).
        module = '''"""Generated RC string table lookup. Keep the companion JSON beside this file."""
import json
from pathlib import Path

_DATA = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
_ALIASES = {name: int(identifier) for identifier, name in _DATA["identifiers"].items()}

def rc_string(identifier, language=None, translate=None):
    """Load by numeric/symbolic ID. translate may be QCoreApplication.translate.

    An explicit language selects stored text; otherwise the source language is
    used and an installed Qt translator can select the application's language.
    Unknown/empty strings return "", like LoadString.
    """
    identifier = _ALIASES.get(identifier, identifier)
    identifier = str(identifier)
    selected = _DATA["source_language"] if language is None else language
    selected = int(selected, 0) if isinstance(selected, str) else selected
    key = f"0x{selected:04x}"
    table = _DATA["languages"].get(key, {})
    neutral = _DATA["languages"].get("0x0000", {})
    value = (_DATA["source_strings"].get(identifier, "") if language is None
             else table.get(identifier, neutral.get(identifier, "")))
    if translate is not None and language is None and value:
        return translate(_DATA["context"], value, identifier)
    return value
'''
        outputs[safe_name(stem) + "_strings.py"] = module.encode("utf-8")
        return outputs


class CommandUi:
    def __init__(self, catalog, root, widget, language):
        self.catalog, self.root, self.widget, self.language = catalog, root, widget, language
        self.names = {node.get("name") for node in root.iter() if node.tag in {"widget", "action", "actiongroup", "layout", "buttongroup"}}
        self.actions, self.primary, self.signatures, self.accelerators, self.aliases = {}, {}, {}, defaultdict(list), []
        self.icons = {}
        self.accelerator_keys = {}

    def unique(self, requested):
        return unique_name(requested, self.names)

    def connect(self, sender, signal, receiver, slot):
        connections = self.root.find("connections")
        if connections is None:
            connections = ET.SubElement(self.root, "connections")
        connection = ET.SubElement(connections, "connection")
        for key, value in (("sender", sender), ("signal", signal), ("receiver", receiver), ("slot", slot)):
            ET.SubElement(connection, key).text = value

    def action(self, command, caption=None, state=0, item_type=0):
        if caption is None and command in self.primary:
            return self.primary[command]
        signature = (command, caption, state, item_type)
        if signature in self.signatures:
            return self.signatures[signature]
        name = self.unique("action_" + self.catalog.name_for(command, "ID_"))
        action = {"name": name, "command": command, "caption": caption, "state": state, "type": item_type}
        self.actions[name] = action
        self.signatures[signature] = name
        if command not in self.primary:
            self.primary[command] = name
        else:
            primary = self.actions[self.primary[command]]
            if (state & 11, bool(item_type & 0x200)) != (primary["state"] & 11, bool(primary["type"] & 0x200)):
                self.catalog.warn("MENU_COMMAND_VARIANT_STATE", None, f"Command {command} has different states in multiple menu entries; port per-entry enable/check handling.")
            self.aliases.append((name, self.primary[command]))
        return name

    def collect_menu(self, menu):
        if not menu:
            return
        def visit(items):
            for item in items:
                if item.popup:
                    visit(item.children)
                elif not item.type & 0x800:
                    self.action(item.command, item.text, item.state, item.type)
        visit(menu["items"])

    def collect_accelerators(self, entries):
        for entry in entries:
            name = self.action(entry["command"])
            text = shortcut_text(entry)
            if text is None:
                self.catalog.warn("ACCELERATOR_KEY", None, f"Command {entry['command']}: unsupported/layout-dependent accelerator key {entry['key']:#x}; flags={entry['flags']:#x}.")
                continue
            previous = self.accelerator_keys.get(text)
            if previous is not None and previous != entry["command"]:
                self.catalog.warn("ACCELERATOR_CONFLICT", None, f"Shortcut {text} is assigned to commands {previous} and {entry['command']}.")
            self.accelerator_keys[text] = entry["command"]
            if text not in self.accelerators[name]:
                self.accelerators[name].append(text)

    def menu_bar(self, parent, menu):
        bar = ET.SubElement(parent, "widget", {"class": "QMenuBar", "name": self.unique("menubar")})
        prop(bar, "nativeMenuBar", "bool", False)
        prop(bar, "RcHelpId", "string", menu["help_id"], True)

        def emit(parent, items):
            additions = []
            for item in items:
                if item.type & ~0x800:
                    self.catalog.warn("MENU_CUSTOM_STYLE", None, f"Menu item {item.command}/{item.text!r}: native type {item.type:#x} needs custom layout/painting.")
                if item.state & ~(3 | 8 | 0x1000) or item.popup and item.state & 8:
                    self.catalog.warn("MENU_CUSTOM_STATE", None, f"Menu item {item.command}/{item.text!r}: native state {item.state:#x} requires application code.")
                if item.state & 0x1000:
                    self.catalog.warn("MENU_DEFAULT_ACTION", None, f"Menu item {item.command}: call QMenu.setDefaultAction in application code; default is retained in RcMenuState.")
                if item.popup:
                    name = self.unique("menu_" + (self.catalog.name_for(item.command, "ID_") if item.command else item.text.replace("&", "")))
                    submenu = ET.SubElement(parent, "widget", {"class": "QMenu", "name": name})
                    prop(submenu, "title", "string", item.text)
                    prop(submenu, "enabled", "bool", not bool(item.state & 3))
                    prop(submenu, "RcCommandId", "string", item.command, True)
                    prop(submenu, "RcMenuType", "string", item.type, True)
                    prop(submenu, "RcMenuState", "string", item.state, True)
                    prop(submenu, "RcHelpId", "string", item.help_id, True)
                    emit(submenu, item.children)
                    additions.append(name)
                elif item.type & 0x800:
                    additions.append("separator")
                else:
                    additions.append(self.action(item.command, item.text, item.state, item.type))
            for name in additions:
                ET.SubElement(parent, "addaction", name=name)
        emit(bar, menu["items"])
        return bar

    def toolbar(self, parent, identifier, toolbar):
        name = self.unique("toolbar_" + self.catalog.name_for(identifier, "IDR_"))
        widget = ET.SubElement(parent, "widget", {"class": "QToolBar", "name": name})
        prop(widget, "windowTitle", "string", self.catalog.name_for(identifier, "IDR_"))
        prop(widget, "RcResourceId", "string", identifier, True)
        set_size(widget, 'iconSize', toolbar['width'], toolbar['height'])
        for attribute_name, tag, value in (("toolBarArea", "enum", "TopToolBarArea"), ("toolBarBreak", "bool", "false")):
            attribute = ET.SubElement(widget, "attribute", name=attribute_name)
            ET.SubElement(attribute, tag).text = value
        icons = self.catalog.toolbar_icons(identifier, self.language, toolbar)
        for position, command in enumerate(toolbar["items"]):
            name = self.action(command) if command else "separator"
            ET.SubElement(widget, "addaction", name=name)
            if position in icons:
                if name in self.icons and self.icons[name] != icons[position]:
                    self.catalog.warn("TOOLBAR_ICON_CONFLICT", None, f"Command {command} appears with different toolbar images; first QAction icon is retained.")
                else:
                    self.icons[name] = icons[position]
        return widget

    def finish(self):
        # Additional accelerators get hidden QActions forwarding trigger() to
        # the primary action. This works in .ui/uic without setShortcuts hacks
        # or dependencies on a Python-specific loader.
        for name, shortcuts in list(self.accelerators.items()):
            for text in shortcuts[1:]:
                primary = self.actions[name]
                alias = self.unique(name + "_shortcut")
                self.actions[alias] = {**primary, "name": alias, "shortcut_only": True}
                self.accelerators[alias] = [text]
                self.aliases.append((alias, name))
        for name, action in self.actions.items():
            element = ET.SubElement(self.widget, "action", name=name)
            command, state, item_type = action["command"], action["state"], action["type"]
            help_text = self.catalog.string(command, self.language)
            status, _, tooltip = (help_text or "").partition("\n")
            caption = action["caption"]
            if caption is None:
                caption = tooltip or self.catalog.name_for(command, "ID_")
            if self.accelerators.get(name):
                caption = caption.split("\t", 1)[0]
            prop(element, "text", "string", caption)
            prop(element, "enabled", "bool", not bool(state & 3))
            if state & 8 or item_type & 0x200 or action.get("ribbon_checkable"):
                prop(element, "checkable", "bool", True)
                prop(element, "checked", "bool", bool(state & 8))
            prop(element, "shortcutContext", "enum", "Qt::WindowShortcut")
            if self.accelerators.get(name):
                prop(element, "shortcut", "string", self.accelerators[name][0])
            if status:
                prop(element, "statusTip", "string", status)
            if tooltip:
                prop(element, "toolTip", "string", tooltip)
            if help_text is not None:
                prop(element, "RcStringTableText", "string", help_text, True)
            if name in self.icons:
                set_icon(element, self.catalog.assets.ui_qrc_name, self.icons[name])
            prop(element, "RcCommandId", "string", command, True)
            prop(element, "RcMenuType", "string", item_type, True)
            prop(element, "RcMenuState", "string", state, True)
            if action["caption"] is not None:
                prop(element, "RcMenuText", "string", action["caption"], True)
            for property_name, (tag, value, dynamic) in action.get("ribbon_properties", {}).items():
                prop(element, property_name, tag, value, dynamic)
            # Explicitly associate actions with the window, including commands
            # used only by an accelerator table.
        for name in self.actions:
            ET.SubElement(self.widget, "addaction", name=name)
        for sender, receiver in self.aliases:
            self.connect(sender, "triggered()", receiver, "trigger()")

    def action_report(self):
        return [{**action, "shortcuts": self.accelerators.get(name, []),
                 "primary": self.primary[action["command"]]} for name, action in self.actions.items()]
