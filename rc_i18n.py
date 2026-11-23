"""Merge compatible localized forms and export Qt text translation catalogs.

Only text may differ. Native IDs, control order, geometry, state, connections,
and image contents must agree. Ambiguous forms stay separate and are reported.
"""

from collections import Counter, defaultdict
import hashlib
import json
import locale
import posixpath
import xml.etree.ElementTree as ET

from mfc_rc import safe_name, serialize


OBJECTS = {"widget", "action", "actiongroup", "layout", "buttongroup"}
SOURCE_METADATA = {"RcLanguage", "RcMenuText", "RcStringTableText", "RcFont", "RcRibbonContext"}
RIBBON_TEXT = {"NAME", "TEXT", "TOOLTIP", "DESCRIPTION", "TOOLTIP_DESCRIPTION"}
REFERENCES = {"sender", "receiver", "tabstop", "zorder"}


def preferred_language(languages, requested=None):
    for language in (requested, 0x409, 0):
        if language in languages:
            return language
    return min(languages)


class Form:
    def __init__(self, data, images):
        self.root = ET.fromstring(data)
        self.paths, self.names, self.strings, self.signature = {}, {}, {}, {}

        def walk(node, path):
            self.paths[node] = path
            if node.tag in OBJECTS and node.get("name"):
                self.names[node.get("name")] = path
            counts = Counter()
            for child in node:
                key = (child.tag, child.get("name")) if child.tag in {"property", "attribute"} else (child.tag, None)
                counts[key] += 1
                if child.tag in {"property", "attribute"}:
                    part = child.tag + "[" + child.get("name") + "]" + (f"[{counts[key]}]" if counts[key] > 1 else "")
                else:
                    part = child.tag + f"[{counts[key]}]"
                walk(child, path + "/" + part)

        walk(self.root, "ui")

        def snapshot(node, parent=None):
            if node.tag == "property" and node.get("name") in SOURCE_METADATA:
                return None
            path = self.paths[node]
            attributes = dict(node.attrib)
            if "name" in attributes and node.tag in OBJECTS | {"addaction"}:
                attributes["name"] = self.names.get(attributes["name"], attributes["name"])
            value = node.text or ""
            if node.tag == "string" and node.get("notr", "false").lower() not in {"true", "1", "yes"}:
                self.strings[path] = node
                value = "<translated text>"
            elif node.tag == "string" and parent is not None and parent.get("name") == "RcRibbonProperties":
                values = json.loads(value)
                value = json.dumps({k: v for k, v in values.items() if k not in RIBBON_TEXT}, sort_keys=True)
            elif node.tag in REFERENCES or node.tag == "cstring" and parent is not None and parent.get("name") == "buddy":
                value = self.names.get(value, value)
            elif node.tag in {"pixmap", "normaloff", "normalon", "disabledoff", "disabledon",
                              "activeoff", "activeon", "selectedoff", "selectedon"}:
                value = images.get(value, value)
            children = tuple(self.paths[child] for child in node if snapshot(child, node))
            self.signature[path] = (node.tag, tuple(sorted(attributes.items())),
                                    value if not len(node) else value.strip(), children)
            return True

        snapshot(self.root)

    def difference(self, other):
        for path in sorted(self.signature.keys() | other.signature.keys()):
            if self.signature.get(path) != other.signature.get(path):
                return "Non-text UI structure or image differs at " + path
        for path, node in self.strings.items():
            if bool(node.text) != bool(other.strings[path].text):
                return "Empty/non-empty text differs at " + path
        return None


def ambiguous_controls(record):
    controls = defaultdict(list)
    for control in record.get("controls", []):
        controls[control["id"]].append(control)
    for identifier, items in controls.items():
        if len(items) < 2:
            continue
        if identifier != -1:
            return f"Repeated control ID {identifier!r} cannot be matched uniquely."
        locations = [(item["native_class"], tuple(item["rect_dlu"]), item["style"], item["exstyle"])
                     for item in items]
        if len(set(locations)) != len(locations):
            return "Anonymous static controls have identical class, geometry and style; correspondence is ambiguous."
    return None


def remap_actions(actions, names):
    for action in actions:
        for key in ("name", "primary"):
            if key in action:
                action[key] = names.get(action[key], action[key])


def remap_ribbon(value, names):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"widget", "element", "action"} and isinstance(child, str):
                value[key] = names.get(child, child)
            else:
                remap_ribbon(child, names)
    elif isinstance(value, list):
        for child in value:
            remap_ribbon(child, names)


