"""MFC AFX_RIBBON XML -> standard Qt widgets, sharing CommandUi and Assets.

MFC's adaptive Office ribbon has no direct Qt Widgets counterpart. Categories
become tabs, panels retain bottom captions, and small/large elements keep their
source order and grouping. Explicit ROW/COLUMN spans are honored when supplied;
otherwise the three-row layout follows MFC's large/small-button hints.

Explicit buttons forward clicks to shared QActions; dropdowns use managed
QMenuBars. Stored QSize properties preserve the layout in Qt Designer without
a loader plugin, custom Qt widget, or Python-only runtime binding.
"""

from dataclasses import dataclass
from pathlib import Path
import codecs
import json
import re
import struct
import zlib
import xml.etree.ElementTree as ET

from mfc_rc import ConversionError, Resource, conversion_decision, prop, safe_name
from rc_commands import decode_dib, png_crop


RIBBON_TYPES = {28, "28", "RT_RIBBON_XML", "RIBBON_XML"}
RIBBON_IMAGE_SIZES = {"IMAGE_SMALL": (16, 16), "IMAGE_LARGE": (32, 32)}
RIBBON_PANEL_ROWS = 3  # Qt approximation when XML does not give a complete grid.
RIBBON_ROW_PADDING = 6


def is_ribbon(resource):
    value = resource.type.strip('"').upper() if isinstance(resource.type, str) else resource.type
    return value in RIBBON_TYPES


def xml_root(data):
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        value = data.decode("utf-16").rstrip("\0")
    elif data[:4] in {b"<\0?\0", b"<\0A\0"}:
        value = data.decode("utf-16-le").rstrip("\0")
    elif data[:4] in {b"\0<\0?", b"\0<\0A"}:
        value = data.decode("utf-16-be").rstrip("\0")
    else:
        value = data.rstrip(b"\0")
    check = value.encode("utf-8") if isinstance(value, str) else value
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", check, re.I):
        raise ConversionError("Ribbon XML DTDs/entities are not supported")
    try:
        root = ET.fromstring(value)
    except (ET.ParseError, UnicodeError) as exc:
        raise ConversionError(f"invalid MFC Ribbon XML: {exc}") from exc
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1].upper()
    if root.tag not in {"AFX_RIBBON", "RIBBON_BAR"}:
        raise ConversionError(f"expected MFC AFX_RIBBON XML, found {root.tag!r}")
    bar = root if root.tag == "RIBBON_BAR" else root.find("RIBBON_BAR")
    if bar is None:
        raise ConversionError("MFC Ribbon XML is missing RIBBON_BAR")
    return root, bar


def number(value, default=None):
    if value is None or not str(value).strip():
        return default
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?(?:0[xX][0-9a-fA-F]+|\d+)", text):
        raise ConversionError(f"Ribbon integer must be a literal: {text!r}")
    return int(text, 16) if "x" in text.lower() else int(text, 10)


def text(node, tag, default=""):
    return node.findtext(tag, default=default) if node is not None else default


def flag(node, tag, default=False):
    value = text(node, tag, "").strip().upper()
    if not value:
        return default
    if value not in {"TRUE", "FALSE", "1", "0", "YES", "NO"}:
        raise ConversionError(f"Ribbon {tag}: invalid boolean {value!r}")
    return value in {"TRUE", "1", "YES"}


def pair(value, default):
    if value is None or not value.strip():
        return default
    values = [number(item) for item in re.split(r"[,;\s]+", value.strip())]
    if len(values) != 2 or min(values) <= 0:
        raise ConversionError(f"invalid Ribbon image size: {value!r}")
    return tuple(values)


def image_id(node):
    if node is None:
        return None, ""
    identifier = node if node.tag == "ID" else node.find("ID")
    if identifier is None:
        return None, ""
    return number(text(identifier, "VALUE")), text(identifier, "NAME").strip()


def xml_symbols(roots):
    """Collect command/image IDs from already parsed Ribbon XML trees."""
    result = {}
    for root in roots:
        for node in root.iter("ID"):
            value, name = image_id(node)
            if value is not None and name:
                result.setdefault(value, []).append(name)
    return result


def standalone(path, language=0):
    data = Path(path).read_bytes()
    xml_root(data)
    return Resource(28, safe_name(Path(path).stem), language, data)


