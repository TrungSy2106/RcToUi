"""Project-local RC preprocessing, with no compiler, shell, SDK or eval().

Byte files follow active code_page directives. A Unicode BOM or --encoding
fixes the physical source encoding; pragmas still control narrow byte escapes.
"""

from dataclasses import dataclass, replace
from pathlib import Path
import codecs
import os
import re
import xml.etree.ElementTree as ET

from RcToUI import ConversionError, STRING, diagnostic


@dataclass(frozen=True)
class Token:
    text: str
    path: str
    line: int
    kind: str = "symbol"
    encoding: str = "cp1252"
    hidden: frozenset = frozenset()
    leading_space: bool = False


PATTERN = re.compile(STRING.pattern + r"|(?:L)?'(?:[^'\\]|\\.)*'|0[xX][0-9a-fA-F]+[uUlL]*|\d+[uUlL]*|[^\W\d]\w*|\$\w+|\.\.\.|##|<<|>>|&&|\|\||<=|>=|==|!=|[^\s]", re.UNICODE)
IDENT = re.compile(r"^[^\W\d]\w*$", re.UNICODE)
COMMENT_OR_LITERAL = re.compile(STRING.pattern + r"|(?:L)?'(?:[^'\\]|\\.)*'|//.*|/\*")


def strip_comments(value, in_comment=False):
    """Strip comments from a decoded logical line, retaining literal contents."""
    result, position = [], 0
    while position < len(value):
        if in_comment:
            end = value.find("*/", position)
            if end < 0:
                result.append(" " * (len(value) - position))
                break
            result.append(" " * (end + 2 - position))
            position, in_comment = end + 2, False
            continue
        match = COMMENT_OR_LITERAL.search(value, position)
        if match is None:
            result.append(value[position:])
            break
        result.append(value[position:match.start()])
        token = match[0]
        result.append(" " * len(token) if token.startswith(("//", "/*")) else token)
        position = match.end()
        if token == "/*":
            in_comment = True
    return "".join(result), in_comment


def lex(value, path="<expression>", line=1, encoding="cp1252"):
    result = []
    end = 0
    for match in PATTERN.finditer(value):
        spelling = match[0]
        kind = "string" if spelling.startswith(('"', 'L"')) else "char" if spelling.startswith(("'", "L'")) else "id" if IDENT.fullmatch(spelling) else "number" if spelling[0].isdigit() else "symbol"
        result.append(Token(spelling, str(path), line + value.count("\n", 0, match.start()), kind, encoding,
                            leading_space=bool(value[end:match.start()])))
        end = match.end()
    return result


def rc_text(tokens):
    """Decode RC byte escapes (2 hex digits) and wide escapes (4 digits)."""
    parts = []
    escapes = {"n":"\n", "r":"\r", "t":"\t", "b":"\b", "f":"\f", "a":"\a", "v":"\v", "\\":"\\", '"':'"', "'":"'"}
    for token in tokens:
        value = token.text
        if token.kind not in {"string", "char"}:
            raise ConversionError(f"expected RC string, found {value!r}")
        wide = value.startswith("L")
        body = value[2:-1] if wide else value[1:-1]
        output, pending, pos = [], bytearray(), 0
        def flush():
            if pending:
                output.append(bytes(pending).decode(token.encoding))
                pending.clear()
        while pos < len(body):
            if body[pos:pos+2] == '""':
                flush(); output.append('"'); pos += 2; continue
            char = body[pos]
            if char != "\\":
                flush(); output.append(char); pos += 1; continue
            pos += 1
            if pos >= len(body):
                raise ConversionError("unterminated string escape")
            char = body[pos]
            numeric = re.match(r"x[0-9a-fA-F]{1," + ("4" if wide else "2") + r"}|[0-7]{1,3}", body[pos:])
            if numeric:
                digits = numeric[0]
                number = int(digits[1:],16) if digits.startswith("x") else int(digits,8)
                if wide:
                    flush(); output.append(chr(number))
                else:
                    if number > 255: raise ConversionError("RC byte escape exceeds 255; use an L-prefixed string")
                    pending.append(number)
                pos += len(digits)
            else:
                flush(); output.append(escapes.get(char, "\\" + char)); pos += 1
        flush()
        decoded = "".join(output)
        if wide:
            decoded = decoded.encode("utf-16-le",errors="surrogatepass").decode("utf-16-le")
        parts.append(decoded)
    return "".join(parts)