def merge_ui_languages(outputs, dialogs, commands, assets, stem, *, separate=False, source_language=None):
    report = {"mode": "separate" if separate else "merge-compatible", "preferred_source_language": source_language,
              "merged_forms": [], "separate_forms": [], "ts_files": []}
    if separate:
        return report
    images = {assets.url(path): hashlib.sha256(data).hexdigest() for path, data in assets.files.items()}
    families = defaultdict(list)
    for kind, records in (("dialog", dialogs), ("frame", commands.forms)):
        for record in records:
            role = "ribbon" if record.get("ribbon") is not None else kind
            families[(role, record["resource"])].append(record)
    catalogs = defaultdict(lambda: defaultdict(list))
    source_languages = defaultdict(set)

    def keep(record, kind, reason):
        report["separate_forms"].append({"resource": record["resource"], "kind": kind,
            "language": record["language"], "file": record["file"], "reason": reason})

    for (kind, identifier), records in families.items():
        if len(records) < 2:
            continue
        language = preferred_language({r["language"] for r in records}, source_language)
        base_record = next(r for r in records if r["language"] == language)
        reason = ambiguous_controls(base_record)
        if reason:
            for record in records:
                keep(record, kind, reason)
            continue
        base = Form(outputs[base_record["file"]], images)
        compatible = [(base_record, base)]
        for record in records:
            if record is base_record:
                continue
            form = Form(outputs[record["file"]], images)
            reason = ambiguous_controls(record) or base.difference(form)
            if reason:
                keep(record, kind, reason)
            else:
                compatible.append((record, form))
        if len(compatible) < 2:
            keep(base_record, kind, "No other language has a safely compatible UI structure.")
            continue
        context = base.root.findtext("class")
        destination = posixpath.join(posixpath.dirname(base_record["file"]), safe_name(context) + ".ui")
        old_files = {r["file"] for r, _ in compatible}
        if any(p.lower() == destination.lower() and p not in old_files for p in outputs):
            for record, _ in compatible:
                keep(record, kind, "The shared UI filename would overwrite another output.")
            continue
        group = {"resource": identifier, "kind": kind, "file": destination, "context": context,
                 "source_language": language, "languages": sorted(r["language"] for r, _ in compatible),
                 "message_count": 0, "matching": "Same native IDs and control order; non-text properties and image bytes agree."}
        for path, node in base.strings.items():
            if not node.text:
                continue
            comment = f"{stem}:{kind}:{identifier}:{path}"
            node.set("comment", comment)
            group["message_count"] += 1
            for record, form in compatible:
                target_language = record["language"]
                catalogs[target_language][context].append((node.text, comment, form.strings[path].text, destination))
                source_languages[target_language].add(language)
        base_names = {path: name for name, path in base.names.items()}
        for record, form in compatible:
            names = {name: base_names[path] for name, path in form.names.items() if base_names[path] != name}
            outputs.pop(record["file"])
            record["file"] = destination
            record["i18n"] = {"source_language": language, "context": context, "object_name_map": names}
            for control in record.get("controls", []):
                control["name"] = names.get(control["name"], control["name"])
            remap_actions(record.get("actions", []), names)
            for binding in commands.dialog_bindings:
                if kind == "dialog" and binding["dialog"] == record["identifier"] and binding["language"] == record["language"]:
                    remap_actions(binding["actions"], names)
            if commands.ribbon is not None:
                for ribbon in commands.ribbon.report:
                    if kind == "ribbon" and ribbon["resource"] == record["ribbon"] and ribbon["language"] == record["language"]:
                        remap_ribbon(ribbon, names)
        outputs[destination] = serialize(base.root)
        report["merged_forms"].append(group)

    for language, contexts in sorted(catalogs.items()):
        ts = ET.Element("TS", version="2.1", language=locale.windows_locale.get(language, "C"))
        if len(source_languages[language]) == 1:
            ts.set("sourcelanguage", locale.windows_locale.get(next(iter(source_languages[language])), "C"))
        for name, messages in sorted(contexts.items()):
            context = ET.SubElement(ts, "context")
            ET.SubElement(context, "name").text = name
            for source, comment, translation, file in messages:
                message = ET.SubElement(context, "message")
                ET.SubElement(message, "location", filename=posixpath.relpath(file, "translations"))
                ET.SubElement(message, "source").text = source
                ET.SubElement(message, "comment").text = comment
                ET.SubElement(message, "translation").text = translation
        filename = f"translations/{safe_name(stem)}_ui_{language:04x}.ts"
        outputs[filename] = serialize(ts)
        report["ts_files"].append(filename)
    if report["merged_forms"]:
        outputs["translations/README.txt"] = TRANSLATION_HELP.encode("utf-8")
        outputs["translations/manifest.json"] = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return report


TRANSLATION_HELP = """Qt translations generated from localized RC resources

Only compatible forms are shared. conversion_report.json / translations lists
the source language, shared forms and variants retained as separate UI files.
The preferred source is --source-language if present in a resource family;
otherwise English (0x0409), neutral (0), then the lowest available language ID.
Text-only variants share a form. Changed geometry, font, controls, image data,
or ambiguous control identities retain separate forms with I18N_VARIANT warnings.
Control/menu order is preserved; differently ordered trees are not guessed.
The metadata in a shared UI comes from its source language; original resource
data and per-language records remain in conversion_report.json.

Build: run Qt's lrelease on the desired .ts file, for example:
    lrelease app_ui_0411.ts -qm app_ui_0411.qm
Replace the filename with one listed in manifest.json. The converter itself
does not require Qt or invoke lrelease. Existing STRINGTABLE .ts files in
../strings use a separate translation context and can be loaded as well.

C++ startup (keep translator alive as long as it is installed):
    QTranslator translator;
    if (translator.load(qmPath))
        app.installTranslator(&translator);
    // Create/setup the widgets after installing the translator.

For live switching with uic-generated widgets, handle QEvent::LanguageChange
in changeEvent() and call ui->retranslateUi(this), then the base changeEvent().
For QUiLoader, call loader.setLanguageChangeEnabled(true) before load().
Keep the loader alive for subsequent language-change handling.

PySide6/PyQt startup uses QTranslator and app.installTranslator in the same way.
Keep a Python reference to the translator. Loading another .qm requires the
same LanguageChange handling described above for already visible widgets.

Translations contain the text already supplied in RC; no machine translation
or missing translations are invented. Use --separate-languages to retain the
original per-language forms, or --language to export a single language only.
"""
