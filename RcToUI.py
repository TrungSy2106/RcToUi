#!/usr/bin/env python3
"""Convert Windows/MFC RC and Ribbon XML directly to Qt Designer resources.

Usage: py RcToUI.py App.rc [-o output_dir] [-I include_dir] [-D NAME=value]

Requires Python 3.9+ and only the standard library; no Windows SDK is needed.
Automatically resolves project headers, images, Ribbon XML and included RC files.
Exports Dialog/Menu/Toolbar/Ribbon to .ui, images to .qrc, and strings to .ts.
Default output: <input_stem>_qt/ beside the source, or the directory set by -o.
Compatible LANGUAGE variants share a .ui and Qt .ts catalogs automatically.
Use --separate-languages to retain the original per-language UI output.
"""

import argparse
from pathlib import Path
import re
import sys


# Shared RC literal syntax; the preprocessor supplies decoding and macro logic.
STRING = re.compile(r'(?:L)?"(?:[^"\\]|\\.|"")*"')


class ConversionError(ValueError):
    pass


class IncompleteConversionError(ConversionError):
    """Valid outputs/report were saved, but the conversion has errors."""
    def __init__(self, status, outputs, report_path):
        self.outputs = outputs
        self.report_path = report_path
        super().__init__(f"Conversion {status}: result is incomplete; see {report_path}")


def diagnostic(severity, code, file, line, resource_id, resource_type, message, control=None):
    location = f"{file}:{line}"
    identity = f" resource {resource_id!r}:" if resource_id is not None else ""
    return {"severity": severity, "code": code, "file": str(file), "line": line,
            "resource_id": resource_id, "resource_type": resource_type,
            "control": control, "message": f"{location}:{identity} {message}"}


def convert(rc_path, output_dir=None, encoding=None, **options):
    from mfc_rc import convert_mfc
    return convert_mfc(rc_path, output_dir, encoding, **options)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rc", type=Path, help="Windows/MFC .rc/.rc2 or .mfcribbon-ms")
    parser.add_argument("-o", "--output-dir", type=Path, help="output root; default: <input_stem>_qt beside the source")
    parser.add_argument("--encoding", help="source encoding override for files without a Unicode BOM")
    parser.add_argument("-I", "--include", action="append", default=[], type=Path, help="additional project include directory")
    parser.add_argument("-D", "--define", action="append", default=[], help="preprocessor macro NAME or NAME=value")
    parser.add_argument("-U", "--undefine", action="append", default=[], help="undefine a predefined macro")
    parser.add_argument("--scale", type=float, help="explicit uniform DLU scale; default: Windows font metrics at 96 DPI")
    parser.add_argument("--language", type=lambda value: int(value, 0), help="filter language ID, e.g. 0x409")
    parser.add_argument("--source-language", type=lambda value: int(value, 0), help="preferred base language for shared UI/translations, e.g. 0x409")
    parser.add_argument("--separate-languages", action="store_true", help="keep separate UI files per language instead of merging compatible forms")
    parser.add_argument("--strict", action="store_true", help="fail before writing if any conversion warning exists")
    parser.add_argument("--widget-map", action="append", default=[], metavar="NATIVE=QT", help="explicit native class to built-in Qt widget mapping")
    parser.add_argument("--dlginit-encoding", help="encoding of ANSI strings in MFC DLGINIT")
    parser.add_argument("--ribbon-resources", type=Path, help="RC source providing IDs/icons for standalone .mfcribbon-ms")
    parser.add_argument("--ribbon-target", help="merge Ribbon into an existing command frame by numeric/symbolic resource ID")
    args = parser.parse_args(argv)
    try:
        paths = convert(args.rc, output_dir=args.output_dir, encoding=args.encoding,
                        include_dirs=args.include, defines=args.define, undefines=args.undefine,
                        scale=args.scale, language=args.language, strict=args.strict,
                        source_language=args.source_language, separate_languages=args.separate_languages,
                        widget_map=args.widget_map, dlginit_encoding=args.dlginit_encoding,
                        ribbon_resources=args.ribbon_resources, ribbon_target=args.ribbon_target)
        for path in paths:
            print(path)
    except IncompleteConversionError as exc:
        for path in exc.outputs:
            print(path)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, LookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.modules.setdefault("RcToUI", sys.modules[__name__])
    sys.exit(main())