@dataclass(frozen=True)
class Integer:
    value: int
    bits: int = 32
    unsigned: bool = False

    def cast(self, bits, unsigned):
        value = self.value & ((1 << bits) - 1)
        if not unsigned and value >= 1 << (bits - 1): value -= 1 << bits
        return Integer(value,bits,unsigned)


class Expression:
    PRECEDENCE = {"||":1,"&&":2,"|":3,"^":4,"&":5,"==":6,"!=":6,"<":7,"<=":7,">":7,">=":7,"<<":8,">>":8,"+":9,"-":9,"*":10,"/":10,"%":10}
    TYPES = {"int", "long", "short", "char", "unsigned", "signed", "DWORD", "WORD", "BYTE", "UINT", "INT", "LONG", "ULONG", "DWORD_PTR", "UINT_PTR", "LPARAM", "WPARAM", "BOOL"}

    def __init__(self, tokens, undefined_zero=False):
        self.tokens = tokens; self.pos = 0; self.undefined_zero = undefined_zero

    def peek(self): return self.tokens[self.pos].text if self.pos < len(self.tokens) else ""
    def take(self):
        if self.pos >= len(self.tokens): raise ConversionError("incomplete integer expression")
        token=self.tokens[self.pos]; self.pos+=1; return token

    def node(self, minimum=0):
        token = self.take(); value = token.text
        if value in {"+","-","~","!"}:
            left = ("unary",value,self.node(11))
        elif value == "(":
            start = self.pos
            while self.peek() in self.TYPES: self.pos += 1
            if self.pos > start and self.peek() == ")":
                types=[item.text for item in self.tokens[start:self.pos]];self.pos+=1
                left=("cast",types,self.node(11))
            else:
                self.pos=start; left=self.node()
                if self.take().text != ")": raise ConversionError("unbalanced expression parentheses")
        elif token.kind == "number":
            match = re.fullmatch(r"(0[xX][0-9a-fA-F]+|\d+)([uUlL]*)",value)
            if not match: raise ConversionError(f"unsupported integer literal {value!r}")
            digits,suffix=match.groups()
            number=int(digits,16) if digits.lower().startswith("0x") else int(digits,8) if digits.startswith("0") and len(digits)>1 else int(digits)
            hexadecimal=digits.lower().startswith("0x") or len(digits)>1 and digits.startswith("0")
            bits=64 if suffix.lower().count("l")>1 or number>0xffffffff or not hexadecimal and "u" not in suffix.lower() and number>0x7fffffff else 32
            unsigned="u" in suffix.lower() or hexadecimal and number>=(1<<(bits-1))
            if number>0xffffffffffffffff:raise ConversionError("integer literal exceeds 64 bits")
            left=("literal",Integer(number,bits,unsigned))
        elif token.kind == "char":
            decoded=rc_text([token])
            if len(decoded)!=1: raise ConversionError("multi-character C character constants are unsupported")
            left=("literal",Integer(ord(decoded)))
        elif token.kind == "id" and self.undefined_zero:
            left=("literal",Integer(0))
        else:
            raise ConversionError(f"unresolved/unsupported integer expression token {value!r}")
        while self.peek() in self.PRECEDENCE and self.PRECEDENCE[self.peek()] >= minimum:
            operator=self.take().text;right=self.node(self.PRECEDENCE[operator]+1)
            left=("binary",operator,left,right)
        if minimum == 0 and self.peek() == "?":
            self.take();yes=self.node()
            if self.take().text != ":": raise ConversionError("ternary expression is missing ':'")
            left=("ternary",left,yes,self.node())
        return left

    def evaluate(self):
        if not self.tokens: raise ConversionError("empty integer expression")
        node=self.node()
        if self.pos != len(self.tokens): raise ConversionError("unsupported integer expression: " + " ".join(t.text for t in self.tokens[self.pos:]))
        return self.compute(node).value

    def compute(self, node):
        kind=node[0]
        if kind=="literal": return node[1]
        if kind=="ternary":
            bits,unsigned=self.result_type(node)
            return self.compute(node[2] if self.compute(node[1]).value else node[3]).cast(bits,unsigned)
        if kind=="cast":
            names=node[1]; bits=8 if any(x in {"BYTE","char"} for x in names) else 16 if any(x in {"WORD","short"} for x in names) else 64 if names.count("long")>1 or any(x in {"DWORD_PTR","UINT_PTR","LPARAM","WPARAM"} for x in names) else 32
            return self.compute(node[2]).cast(bits,"unsigned" in names or any(x in {"DWORD","WORD","BYTE","UINT","ULONG","DWORD_PTR","UINT_PTR","WPARAM"} for x in names))
        if kind=="unary":
            operator=node[1];value=self.compute(node[2])
            if value.bits<32:value=Integer(value.value)
            if operator=="!": return Integer(int(not value.value))
            result=value.value if operator=="+" else -value.value if operator=="-" else ~value.value
            return Integer(result,value.bits,value.unsigned).cast(value.bits,value.unsigned)
        operator=node[1];left=self.compute(node[2])
        if left.bits<32:left=Integer(left.value)
        if operator=="&&" and not left.value:return Integer(0)
        if operator=="||" and left.value:return Integer(1)
        right=self.compute(node[3])
        if right.bits<32:right=Integer(right.value)
        if operator in {"&&","||"}: return Integer(int(bool(right.value)))
        if operator in {"<<",">>"}:
            if not 0<=right.value<64:raise ConversionError("invalid C shift count")
            value=left.value<<right.value if operator=="<<" else left.value>>right.value
            return Integer(value,left.bits,left.unsigned).cast(left.bits,left.unsigned)
        bits=max(left.bits,right.bits)
        unsigned=left.unsigned if left.bits>right.bits else right.unsigned if right.bits>left.bits else left.unsigned or right.unsigned
        a,b=left.cast(bits,unsigned).value,right.cast(bits,unsigned).value
        if operator in {"==","!=","<","<=",">",">="}:
            return Integer(int({"==":a==b,"!=":a!=b,"<":a<b,"<=":a<=b,">":a>b,">=":a>=b}[operator]))
        if operator in {"/","%"}:
            if not b:raise ConversionError("division by zero in integer expression")
            quotient=abs(a)//abs(b)*(-1 if (a<0)!=(b<0) else 1)
            value=quotient if operator=="/" else a-quotient*b
        else:
            value={"+":lambda:a+b,"-":lambda:a-b,"*":lambda:a*b,"|":lambda:a|b,"&":lambda:a&b,"^":lambda:a^b}[operator]()
        return Integer(value,bits,unsigned).cast(bits,unsigned)

    def result_type(self,node):
        """C integer conversions without evaluating an unselected branch."""
        kind=node[0]
        if kind=="literal":return node[1].bits,node[1].unsigned
        if kind=="cast":
            # Cast the type only; the operand may contain an unevaluated 1/0.
            value=self.compute(("cast",node[1],("literal",Integer(0))))
            return value.bits,value.unsigned
        if kind=="unary":
            if node[1]=="!":return 32,False
            bits,unsigned=self.result_type(node[2]);return (32,False) if bits<32 else (bits,unsigned)
        if kind=="binary" and node[1] in {"&&","||","==","!=","<","<=",">",">="}:return 32,False
        left,right=node[2],node[3]
        a,ua=self.result_type(left);b,ub=self.result_type(right)
        if a<32:a,ua=32,False
        if b<32:b,ub=32,False
        if kind=="binary" and node[1] in {"<<",">>"}:return a,ua
        return max(a,b),ua if a>b else ub if b>a else ua or ub