def png_pixels(data):
    """Decode the PNG strip variants normally embedded by the MFC designer."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ConversionError("invalid Ribbon PNG signature")
    position, compressed, palette, transparency = 8, bytearray(), b"", b""
    header = None
    ended = False
    while position < len(data):
        if position + 12 > len(data):
            raise ConversionError("truncated Ribbon PNG chunk")
        size = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + size]
        if position + size + 12 > len(data) or zlib.crc32(kind + payload) & 0xFFFFFFFF != struct.unpack_from(">I", data, position + size + 8)[0]:
            raise ConversionError("invalid Ribbon PNG chunk/CRC")
        if kind == b"IHDR":
            if header is not None or size != 13:
                raise ConversionError("invalid Ribbon PNG header")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"PLTE":
            palette = payload
        elif kind == b"tRNS":
            transparency = payload
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            ended = True
            break
        elif kind[:1].isupper() and kind not in {b"IHDR"}:
            raise ConversionError(f"unsupported Ribbon PNG critical chunk {kind!r}")
        position += size + 12
    if header is None or not ended:
        raise ConversionError("incomplete Ribbon PNG")
    width, height, depth, color_type, compression, filtering, interlace = header
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if not width or not height or not channels or compression or filtering or interlace or depth not in ({1, 2, 4, 8} if color_type in {0, 3} else {8}):
        raise ConversionError("unsupported Ribbon PNG depth/interlacing/color type")
    stride = (width * channels * depth + 7) // 8
    expected = (stride + 1) * height
    if expected > 128 * 1024 * 1024:
        raise ConversionError("Ribbon PNG exceeds the 128 MiB decoded-image limit")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(bytes(compressed), expected + 1)
    if len(raw) != expected or not decoder.eof:
        raise ConversionError("invalid Ribbon PNG decompressed size")
    bpp = max(1, (channels * depth + 7) // 8)
    previous, result = bytearray(stride), bytearray(width * height * 4)
    for y in range(height):
        mode = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if mode > 4:
            raise ConversionError("unknown Ribbon PNG row filter")
        for index in range(stride):
            left = row[index - bpp] if index >= bpp else 0
            up = previous[index]
            corner = previous[index - bpp] if index >= bpp else 0
            if mode == 1:
                prediction = left
            elif mode == 2:
                prediction = up
            elif mode == 3:
                prediction = (left + up) // 2
            elif mode == 4:
                value = left + up - corner
                distances = (abs(value - left), abs(value - up), abs(value - corner))
                prediction = (left, up, corner)[distances.index(min(distances))]
            else:
                prediction = 0
            row[index] = (row[index] + prediction) & 255
        for x in range(width):
            if color_type in {0, 3}:
                sample = (row[x * depth // 8] >> (8 - depth - x * depth % 8)) & ((1 << depth) - 1)
                if color_type == 3:
                    if sample * 3 + 3 > len(palette):
                        raise ConversionError("Ribbon PNG references a missing palette entry")
                    color = (*palette[sample * 3:sample * 3 + 3], transparency[sample] if sample < len(transparency) else 255)
                else:
                    gray = sample * 255 // ((1 << depth) - 1)
                    color = (gray, gray, gray, 0 if len(transparency) == 2 and sample == int.from_bytes(transparency, "big") else 255)
            elif color_type == 2:
                rgb = tuple(row[x * 3:x * 3 + 3])
                transparent = len(transparency) == 6 and rgb == struct.unpack(">HHH", transparency)
                color = (*rgb, 0 if transparent else 255)
            elif color_type == 4:
                gray, alpha = row[x * 2:x * 2 + 2]
                color = (gray, gray, gray, alpha)
            else:
                color = row[x * 4:x * 4 + 4]
            result[(y * width + x) * 4:(y * width + x + 1) * 4] = bytes(color)
        previous = row
    return width, height, bytes(result)


@dataclass
class Ribbon:
    resource: Resource
    root: ET.Element
    bar: ET.Element
    target: object


class RibbonResources:
    def __init__(self, resources, symbols, name_for, assets, warn, language=None, target=None):
        self.assets, self.warn, self.name_for = assets, warn, name_for
        self.frames, self.report, self.used_images = {}, [], set()
        self.names = {name: value for value, names in symbols.items() for name in names}
        self.image_cache = {}
        self.image_urls = {}
        self.image_sizes = {}
        if target is not None:
            target = self.names.get(str(target), number(target) if re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|\d+)", str(target)) else target)
            frame_ids = {resource.name for resource in resources if resource.type in {4, 9, 241}}
            if target not in frame_ids:
                raise ConversionError(f"--ribbon-target {target!r} is not an existing menu/toolbar/accelerator resource ID")
        for resource in resources:
            if not is_ribbon(resource) or language is not None and resource.language not in {0, language}:
                continue
            root, bar = xml_root(resource.data)
            ribbon = Ribbon(resource, root, bar, resource.name if target is None else target)
            key = (ribbon.target, resource.language)
            if key in self.frames:
                raise ConversionError(f"multiple Ribbons target resource {ribbon.target!r}, language {resource.language:#x}")
            self.frames[key] = ribbon
        if target is not None and not self.frames:
            raise ConversionError("--ribbon-target was specified but no Ribbon exists for the requested language")
        for value, names in xml_symbols(ribbon.root for ribbon in self.frames.values()).items():
            for name in names:
                if name in self.names and self.names[name] != value:
                    warn("RIBBON_ID_MISMATCH", None, f"XML ID {name}={value} differs from compiled header ID {self.names[name]}; XML VALUE is retained.")
                else:
                    self.names[name] = value

    def select(self, identifier, language):
        return self.frames.get((identifier, language), self.frames.get((identifier, 0)))

    def identifier(self, node):
        value, name = image_id(node)
        if value is None and name:
            value = self.names.get(name)
            if value is None:
                self.warn("RIBBON_UNRESOLVED_ID", None, f"Ribbon ID {name!r} has no numeric VALUE/header definition; its symbolic ID is retained.")
                value = name
        if value in {-1, 0xFFFFFFFF}:
            return None, name
        return value, name

    def image(self, reference, size, index, language):
        identifier, _ = self.identifier(reference)
        if identifier is None:
            return None
        key = (identifier, language)
        image_key = (identifier, language, size, index)
        if image_key in self.image_urls:
            return self.image_urls[image_key]
        try:
            if key not in self.image_cache:
                resource = self.assets.find(2, identifier, language)
                if resource is None:
                    choices = [r for r in self.assets.resources if r.name == identifier and r.data.startswith(b"\x89PNG\r\n\x1a\n")]
                    resource = next((r for r in choices if r.language == language), next((r for r in choices if r.language == 0), choices[0] if choices else None))
                if resource is None:
                    raise ConversionError(f"image resource {identifier!r} is missing; provide --ribbon-resources for standalone XML")
                pixels = png_pixels(resource.data) if resource.data.startswith(b"\x89PNG") else decode_dib(resource.data[14:] if resource.data.startswith(b"BM") else resource.data)
                self.image_cache[key] = pixels
                self.used_images.add((resource.type, resource.name, resource.language))
            width, height, rgba = self.image_cache[key]
            if index is None:
                x, y, cell_width, cell_height = 0, 0, width, height
            else:
                cell_width, cell_height = size
                if cell_width <= 0 or cell_height <= 0 or width % cell_width or height % cell_height:
                    raise ConversionError(f"image {identifier!r} size {width}x{height} is not divisible by cell size {size}")
                columns = width // cell_width
                x, y = index % columns * cell_width, index // columns * cell_height
            data = png_crop(width, height, rgba, x, y, cell_width, cell_height)
            # Reuse exactly the same Assets/QRC inventory as classic toolbars.
            filename = next((name for name, existing in self.assets.files.items()
                             if name.endswith(".png") and existing == data), None)
            if filename is None:
                suffix = "whole" if index is None else str(index)
                folder = self.assets.image_folder if index is None else self.assets.icon_folder
                filename = f"{folder}/ribbon_{safe_name(identifier)}_{language:04x}_{cell_width}x{cell_height}_{suffix}.png"
                self.assets.files[filename] = data
            self.image_urls[image_key] = self.assets.url(filename)
            self.image_sizes[self.image_urls[image_key]] = (cell_width, cell_height)
            return self.image_urls[image_key]
        except (ConversionError, ValueError, struct.error, zlib.error) as exc:
            self.warn("RIBBON_IMAGE", None, str(exc))
            return None

    def render(self, ribbon, builder, central):
        renderer = RibbonUi(self, ribbon, builder)
        renderer.render(central)
        self.report.append(renderer.report)


class RibbonUi:
    def __init__(self, catalog, ribbon, builder):
        self.catalog, self.ribbon, self.builder = catalog, ribbon, builder
        self.language = builder.language
        self.paths = {id(node): path for node, path in self.walk(ribbon.bar, "RIBBON_BAR")}
        self.report = {"resource": ribbon.resource.name, "target": ribbon.target,
                       "language": self.language, "tabs": [], "elements": [], "decisions": [],
                       "notes": ["Standard Qt tabs/panels approximate MFC adaptive sizing and collapse; XML order, group and large/small hints are retained.",
                                 "Ribbon keytips and contextual visibility are retained as metadata and need application code.",
                                 "Explicit buttons forward clicks to shared QActions; managed menu bars and stored QSize properties survive Designer serialization."]}
        self.decision_indices = {}
        self.size_rules = {}
        self.small_size = self.size_hint("IMAGE_SMALL")
        self.large_size = self.size_hint("IMAGE_LARGE")
        self.definitions = {}
        self.unsupported = set()

    @staticmethod
    def walk(node, path):
        yield node, path
        counts = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            yield from RibbonUi.walk(child, f"{path}/{child.tag}[{counts[child.tag]}]")

    def decision(self, node, property, value, origin, reason):
        path = self.paths.get(id(node), node.tag)
        record = {"path": path, **conversion_decision(property, value, origin, reason)}
        key = (path, property)
        if key in self.decision_indices:
            self.report["decisions"][self.decision_indices[key]] = record
        else:
            self.decision_indices[key] = len(self.report["decisions"])
            self.report["decisions"].append(record)

    def size_hint(self, tag):
        default = RIBBON_IMAGE_SIZES[tag]
        for path in (f"HEADER/SIZES/{tag}", f"HEADER/SIZE_{tag}", f"SIZES/{tag}"):
            value = self.ribbon.root.findtext(path)
            if value:
                size = pair(value, default)
                self.size_rules[tag] = ("source", "Ribbon XML " + path)
                break
        else:
            size = default
            self.size_rules[tag] = ("platform_default", "MFC small/large image convention at the 96-DPI logical baseline; no XML size was supplied.")
        self.decision(self.ribbon.bar, tag.lower() + "_size", size, *self.size_rules[tag])
        return size

    def properties(self, widget, node, command=None):
        for previous in list(widget.findall("property")):
            if previous.get("name") in {"RcRibbonPath", "RcRibbonType", "RcRibbonProperties", "RcCommandId", "RcRibbonCommandName", "RcRibbonKeys"}:
                widget.remove(previous)
        prop(widget, "RcRibbonPath", "string", self.paths.get(id(node), node.tag), True)
        prop(widget, "RcRibbonType", "string", text(node, "ELEMENT_NAME", node.tag), True)
        parameters = {}
        for child in node:
            if not len(child):
                parameters[child.tag] = child.text or ""
        if parameters:
            prop(widget, "RcRibbonProperties", "string", json.dumps(parameters, ensure_ascii=False), True)
        if command is not None:
            prop(widget, "RcCommandId", "string", command, True)
            _, name = self.catalog.identifier(node)
            if name:
                prop(widget, "RcRibbonCommandName", "string", name, True)
        if text(node, "KEYS"):
            prop(widget, "RcRibbonKeys", "string", text(node, "KEYS"), True)

    def layout(self, parent, kind="QVBoxLayout", prefix="ribbonLayout"):
        layout = ET.SubElement(parent, "layout", {"class": kind, "name": self.builder.unique(prefix)})
        for key in ("leftMargin", "topMargin", "rightMargin", "bottomMargin"):
            prop(layout, key, "number", 2)
        prop(layout, "spacing", "number", 2)
        return layout

    @staticmethod
    def size_policy(widget, vertical="Fixed", horizontal="Expanding"):
        policy = prop(widget, "sizePolicy", "sizepolicy", "")
        policy.set("hsizetype", horizontal)
        policy.set("vsizetype", vertical)
        ET.SubElement(policy, "horstretch").text = "0"
        ET.SubElement(policy, "verstretch").text = "0"

    def spacer(self, layout, horizontal=True):
        item = ET.SubElement(layout, "item")
        spacer = ET.SubElement(item, "spacer", name=self.builder.unique("ribbonSpacer"))
        prop(spacer, "orientation", "enum", "Qt::Horizontal" if horizontal else "Qt::Vertical")
        size = prop(spacer, "sizeHint", "size", "")
        ET.SubElement(size, "width").text = "1"
        ET.SubElement(size, "height").text = "1"

    def inherited_images(self, node, inherited):
        result = dict(inherited)
        for tag, key in (("IMAGE_SMALL", "small"), ("IMAGE_LARGE", "large")):
            reference = node.find(tag)
            if reference is not None:
                result[key] = reference
        # Button groups own a small-image strip independent of their category.
        if text(node, "ELEMENT_NAME") == "Group" and node.find("IMAGE") is not None:
            result["small"] = node.find("IMAGE")
        return result

    def icon(self, node, inherited, large=False):
        own = node.find("IMAGE")
        if own is not None and text(node, "ELEMENT_NAME") not in {"Group", "Button_Gallery", "Button_Color", "Button_Undo"}:
            image = self.catalog.image(own, self.large_size if large else self.small_size, None, self.language)
            if image:
                self.decision(node, "own_image_size", self.catalog.image_sizes[image], "source", "Whole-image dimensions read from the referenced image resource.")
            return image
        attempts = (("INDEX_LARGE", "large", self.large_size), ("INDEX_SMALL", "small", self.small_size)) if large else (("INDEX_SMALL", "small", self.small_size), ("INDEX_LARGE", "large", self.large_size))
        for tag, key, default in attempts:
            index = number(text(node, tag), -1)
            reference = inherited.get(key)
            if index >= 0 and reference is not None:
                size = pair(text(reference, "SIZE"), default)
                origin, reason = ("source", "Image reference SIZE overrides the Ribbon header/default.") if text(reference, "SIZE") else self.size_rules["IMAGE_" + key.upper()]
                self.decision(node, key + "_icon_cell_size", size, origin, reason)
                return self.catalog.image(reference, size, index, self.language)
        return None

    def action(self, node, inherited, large=False):
        command, command_name = self.catalog.identifier(node)
        if command is None:
            return None
        if command in self.builder.primary:
            name = self.builder.primary[command]
            caption=text(node,"TEXT")
            primary=self.builder.actions[name]
            normalize=lambda value:(value or "").split("\t",1)[0].replace("&","")
            if caption and normalize(caption)!=normalize(primary["caption"]):
                # Different views may label the same command differently.
                # CommandUi forwards this presentation action to the primary.
                name=self.builder.action(command,caption,primary["state"],primary["type"])
        else:
            caption = text(node, "TEXT") or command_name or None
            name = self.builder.action(command, caption)
        action = self.builder.actions[name]
        self.definitions.setdefault(command, (node, inherited))
        if text(node, "ELEMENT_NAME") in {"Button_Check", "Button_Radio"}:
            action["ribbon_checkable"] = True
        for tag, mask in (("CHECKED", 8), ("ENABLED", 3)):
            if node.find(tag) is not None:
                active = flag(node, tag)
                action["state"] = (action["state"] | mask) if (active if tag == "CHECKED" else not active) else (action["state"] & ~mask)
        extras = action.setdefault("ribbon_properties", {})
        if command_name:
            extras["RcRibbonCommandName"] = ("string", command_name, True)
        if text(node, "TOOLTIP"):
            extras["toolTip"] = ("string", text(node, "TOOLTIP"), False)
        for tag in ("DESCRIPTION", "TOOLTIP_DESCRIPTION"):
            if text(node, tag):
                extras["whatsThis"] = ("string", text(node, tag), False)
        if text(node, "KEYS"):
            extras["RcRibbonKeys"] = ("string", text(node, "KEYS"), True)
        icon = self.icon(node, inherited, large)
        if icon and (name not in self.builder.icons or large and action.get("ribbon_icon_owner")):
            self.builder.icons[name] = icon
            action["ribbon_icon_owner"] = True
        return name

    def collect(self, node, inherited):
        inherited = self.inherited_images(node, inherited)
        if node.tag in {"ELEMENT", "BUTTON_MAIN", "BUTTON_LAUNCH", "LAUNCH_BUTTON"} and node.find("ID") is not None:
            self.action(node, inherited, flag(node, "ALWAYS_LARGE"))
        for child in node:
            self.collect(child, inherited)

    def bounds(self, widget, *, width=None, height=None, minimum_height=None):
        # The convenience width/height properties are not STORED/DESIGNABLE.
        # Designer previews serialize forms first, so persist their QSize pair.
        sizes={"minimumSize":[0,0],"maximumSize":[16777215,16777215]}
        for key,values in sizes.items():
            previous=widget.find("property[@name='"+key+"']")
            if previous is not None:
                values[:]=[int(previous.findtext("size/width",str(values[0]))),int(previous.findtext("size/height",str(values[1])))]
                widget.remove(previous)
            if width is not None:values[0]=width
            if height is not None:values[1]=height
            elif key=="minimumSize" and minimum_height is not None:values[1]=minimum_height
            size=prop(widget,key,"size","")
            ET.SubElement(size,"width").text=str(values[0]);ET.SubElement(size,"height").text=str(values[1])

    def popup_bar(self, parent, name):
        widget=ET.SubElement(parent,"widget",{"class":"QMenuBar","name":self.builder.unique(name)})
        prop(widget,"nativeMenuBar","bool",False)
        self.size_policy(widget,horizontal="Maximum")
        prop(widget,"styleSheet","string","QMenuBar { background: transparent; border: 0px; } QMenuBar::item { padding: 2px 4px; }")
        return widget

    def action_button(self, parent, name, node, action, caption, icon=None, large=False):
        button=ET.SubElement(parent,"widget",{"class":"QPushButton","name":self.builder.unique(name)})
        prop(button,"text","string",caption)
        prop(button,"flat","bool",True)
        prop(button,"autoDefault","bool",False)
        prop(button,"styleSheet","string","QPushButton { border: 0px; padding: 1px 3px; text-align: left; } QPushButton:hover { background-color: palette(midlight); } QPushButton:pressed, QPushButton:checked { background-color: palette(mid); }")
        self.size_policy(button,horizontal="Maximum")
        self.properties(button,node,self.catalog.identifier(node)[0])
        if icon:
            prop_icon=prop(button,"icon","iconset","");prop_icon.set("resource",self.catalog.assets.qrc_name)
            ET.SubElement(prop_icon,"normaloff").text=icon
            size=prop(button,"iconSize","size","")
            for key,value in zip(("width","height"),self.catalog.image_sizes.get(icon, self.large_size if large else self.small_size)):ET.SubElement(size,key).text=str(value)
        if action:
            data=self.builder.actions[action]
            prop(button,"enabled","bool",not bool(data["state"]&3))
            if data["state"]&8 or data.get("ribbon_checkable"):
                prop(button,"checkable","bool",True);prop(button,"checked","bool",bool(data["state"]&8))
                self.builder.connect(action,"toggled(bool)",button.get("name"),"setChecked(bool)")
            self.builder.connect(button.get("name"),"clicked()",action,"trigger()")
            help_text=self.builder.catalog.string(data["command"],self.language) or ""
            status,_,tip=help_text.partition("\n")
            if status:prop(button,"statusTip","string",status)
            extras=data.get("ribbon_properties",{})
            if "toolTip" in extras:tip=extras["toolTip"][1]
            if tip:prop(button,"toolTip","string",tip)
            if "whatsThis" in extras:prop(button,"whatsThis","string",extras["whatsThis"][1])
        return button

    def button_widget(self, parent, node, inherited, large=False, action=None):
        # Use managed, explicit widgets: nested QToolBar editor internals can
        # discard captions and menu widgets when Designer regenerates a form.
        command,_=self.catalog.identifier(node)
        name="ribbonButton_"+(str(command) if command is not None else text(node,"TEXT","Menu"))
        icon=self.icon(node,inherited,large)
        caption=text(node,"TEXT")
        elements=node.find("ELEMENTS")
        popup=bool(elements is not None and list(elements)) or (text(node,"ELEMENT_NAME") in {"Button_Color","Button_Undo"} and flag(node,"BUTTON_MODE",True))
        main=action is not None and (not popup or flag(node,"DEFAULT_COMMAND",True))
        if popup and not main:
            bar=self.popup_bar(parent,name)
            menu=self.menu(bar,node,inherited)
            if icon:
                image=prop(bar.find("widget[@name='"+menu+"']"),"icon","iconset","")
                image.set("resource",self.catalog.assets.qrc_name);ET.SubElement(image,"normaloff").text=icon
            ET.SubElement(bar,"addaction",name=menu)
            return bar
        if not large and not popup:
            if action:return self.action_button(parent,name,node,action,caption,icon)
            label=ET.SubElement(parent,"widget",{"class":"QLabel","name":self.builder.unique("ribbonLabel")})
            prop(label,"text","string",caption)
            return label
        wrapper=ET.SubElement(parent,"widget",{"class":"QWidget","name":self.builder.unique("ribbonLargeButton" if large else "ribbonSplitButton")})
        self.size_policy(wrapper,horizontal="Maximum")
        layout=self.layout(wrapper,"QVBoxLayout" if large else "QHBoxLayout")
        for value in layout.findall("property"):value.find("number").text="0"
        if main:
            item=ET.SubElement(layout,"item",alignment="Qt::AlignHCenter" if large else "Qt::AlignLeft")
            button=self.action_button(item,name,node,action,"" if large and icon else caption,icon,large)
            if large and icon:
                self.bounds(button,minimum_height=self.catalog.image_sizes.get(icon, self.large_size)[1]+4)
                self.action_button(ET.SubElement(layout,"item",alignment="Qt::AlignHCenter"),name+"_caption",node,action,caption)
        if popup:
            bar=self.popup_bar(ET.SubElement(layout,"item",alignment="Qt::AlignHCenter" if large else "Qt::AlignVCenter"),"ribbonDropdown")
            menu=self.menu(bar,node,inherited,"▾")
            ET.SubElement(bar,"addaction",name=menu)
        return wrapper


    def menu(self, parent, node, inherited, title=None):
        name = self.builder.unique("ribbonMenu_" + text(node, "TEXT", "Commands"))
        menu = ET.SubElement(parent, "widget", {"class": "QMenu", "name": name})
        prop(menu, "title", "string", title if title is not None else text(node, "TEXT", text(node, "NAME", "File")))
        self.properties(menu, node)
        additions = []
        for child in node.findall("ELEMENTS/ELEMENT"):
            kind = text(child, "ELEMENT_NAME", "Button")
            if kind == "Separator":
                additions.append("separator")
                self.record(child, menu, placement="menu_separator")
            elif child.find("ELEMENTS/ELEMENT") is not None:
                submenu = self.menu(menu, child, self.inherited_images(child, inherited))
                additions.append(submenu)
                self.record(child, menu.find(f"widget[@name='{submenu}']"), self.action(child, inherited), "submenu")
            else:
                action = self.action(child, inherited)
                if action:
                    additions.append(action)
                    self.record(child, menu, action, "menu")
                elif kind == "Label":
                    label_action = self.builder.unique("ribbonMenuLabel")
                    element = ET.SubElement(self.builder.widget, "action", name=label_action)
                    prop(element, "text", "string", text(child, "TEXT"))
                    prop(element, "enabled", "bool", False)
                    additions.append(label_action)
                    self.record(child, element, label_action, "menu_label")
                else:
                    self.warning(child, f"{kind} inside a dropdown requires a QWidgetAction; metadata is preserved.")
        for addition in additions:
            ET.SubElement(menu, "addaction", name=addition)
        return name

    def record(self, node, widget, action=None, placement=None):
        command, command_name = self.catalog.identifier(node)
        self.report["elements"].append({"path": self.paths.get(id(node), node.tag), "type": text(node, "ELEMENT_NAME", node.tag),
                                         "command": command, "command_name": command_name, "widget": widget.get("name"),
                                         "qt_class": widget.get("class"), "action": action, "placement": placement})

    def warning(self, node, message):
        key = (id(node), message)
        if key not in self.unsupported:
            self.unsupported.add(key)
            self.catalog.warn("RIBBON_FALLBACK", None, self.paths.get(id(node), node.tag) + ": " + message)

    def element(self, parent, node, inherited):
        kind = text(node, "ELEMENT_NAME", "Button")
        inherited = self.inherited_images(node, inherited)
        large = self.large_button(node,inherited)
        action = self.action(node, inherited, large)
        container=None
        if kind=="Button_Check" and not self.icon(node,inherited):
            widget=ET.SubElement(parent,"widget",{"class":"QCheckBox","name":self.builder.unique("ribbonCheck_"+str(self.catalog.identifier(node)[0]))})
            prop(widget,"text","string",text(node,"TEXT"))
            prop(widget,"checked","bool",bool(action and self.builder.actions[action]["state"]&8))
            if action:
                self.builder.connect(widget.get("name"),"clicked()",action,"trigger()")
                self.builder.connect(action,"toggled(bool)",widget.get("name"),"setChecked(bool)")
        elif kind in {"Button", "Button_Main_Panel", "Button_Launch", "Button_Check", "Button_Radio", "Button_LinkCtrl", "Button_Undo", "Button_Color", "Button_Main"}:
            widget = self.button_widget(parent, node, inherited, large, action)
            if kind in {"Button_Color", "Button_Undo"}:
                self.warning(node, f"{kind} maps to a command button; color picker/undo-history behavior requires application code.")
            if kind == "Button_LinkCtrl":
                prop(widget, "RcRibbonLink", "string", text(node, "LINK"), True)
        elif kind == "Separator":
            widget = ET.SubElement(parent, "widget", {"class": "QFrame", "name": self.builder.unique("ribbonSeparator")})
            prop(widget, "frameShape", "enum", "QFrame::HLine" if flag(node, "HORIZ") else "QFrame::VLine")
            prop(widget, "frameShadow", "enum", "QFrame::Sunken")
        elif kind == "Label":
            widget = ET.SubElement(parent, "widget", {"class": "QLabel", "name": self.builder.unique("ribbonLabel")})
            prop(widget, "text", "string", text(node, "TEXT"))
            prop(widget, "textFormat", "enum", "Qt::PlainText")
        elif kind == "Group":
            widget = ET.SubElement(parent, "widget", {"class": "QWidget", "name": self.builder.unique("ribbonGroup")})
            layout = self.layout(widget, "QVBoxLayout" if flag(node, "VERTICAL") else "QHBoxLayout")
            for item in layout.findall("property"):
                if item.get("name").endswith("Margin"):item.find("number").text="0"
            for child in node.findall("ELEMENTS/ELEMENT"):
                self.element(ET.SubElement(layout, "item"), child, inherited)
        elif kind in {"Edit", "ComboBox", "ComboBox_Font", "Slider", "Progress"}:
            qt_type = {"Edit": "QSpinBox" if flag(node, "SPIN_BUTTONS") else "QLineEdit", "ComboBox": "QComboBox", "ComboBox_Font": "QFontComboBox", "Slider": "QSlider", "Progress": "QProgressBar"}[kind]
            control_parent=parent
            label=None
            if kind in {"Edit","ComboBox","ComboBox_Font"} and text(node,"TEXT"):
                container=ET.SubElement(parent,"widget",{"class":"QWidget","name":self.builder.unique("ribbonField")})
                self.size_policy(container,horizontal="Maximum")
                field_layout=self.layout(container,"QHBoxLayout")
                for item in field_layout.findall("property"):
                    if item.get("name").endswith("Margin"):item.find("number").text="0"
                label=ET.SubElement(ET.SubElement(field_layout,"item"),"widget",{"class":"QLabel","name":self.builder.unique("ribbonFieldLabel")})
                prop(label,"text","string",text(node,"TEXT"))
                prop(label,"textFormat","enum","Qt::PlainText")
                control_parent=ET.SubElement(field_layout,"item")
            widget = ET.SubElement(control_parent, "widget", {"class": qt_type, "name": self.builder.unique("ribbonControl_" + str(self.catalog.identifier(node)[0]))})
            if label is not None:prop(label,"buddy","cstring",widget.get("name"))
            if kind == "Edit":
                if qt_type == "QLineEdit":
                    prop(widget, "text", "string", text(node, "VALUE"))
                    signal = "editingFinished()"
                else:
                    self.range_properties(widget, node, spin=True)
                    signal = "editingFinished()"
            elif kind in {"ComboBox", "ComboBox_Font"}:
                prop(widget, "editable", "bool", flag(node, "EDIT_BOX", True))
                if kind == "ComboBox":
                    values = [item.text or "" for item in node.findall("ITEMS/ITEM")]
                    for value in values:
                        prop(ET.SubElement(widget, "item"), "text", "string", value)
                    if text(node, "VALUE") in values:
                        prop(widget, "currentIndex", "number", values.index(text(node, "VALUE")))
                    elif text(node, "VALUE"):
                        prop(widget, "currentText", "string", text(node, "VALUE"))
                else:
                    if text(node, "VALUE"):
                        font = prop(widget, "currentFont", "font", "")
                        ET.SubElement(font, "family").text = text(node, "VALUE")
                    else:prop(widget,"currentIndex","number",-1)
                signal = "activated(int)"
            else:
                self.range_properties(widget, node)
                if kind == "Slider":
                    prop(widget, "orientation", "enum", "Qt::Vertical" if number(text(node, "STYLE"), 0) & 2 else "Qt::Horizontal")
                    signal = "valueChanged(int)"
                    if flag(node, "ZOOM_BUTTONS"):
                        self.warning(node, "Slider zoom buttons need application code; the slider/range is retained.")
                else:
                    prop(widget, "textVisible", "bool", False)
                    signal = None
            if action and signal:
                self.builder.connect(widget.get("name"), signal, action, "trigger()")
        elif kind == "Button_Gallery":
            widget = self.gallery(parent, node, inherited, action)
        else:
            widget = ET.SubElement(parent, "widget", {"class": "QGroupBox", "name": self.builder.unique("ribbonUnsupported")})
            prop(widget, "title", "string", text(node, "TEXT", kind))
            prop(widget, "RcRibbonXml", "string", ET.tostring(node, encoding="unicode"), True)
            self.warning(node, f"Unknown element {kind!r} retained as a group with its child elements and source XML.")
            layout = self.layout(widget, "QHBoxLayout")
            for child in node.findall("ELEMENTS/ELEMENT"):
                self.element(ET.SubElement(layout, "item"), child, inherited)
        self.properties(widget, node, self.catalog.identifier(node)[0])
        for dimension in ("WIDTH", "HEIGHT"):
            value = number(text(node, dimension))
            if value is not None and value > 0:
                self.bounds(widget,**({"width":value} if dimension=="WIDTH" else {"height":value}))
                self.decision(node, dimension.lower(), value, "source", "Explicit Ribbon XML dimension.")
        if node.find("VISIBLE") is not None and not flag(node, "VISIBLE", True):
            prop(widget, "visible", "bool", False)
        if node.find("ENABLED") is not None:
            prop(widget, "enabled", "bool", flag(node, "ENABLED", True))
        self.record(node, widget, action)
        return container if container is not None else widget

    def large_button(self,node,inherited):
        kind=text(node,"ELEMENT_NAME","Button")
        forced = flag(node,"ALWAYS_LARGE")
        inferred = (kind.startswith("Button") and kind not in {"Button_Check","Button_Radio"}
                    and number(text(node,"INDEX_LARGE"),-1)>=0 and inherited.get("large") is not None)
        if kind.startswith("Button"):
            self.decision(node, "large_presentation", bool(forced or inferred),
                          "source" if forced else "approximation",
                          "ALWAYS_LARGE explicitly requests a large button." if forced else
                          "Large-image availability guides the Qt presentation; MFC's adaptive runtime sizing is not stored here.")
        return bool(forced or inferred)

    @staticmethod
    def range_properties(widget, node, spin=False):
        minimum, maximum = number(text(node, "MIN"), 0), number(text(node, "MAX"), 100)
        if maximum < minimum:
            raise ConversionError("Ribbon range MAX is smaller than MIN")
        if flag(node, "INFINITE"):
            minimum = maximum = 0
        prop(widget, "minimum", "number", minimum)
        prop(widget, "maximum", "number", maximum)
        value = number(text(node, "VALUE") if spin else text(node, "POS"), minimum)
        prop(widget, "value", "number", max(minimum, min(maximum, value)))

    def gallery(self, parent, node, inherited, action):
        if flag(node, "BUTTON_MODE"):
            return self.gallery_popup(parent, node, inherited, action)
        if node.find("IMAGE") is None and not node.findall("ITEMS/ITEM") and node.find("ELEMENTS/ELEMENT") is None:
            self.warning(node,"Gallery entries are supplied at runtime; its command is shown as a labeled button until items are available.")
            return self.button_widget(parent,node,inherited,self.large_button(node,inherited),action)
        if node.find("ELEMENTS/ELEMENT") is not None:
            wrapper = ET.SubElement(parent, "widget", {"class": "QWidget", "name": self.builder.unique("ribbonGalleryGroup")})
            layout = self.layout(wrapper)
            self.gallery_list(ET.SubElement(layout, "item"), node, inherited, action)
            for child in node.findall("ELEMENTS/ELEMENT"):
                self.element(ET.SubElement(layout, "item"), child, inherited)
            return wrapper
        return self.gallery_list(parent, node, inherited, action)

    def gallery_data(self, node):
        size_value = text(node, "SIZE_ICON")
        zero = re.fullmatch(r"\s*0\s*,\s*0\s*", size_value)
        size = self.large_size if zero else pair(size_value, self.large_size)
        paths = []
        reference = node.find("IMAGE")
        if reference is not None:
            first = self.catalog.image(reference, size, 0, self.language)
            identifier, _ = self.catalog.identifier(reference)
            pixels = self.catalog.image_cache.get((identifier, self.language))
            if first and pixels:
                width, height, _ = pixels
                paths = [self.catalog.image(reference, size, index, self.language) for index in range(width // size[0] * (height // size[1]))]
        labels = [item.text or "" for item in node.findall("ITEMS/ITEM")]
        return size, paths, labels

    def gallery_popup(self, parent, node, inherited, action):
        size, paths, labels = self.gallery_data(node)
        toolbar = self.popup_bar(parent,"ribbonGalleryButton")
        name = self.builder.unique("ribbonGalleryMenu")
        menu = ET.SubElement(toolbar, "widget", {"class": "QMenu", "name": name})
        prop(menu, "title", "string", text(node, "TEXT"))
        image = self.icon(node, inherited, flag(node, "ALWAYS_LARGE"))
        if image:
            icon = prop(menu, "icon", "iconset", "")
            icon.set("resource", self.catalog.assets.qrc_name)
            ET.SubElement(icon, "normaloff").text = image
        group = ET.SubElement(self.builder.widget, "actiongroup", name=self.builder.unique("ribbonGallerySelection"))
        prop(group, "exclusive", "bool", True)
        additions = []
        for index in range(max(len(paths), len(labels))):
            item_name = self.builder.unique("ribbonGalleryItem")
            item = ET.SubElement(group, "action", name=item_name)
            prop(item, "text", "string", labels[index] if index < len(labels) else str(index + 1))
            prop(item, "checkable", "bool", True)
            prop(item, "RcGalleryIndex", "number", index, True)
            command, _ = self.catalog.identifier(node)
            if command is not None:
                prop(item, "RcCommandId", "string", command, True)
            if index < len(paths) and paths[index]:
                icon = prop(item, "icon", "iconset", "")
                icon.set("resource", self.catalog.assets.qrc_name)
                ET.SubElement(icon, "normaloff").text = paths[index]
            additions.append(item_name)
            if action:
                self.builder.connect(item_name, "triggered()", action, "trigger()")
        for child in node.findall("ELEMENTS/ELEMENT"):
            if text(child, "ELEMENT_NAME") == "Separator":
                additions.append("separator")
                self.record(child, menu, placement="gallery_menu_separator")
            elif child.find("ELEMENTS/ELEMENT") is not None:
                submenu = self.menu(menu, child, inherited)
                additions.append(submenu)
                self.record(child, menu.find(f"widget[@name='{submenu}']"), self.action(child, inherited), "gallery_submenu")
            else:
                child_action = self.action(child, inherited)
                if child_action:
                    additions.append(child_action)
                    self.record(child, menu, child_action, "gallery_menu")
        for item_name in additions:
            ET.SubElement(menu, "addaction", name=item_name)
        ET.SubElement(toolbar, "addaction", name=name)
        self.properties(menu, node, self.catalog.identifier(node)[0])
        if not paths and not labels:
            self.warning(node, "Gallery entries are generated at runtime; populate the retained dropdown menu in application code.")
        self.warning(node, "Popup gallery preserves icons/items in a standard QMenu; native multi-column gallery layout is approximated.")
        return toolbar

    def gallery_list(self, parent, node, inherited, action):
        widget = ET.SubElement(parent, "widget", {"class": "QListWidget", "name": self.builder.unique("ribbonGallery")})
        prop(widget, "viewMode", "enum", "QListView::IconMode")
        prop(widget, "resizeMode", "enum", "QListView::Adjust")
        prop(widget, "movement", "enum", "QListView::Static")
        prop(widget, "selectionMode", "enum", "QAbstractItemView::SingleSelection")
        size, paths, labels = self.gallery_data(node)
        self.size_policy(widget, "Ignored")
        self.bounds(widget,minimum_height=max(RIBBON_PANEL_ROWS * (self.small_size[1] + RIBBON_ROW_PADDING), size[1] + 14))
        icon_size = prop(widget, "iconSize", "size", "")
        ET.SubElement(icon_size, "width").text = str(size[0])
        ET.SubElement(icon_size, "height").text = str(size[1])
        for index in range(max(len(paths), len(labels))):
            item = ET.SubElement(widget, "item")
            if index < len(labels):
                prop(item, "text", "string", labels[index])
            if index < len(paths) and paths[index]:
                icon = prop(item, "icon", "iconset", "")
                icon.set("resource", self.catalog.assets.qrc_name)
                ET.SubElement(icon, "normaloff").text = paths[index]
        if not paths and not labels:
            self.warning(node, "Gallery items are supplied at runtime; an empty QListWidget retains the command and source settings.")
        if action:
            self.builder.connect(widget.get("name"), "itemClicked(QListWidgetItem*)", action, "trigger()")
        return widget

    def panel(self, parent, node, inherited):
        widget = ET.SubElement(parent, "widget", {"class": "QFrame", "name": self.builder.unique("ribbonPanel_" + text(node, "NAME", "Panel"))})
        prop(widget, "frameShape", "enum", "QFrame::StyledPanel")
        self.size_policy(widget,vertical="Expanding",horizontal="Maximum")
        prop(widget, "accessibleName", "string", text(node, "NAME"))
        self.properties(widget, node)
        children = node.findall("ELEMENTS/ELEMENT")
        explicit_grid = bool(children) and all(all(child.find(tag) is not None for tag in ("ROW", "COLUMN", "ROWSPAN")) for child in children)
        row_count = max(number(text(child,"ROW")) + number(text(child,"ROWSPAN")) for child in children) if explicit_grid else RIBBON_PANEL_ROWS
        self.decision(node, "panel_grid_rows", row_count,
                      "source" if explicit_grid else "approximation",
                      "Complete XML ROW/COLUMN/ROWSPAN grid." if explicit_grid else
                      "A three-row Qt layout approximates MFC adaptive panel layout; complete grid coordinates are absent.")
        self.decision(node, "panel_row_padding", RIBBON_ROW_PADDING, "approximation", "Qt spacing allowance; not a dimension from the MFC resource.")
        layout = self.layout(widget)
        layout.set("stretch","1,0")
        body = ET.SubElement(ET.SubElement(layout, "item"), "widget", {"class": "QWidget", "name": self.builder.unique("ribbonPanelBody")})
        self.size_policy(body,vertical="Expanding")
        self.bounds(body,minimum_height=row_count*(self.small_size[1]+RIBBON_ROW_PADDING))
        grid = self.layout(body, "QGridLayout")
        row, column = 0, 0
        placements = []
        for child in children:
            large = self.large_button(child,inherited) or text(child, "ELEMENT_NAME") == "Group" and any(self.large_button(item,inherited) for item in child.findall("ELEMENTS/ELEMENT"))
            separator = text(child, "ELEMENT_NAME") == "Separator" and not flag(child, "HORIZ")
            inline_gallery=text(child,"ELEMENT_NAME")=="Button_Gallery" and not flag(child,"BUTTON_MODE") and (child.find("IMAGE") is not None or child.findall("ITEMS/ITEM"))
            span = number(text(child, "ROWSPAN"), row_count if large or separator or inline_gallery else 1)
            column_span = number(text(child, "COLSPAN"), 1)
            explicit_row = number(text(child, "ROW"))
            explicit_column = number(text(child, "COLUMN"))
            if span <= 0 or column_span <= 0:
                raise ConversionError("Ribbon row/column spans must be positive")
            if explicit_row is not None or explicit_column is not None:
                current_row, current_column = explicit_row or 0, explicit_column if explicit_column is not None else column
            else:
                if span >= row_count and row:
                    column, row = column + 1, 0
                current_row, current_column = row, column
            if min(current_row, current_column) < 0:
                raise ConversionError("Ribbon grid ROW/COLUMN cannot be negative")
            item = ET.SubElement(grid, "item", row=str(current_row), column=str(current_column), rowspan=str(span), colspan=str(column_span))
            if flag(node, "CENTER_COLUMN_VERT"):
                item.set("alignment", "Qt::AlignVCenter")
            element = self.element(item, child, inherited)
            placements.append({"element": element.get("name"), "row": current_row, "column": current_column,
                               "rowspan": span, "colspan": column_span,
                               "origins": {"row": "source" if explicit_row is not None else "approximation",
                                           "column": "source" if explicit_column is not None else "approximation",
                                           "rowspan": "source" if child.find("ROWSPAN") is not None else "approximation",
                                           "colspan": "source" if child.find("COLSPAN") is not None else "platform_default"}})
            if span >= row_count:
                row, column = 0, current_column + column_span
            else:
                row, column = current_row + span, current_column
                if row >= row_count:
                    row, column = 0, column + column_span
        if flag(node, "JUSTIFY_COLUMNS"):
            grid.set("columnstretch", ",".join("1" for _ in range(max([item["column"] + item["colspan"] for item in placements] or [1]))))
        if not flag(node,"CENTER_COLUMN_VERT"):
            rows=max([row_count]+[item["row"]+item["rowspan"] for item in placements])
            grid.set("rowminimumheight",",".join(str(self.small_size[1]+RIBBON_ROW_PADDING) for _ in range(rows)))
            grid.set("rowstretch",",".join("1" for _ in range(rows)))
        footer = ET.SubElement(ET.SubElement(layout, "item"), "widget", {"class": "QWidget", "name": self.builder.unique("ribbonPanelFooter")})
        self.size_policy(footer)
        footer_layout = self.layout(footer, "QHBoxLayout")
        label = ET.SubElement(ET.SubElement(footer_layout, "item",alignment="Qt::AlignBottom"), "widget", {"class": "QLabel", "name": self.builder.unique("ribbonPanelCaption")})
        prop(label, "text", "string", text(node, "NAME"))
        prop(label, "alignment", "set", "Qt::AlignCenter")
        for tag in ("BUTTON_LAUNCH", "LAUNCH_BUTTON"):
            launch = node.find(tag)
            if launch is not None:
                self.element(ET.SubElement(footer_layout, "item"), launch, inherited)
        return {"name": text(node, "NAME"), "widget": widget.get("name"), "placements": placements}

    def categories(self):
        bar = self.ribbon.bar
        result = [(node, None) for node in bar.findall("CATEGORIES/CATEGORY")]
        for wrapper in ("CONTEXTS/CONTEXT", "CONTEXT_CATEGORIES/CONTEXT", "CONTEXT_CATEGORIES/CONTEXT_CATEGORY"):
            for context in bar.findall(wrapper):
                for node in context.findall("CATEGORIES/CATEGORY") + context.findall("CATEGORY"):
                    result.append((node, context))
        return result

    def header(self, parent, inherited, qat_only=False, show_qat=True):
        bar = self.ribbon.bar
        header = ET.SubElement(parent, "widget", {"class": "QWidget", "name": self.builder.unique("ribbonHeader")})
        self.size_policy(header)
        layout = self.layout(header, "QHBoxLayout")
        main = bar.find("BUTTON_MAIN")
        main_category = bar.find("CATEGORY_MAIN")
        if main_category is not None and not qat_only:
            # File/Application commands become a real popup in the header.
            wrapper = self.popup_bar(ET.SubElement(layout,"item"),"ribbonApplicationButton")
            menu = self.menu(wrapper, main_category, self.inherited_images(main_category, inherited), text(main_category, "NAME", text(main, "TOOLTIP", "File")))
            if main is not None:
                image = self.icon(main, inherited, True)
                if image:
                    icon = prop(wrapper.find(f"widget[@name='{menu}']"), "icon", "iconset", "")
                    icon.set("resource", self.catalog.assets.qrc_name)
                    ET.SubElement(icon, "normaloff").text = image
                self.properties(wrapper, main, self.catalog.identifier(main)[0])
                self.record(main, wrapper, placement="application")
            ET.SubElement(wrapper, "addaction", name=menu)
            recent = main_category.find("RECENT_FILE_LIST")
            if recent is not None and flag(recent, "ENABLE"):
                self.warning(recent, "Recent-file entries are generated at runtime; populate the retained application menu from application settings.")
        qat = bar.find("QAT_ELEMENTS")
        if qat is not None and show_qat:
            toolbar = ET.SubElement(ET.SubElement(layout, "item"), "widget", {"class": "QToolBar", "name": self.builder.unique("ribbonQuickAccess")})
            prop(toolbar, "movable", "bool", False)
            prop(toolbar, "floatable", "bool", False)
            prop(toolbar, "toolButtonStyle", "enum", "Qt::ToolButtonIconOnly")
            icon_size=prop(toolbar,"iconSize","size","")
            ET.SubElement(icon_size,"width").text=str(self.small_size[0])
            ET.SubElement(icon_size,"height").text=str(self.small_size[1])
            self.properties(toolbar, qat)
            self.size_policy(toolbar,horizontal="Maximum")
            for item in qat.findall("ITEMS/ITEM"):
                command, _ = self.catalog.identifier(item)
                if command is None:
                    ET.SubElement(toolbar, "addaction", name="separator")
                    continue
                definition, images = self.definitions.get(command, (item, inherited))
                name = self.action(definition, images)
                if name and flag(item, "VISIBLE", True):
                    ET.SubElement(toolbar, "addaction", name=name)
                self.record(item, toolbar, name, "quick_access" if flag(item, "VISIBLE", True) else "quick_access_hidden")
        self.spacer(layout)
        tab_elements = bar.find("TAB_ELEMENTS")
        if tab_elements is not None and not qat_only:
            for node in tab_elements.findall("ELEMENTS/ELEMENT"):
                self.element(ET.SubElement(layout, "item"), node, inherited)
        return header

    def render(self, central):
        bar = self.ribbon.bar
        inherited = {"small": bar.find("IMAGE"), "large": None}
        inherited = self.inherited_images(bar, inherited)
        self.collect(bar, inherited)
        root_layout = self.layout(central, "QVBoxLayout", "ribbonRootLayout")
        top = flag(bar.find("QAT_ELEMENTS"), "QAT_TOP", True) if bar.find("QAT_ELEMENTS") is not None else True
        if top or bar.find("CATEGORY_MAIN") is not None or bar.find("TAB_ELEMENTS") is not None:
            self.header(ET.SubElement(root_layout, "item"), inherited, show_qat=top)
        tabs = ET.SubElement(ET.SubElement(root_layout, "item"), "widget", {"class": "QTabWidget", "name": self.builder.unique("ribbonTabs")})
        prop(tabs, "documentMode", "bool", True)
        prop(tabs, "usesScrollButtons", "bool", True)
        policy = prop(tabs, "sizePolicy", "sizepolicy", "")
        policy.set("hsizetype", "Expanding")
        policy.set("vsizetype", "Fixed")
        ET.SubElement(policy, "horstretch").text = "0"
        ET.SubElement(policy, "verstretch").text = "0"
        self.properties(tabs, bar)
        for category, context in self.categories():
            images = self.inherited_images(category, inherited)
            page = ET.SubElement(tabs, "widget", {"class": "QWidget", "name": self.builder.unique("ribbonTab_" + text(category, "NAME", "Tab"))})
            attribute = ET.SubElement(page, "attribute", name="title")
            ET.SubElement(attribute, "string").text = text(category, "NAME")
            self.properties(page, category)
            if context is not None:
                prop(page, "RcRibbonContext", "string", text(context, "NAME", text(context, "TEXT")), True)
                prop(page, "RcRibbonContextXml", "string", ET.tostring(context, encoding="unicode"), True)
            if category.find("VISIBLE") is not None and not flag(category, "VISIBLE", True):
                prop(page, "RcRibbonInitiallyVisible", "bool", False, True)
                self.warning(category, "Hidden/contextual tabs are retained; set tab visibility from application code.")
            page_layout = self.layout(page)
            scroll = ET.SubElement(ET.SubElement(page_layout, "item"), "widget", {"class": "QScrollArea", "name": self.builder.unique("ribbonPanelScroll")})
            prop(scroll, "widgetResizable", "bool", True)
            prop(scroll,"sizeAdjustPolicy","enum","QAbstractScrollArea::AdjustToContents")
            prop(scroll,"verticalScrollBarPolicy","enum","Qt::ScrollBarAlwaysOff")
            prop(scroll, "frameShape", "enum", "QFrame::NoFrame")
            contents = ET.SubElement(scroll, "widget", {"class": "QWidget", "name": self.builder.unique("ribbonPanels")})
            panels_layout = self.layout(contents, "QHBoxLayout")
            prop(panels_layout,"sizeConstraint","enum","QLayout::SetMinimumSize")
            tab_report = {"name": text(category, "NAME"), "widget": page.get("name"),
                          "context": text(context, "NAME", text(context, "TEXT")) if context is not None else None, "panels": []}
            for panel in category.findall("PANELS/PANEL"):
                tab_report["panels"].append(self.panel(ET.SubElement(panels_layout, "item"), panel, images))
            # Hidden category commands remain QAction definitions and metadata.
            for hidden in category.findall("HIDDEN_ELEMENTS/ELEMENT"):
                action = self.action(hidden, images)
                self.record(hidden, page, action, "hidden")
            self.spacer(panels_layout)
            self.report["tabs"].append(tab_report)
        if not top:
            self.header(ET.SubElement(root_layout, "item"), inherited, qat_only=True)
        if self.report["tabs"]:
            prop(tabs, "currentIndex", "number", 0)
        workspace = ET.SubElement(ET.SubElement(root_layout, "item"), "widget", {"class": "QWidget", "name": self.builder.unique("ribbonWorkspace")})
        self.bounds(workspace,minimum_height=1)