@dataclass
class Macro:
    body: list
    parameters: object = None
    variadic: bool = False


class ProjectFiles:
    SKIP = {".git",".vs","node_modules","__pycache__","debug","release","x64","x86","build","dist","out"}

    def __init__(self, entry, include_dirs=()):
        self.entry=Path(entry).resolve();self.root=self.entry.parent
        for parent in list(self.entry.parents)[:4]:
            if list(parent.glob("*.vcxproj")) or list(parent.glob("*.sln")):
                self.root=parent;break
        self.includes=[Path(value).resolve() for value in include_dirs]
        self.index=None;self.opened=set();self.discovered=[]
        for project in self.root.glob("*.vcxproj"):
            try: root=ET.parse(project).getroot()
            except (OSError,ET.ParseError):continue
            for node in root.iter():
                if node.tag.rsplit("}",1)[-1] not in {"AdditionalIncludeDirectories","IncludePath"}:continue
                for value in (node.text or "").split(";"):
                    for key,replacement in (("$(ProjectDir)",str(project.parent)+"/"),("$(SolutionDir)",str(self.root)+"/")):
                        value=value.replace(key,replacement)
                    if not value or "$" in value or "%" in value:continue
                    folder=Path(value.replace("\\","/"));folder=folder if folder.is_absolute() else project.parent/folder
                    if folder.is_dir() and folder.resolve() not in self.includes:self.includes.append(folder.resolve())

    def direct(self, name, source):
        normalized=name.replace("\\","/")
        for base in (Path(source).parent,self.entry.parent,*self.includes,self.root):
            path=Path(normalized) if Path(normalized).is_absolute() else base/normalized
            if path.is_file():return path.resolve()
            if os.name!="nt":
                current=Path(path.anchor)
                try:
                    for component in path.parts[1:]:
                        if component=="..":current=current.parent;continue
                        if component==".":continue
                        choices=[item for item in current.iterdir() if item.name.casefold()==component.casefold()]
                        if len(choices)!=1:break
                        current=choices[0]
                    else:
                        if current.is_file():return current.resolve()
                except OSError:pass
        return None

    def find(self, name, source):
        direct=self.direct(name,source)
        if direct:return direct
        if self.index is None:
            self.index=[]
            for folder,dirs,files in os.walk(self.root):
                dirs[:]=[d for d in dirs if d.lower() not in self.SKIP]
                self.index.extend(Path(folder)/name for name in files)
                if len(self.index)>100000:raise ConversionError("project file search exceeds 100000 files; pass narrower -I paths")
        normalized=name.replace("\\","/").lstrip("./").lower()
        suffix=[path for path in self.index if path.as_posix().lower().endswith("/"+normalized)]
        candidates=suffix or [path for path in self.index if path.name.lower()==Path(normalized).name.lower()]
        if len(candidates)==1:
            self.discovered.append({"requested":name,"resolved":str(candidates[0]),"from":str(source)})
            return candidates[0].resolve()
        if candidates:raise ConversionError(f"ambiguous project path {name!r}: " + ", ".join(str(x) for x in candidates[:5]))
        raise ConversionError(f"file not found in project: {name!r}")


class Preprocessor:
    BUILTIN_HEADERS = {"windows.h","winuser.h","winres.h","winresrc.h","afxres.h","afxresrc.h","commctrl.h","richedit.h","winver.h","winnt.h","wingdi.h","verrsrc.h","afxribbonres.h"}

    def __init__(self, entry, include_dirs=(), defines=(), undefines=(), encoding=None):
        self.files=ProjectFiles(entry,include_dirs);self.encoding=encoding
        self.macros={name:Macro(lex("1")) for name in ("RC_INVOKED","_WIN32","WIN32")};self.once=set();self.stack=[]
        self.warnings=[];self.builtin_headers=[];self.conditions=[];self.used_ids={};self.encodings=[]
        for value in defines:
            name,separator,body=value.partition("=");self.define(name+" "+(body if separator else "1"),"<command line>",1)
        for value in undefines:self.macros.pop(value,None)

    def warn(self, code, location, message, *, severity="warning", resource_id=None, resource_type=None):
        path,line=location
        self.warnings.append(diagnostic(severity,code,path,line,resource_id,resource_type,message))

    def define(self, declaration, path, line, encoding="cp1252"):
        match=re.match(r"([^\W\d]\w*)(.*)$",declaration,re.S)
        if not match:raise ConversionError("invalid #define")
        name,tail=match.groups();parameters=None;variadic=False
        if tail.startswith("("):
            end=tail.find(")")
            if end<0:raise ConversionError("unterminated macro parameter list")
            parameters=[item.strip() for item in tail[1:end].split(",") if item.strip()]
            if parameters and parameters[-1]=="...":parameters[-1]="__VA_ARGS__";variadic=True
            if any(not IDENT.fullmatch(item) for item in parameters):raise ConversionError("unsupported macro parameter declaration")
            tail=tail[end+1:]
        self.macros[name]=Macro(lex(tail.strip(),path,line,encoding),parameters,variadic)

    def expand(self, tokens, depth=0):
        if depth>100:raise ConversionError("macro nesting exceeds 100")
        work=list(tokens);index=0;steps=0
        while index<len(work):
            token=work[index];macro=self.macros.get(token.text)
            if macro is None or token.text in token.hidden:index+=1;continue
            finish=index+1;arguments={}
            if macro.parameters is not None:
                while finish<len(work) and work[finish].kind=="newline":finish+=1
                if finish>=len(work) or work[finish].text!="(":index+=1;continue
                cursor=finish+1;level=1;groups=[[]]
                while cursor<len(work):
                    current=work[cursor]
                    if current.text=="(":level+=1
                    elif current.text==")":
                        level-=1
                        if not level:break
                    if current.text=="," and level==1:groups.append([])
                    elif current.kind!="newline":groups[-1].append(current)
                    cursor+=1
                if level:raise ConversionError(f"unterminated macro invocation {token.text}")
                if not macro.parameters and groups==[[]]:groups=[]
                minimum=len(macro.parameters)-int(macro.variadic)
                if len(groups)<minimum or not macro.variadic and len(groups)!=len(macro.parameters):raise ConversionError(f"wrong argument count for macro {token.text}")
                if macro.variadic:
                    extra=[]
                    for group in groups[minimum:]:
                        if extra:extra.append(replace(token,text=",",kind="symbol"))
                        extra.extend(group)
                    groups=groups[:minimum]+[extra]
                arguments=dict(zip(macro.parameters,groups));finish=cursor+1
            replacement=[];body=macro.body;position=0
            while position<len(body):
                current=body[position]
                if current.text=="#" and position+1<len(body) and body[position+1].text in arguments:
                    values=arguments[body[position+1].text]
                    joined="".join((" " if index and item.leading_space else "")+item.text for index,item in enumerate(values))
                    replacement.append(replace(token,text='"'+joined.replace("\\","\\\\").replace('"','\\"')+'"',kind="string"));position+=2;continue
                if current.text in arguments:
                    pasted=position and body[position-1].text=="##" or position+1<len(body) and body[position+1].text=="##"
                    replacement.extend(arguments[current.text] if pasted else self.expand(arguments[current.text],depth+1))
                else:replacement.append(current)
                position+=1
            position=0
            while position<len(replacement):
                if replacement[position].text!="##":position+=1;continue
                if position==0 or position+1==len(replacement):raise ConversionError(f"empty token paste in macro {token.text}")
                pasted=lex(replacement[position-1].text+replacement[position+1].text,token.path,token.line,token.encoding)
                if len(pasted)!=1:raise ConversionError(f"invalid token paste in macro {token.text}")
                replacement[position-1:position+2]=pasted;position=max(0,position-1)
            hidden=token.hidden|{token.text}
            replacement=[replace(item,path=token.path,line=token.line,encoding=token.encoding,hidden=item.hidden|hidden) for item in replacement]
            work[index:finish]=replacement
            steps+=1
            if steps>10000 or len(work)>200000:raise ConversionError("macro expansion exceeds its resource limit")
        return work

    def integer(self, tokens, undefined_zero=False):return Expression(self.expand(tokens),undefined_zero).evaluate()

    def condition(self, value, path, line, encoding="cp1252"):
        tokens=lex(value,path,line,encoding);result=[];index=0
        while index<len(tokens):
            token=tokens[index]
            if token.text!="defined":result.append(token);index+=1;continue
            index+=1;parenthesized=index<len(tokens) and tokens[index].text=="("
            if parenthesized:index+=1
            if index>=len(tokens):raise ConversionError("defined operator is missing its identifier")
            name=tokens[index].text;index+=1
            if parenthesized:
                if index>=len(tokens) or tokens[index].text!=")":raise ConversionError("invalid defined(...) expression")
                index+=1
            result.append(replace(token,text=str(int(name in self.macros)),kind="number"))
        chosen=bool(self.integer(result,True));self.conditions.append({"file":str(path),"line":line,"expression":value,"result":chosen})
        return chosen

    def builtin(self, name):
        from rc_defines import CONSTANTS, FUNCTION_MACROS
        for key,value in CONSTANTS.items():
            if key not in self.macros:self.macros[key]=Macro(lex(value,"<builtin "+name+">"))
        for declaration in FUNCTION_MACROS:
            if declaration.split("(",1)[0] not in self.macros:self.define(declaration,"<builtin "+name+">",1)
        if name not in self.builtin_headers:self.builtin_headers.append(name)

    def read(self, path, inherited_codec=None):
        path=Path(path).resolve()
        if path in self.once:return []
        if len(self.stack)>100:raise ConversionError("include nesting exceeds 100")
        # Header guards may intentionally break include cycles: only reject a
        # repeated resource script, not a guarded header before its #ifndef.
        if path in self.stack and path.suffix.lower() not in {".h",".hpp",".c"}:raise ConversionError(f"resource include cycle: {path}")
        self.stack.append(path);self.files.opened.add(str(path))
        try:
            raw=path.read_bytes()
            source_encoding=None
            if raw.startswith((codecs.BOM_UTF16_LE,codecs.BOM_UTF16_BE)):
                content,codec,origin=raw.decode("utf-16"),"cp1252","bom"
                source_encoding="utf-16"
            elif raw.startswith(codecs.BOM_UTF8):
                content,codec,origin=raw.decode("utf-8-sig"),"utf-8","bom"
                source_encoding="utf-8-sig"
            elif self.encoding:
                content=raw.decode(self.encoding)
                codec="cp1252" if codecs.lookup(self.encoding).name.startswith(("utf-16","utf-32")) else self.encoding
                origin="override"
                source_encoding=self.encoding
            else:
                content,codec,origin=raw,inherited_codec or "cp1252","inherited" if inherited_codec else "default"
                try:raw.decode("utf-8")
                except UnicodeDecodeError:
                    if not inherited_codec and not re.search(rb"#\s*pragma\s+code_page\s*\(",raw):
                        self.warn("RC_ENCODING",(path,1),"no BOM/code_page; interpreted legacy bytes as cp1252; override with --encoding")
                else:
                    if not inherited_codec and not raw.isascii():codec,origin="utf-8","detected_utf8"
            default_codec=codec if self.encoding else "cp1252"
            fixed_source=isinstance(content,str)
            self.encodings.append({"file":str(path),"line":1,"encoding":codec,"origin":origin,
                                   "source_encoding":source_encoding or codec})

            def logical_lines():
                # Decode before splicing: a DBCS trail byte 0x5c is not '\\'.
                # The generator resumes after each directive updates codec.
                separator="\n" if fixed_source else b"\n"
                physical_lines=content.split(separator)
                pending="";start=1;continuing=False;in_comment=False;comment_line=1
                for physical,value in enumerate(physical_lines,1):
                    if not fixed_source:value=value.decode(codec,errors="surrogateescape")
                    value=value.removesuffix("\r")
                    if not continuing:start=physical
                    if physical<len(physical_lines) and value.endswith("\\"):
                        pending+=value[:-1];continuing=True;continue
                    value=pending+value;pending="";continuing=False
                    value,open_comment=strip_comments(value,in_comment)
                    if open_comment and not in_comment:comment_line=start
                    in_comment=open_comment
                    yield start,value
                if in_comment:raise ConversionError(f"{path}:{comment_line}: unterminated block comment")

            output=[];buffer=[];conditions=[];active=True;header=path.suffix.lower() in {".h",".hpp",".c"}
            def flush():
                output.extend(self.expand(buffer));buffer.clear()
            for line,value in logical_lines():
                if active and re.search(r"[\udc80-\udcff]",value):
                    raise ConversionError(f"{path}:{line}: cannot decode source bytes using {codec}; check #pragma code_page or pass --encoding for a uniformly encoded file")
                directive=re.match(r"\s*#\s*(\w+)(.*)",value,re.S)
                if directive:
                    flush()
                    command,argument=directive[1].lower(),directive[2].strip()
                    if command in {"if","ifdef","ifndef"}:
                        chosen=(self.condition(argument,path,line,codec) if command=="if" else (argument in self.macros) != (command=="ifndef")) if active else False
                        if command!="if":self.conditions.append({"file":str(path),"line":line,"expression":command+" "+argument,"result":bool(active and chosen)})
                        conditions.append([active,chosen,False]);active=active and chosen;continue
                    if command in {"else","elif","endif"}:
                        if not conditions:raise ConversionError(f"{path}:{line}: unmatched #{command}")
                        parent,taken,saw_else=conditions[-1]
                        if command=="endif":conditions.pop();active=parent;continue
                        if saw_else:raise ConversionError(f"{path}:{line}: directive after #else")
                        chosen=not taken and parent and (self.condition(argument,path,line,codec) if command=="elif" else True)
                        conditions[-1]=[parent,taken or chosen,command=="else"];active=chosen;continue
                    if not active:continue
                    if command=="define":self.define(argument,path,line,codec)
                    elif command=="undef":self.macros.pop(argument,None)
                    elif command=="include":output.extend(self.include(argument,path,line,codec))
                    elif command=="pragma":
                        if argument=="once":self.once.add(path)
                        elif re.match(r"code_page\s*\(",argument):
                            match=re.fullmatch(r"code_page\s*\(\s*(\d+|DEFAULT)\s*\)",argument,re.I)
                            if match:
                                codec=default_codec if match[1].upper()=="DEFAULT" else "cp"+match[1]
                                codecs.lookup(codec)
                                self.encodings.append({"file":str(path),"line":line,"encoding":codec,"origin":"pragma",
                                                       "source_encoding":source_encoding or codec})
                            else:self.warn("PP_PRAGMA",(path,line),"cannot parse #pragma "+argument)
                        elif not argument.startswith(("warning","pack","message")):self.warn("PP_PRAGMA",(path,line),"unsupported #pragma "+argument)
                    elif command in {"line"}:pass
                    elif command=="error":raise ConversionError(f"{path}:{line}: #error {argument}")
                    elif command=="warning":self.warn("PP_WARNING",(path,line),argument)
                    else:self.warn("PP_DIRECTIVE",(path,line),"unsupported preprocessor directive #"+command)
                    continue
                if not active or header or not value.strip():continue
                rcinclude=re.match(r"\s*RCINCLUDE\s+(.+)",value,re.I)
                if rcinclude:flush();output.extend(self.include(rcinclude[1],path,line,codec));continue
                tokens=lex(value,path,line,codec)
                for candidate in {t.text for t in tokens if t.kind=="id" and t.text.startswith("ID")}:
                    macro=self.macros.get(candidate)
                    if macro is not None and macro.parameters is None:
                        try:
                            identifier=self.integer(lex(candidate,path,line,codec))
                            self.used_ids.setdefault(identifier,set()).add(candidate)
                        except (ConversionError,ValueError,UnicodeError):pass
                buffer.extend(tokens);buffer.append(Token("\n",str(path),line,"newline",codec))
            flush()
            if conditions:raise ConversionError(f"{path}: missing #endif")
            return output
        except (ConversionError,UnicodeError,LookupError) as exc:
            if str(path) in str(exc):raise
            raise ConversionError(f"{path}:{locals().get('line',1)}: {exc}") from exc
        finally:self.stack.pop()

    def include(self, value, path, line, encoding="cp1252"):
        expanded=self.expand(lex(value,path,line,encoding));spelling="".join(t.text for t in expanded)
        if spelling.startswith('"'):
            name=rc_text(expanded)
        elif spelling.startswith("<") and spelling.endswith(">"):
            name=spelling[1:-1]
        else:name=spelling
        basename=Path(name.replace("\\","/")).name.lower()
        try:resolved=self.files.find(name,path)
        except ConversionError as exc:
            if basename in self.BUILTIN_HEADERS and str(exc).startswith("file not found in project:"):
                self.builtin(basename);return []
            self.warn("PP_INCLUDE",(path,line),str(exc),severity="error",resource_id=name,resource_type="INCLUDE");return []
        return self.read(resolved,encoding)

    def symbols(self):
        result={value:sorted(names) for value,names in self.used_ids.items()}
        for name,macro in self.macros.items():
            if macro.parameters is not None or not name.startswith("ID"):continue
            try:value=self.integer(lex(name))
            except (ConversionError,ValueError,UnicodeError):continue
            if name not in result.setdefault(value,[]):result[value].append(name)
        return result
