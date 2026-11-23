"""Read RC projects directly in Python; normalize resource payloads in memory.

No .res file is created or opened, and no compiler/SDK is consulted. The small
in-memory payload encoders are adapters to the existing Qt exporters, not an
external resource-compilation stage. Unknown resources retain source text and
produce diagnostics instead of being treated as successfully converted.
"""

from dataclasses import replace
import struct

from RcToUI import ConversionError
from mfc_rc import Resource
from rc_preprocessor import Preprocessor, Expression, lex, rc_text


MEMORY = {"DISCARDABLE","PURE","IMPURE","MOVEABLE","MOVABLE","FIXED","PRELOAD","LOADONCALL"}
RESOURCE_TYPES = {"CURSOR":12,"BITMAP":2,"ICON":14,"MENU":4,"MENUEX":4,"DIALOG":5,"DIALOGEX":5,
                  "STRINGTABLE":6,"FONTDIR":7,"FONT":8,"ACCELERATORS":9,"RCDATA":10,"MESSAGETABLE":11,
                  "VERSIONINFO":16,"DLGINCLUDE":17,"HTML":23,"MANIFEST":24,"RT_RIBBON_XML":28,
                  "RIBBON_XML":28,"DLGINIT":240,"TOOLBAR":241,"DESIGNINFO":"DESIGNINFO"}
CONTROL_DEFAULTS = {
    "PUSHBUTTON":("Button",0x10000),"DEFPUSHBUTTON":("Button",0x10001),
    "CHECKBOX":("Button",0x10002),"AUTOCHECKBOX":("Button",0x10003),
    "RADIOBUTTON":("Button",4),"AUTORADIOBUTTON":("Button",9),
    "STATE3":("Button",0x10005),"AUTO3STATE":("Button",0x10006),
    "GROUPBOX":("Button",7),"PUSHBOX":("Button",0x1000A),"USERBUTTON":("Button",0x10008),
    "LTEXT":("Static",0x20000),"CTEXT":("Static",0x20001),"RTEXT":("Static",0x20002),
    "ICON":("Static",3),"EDITTEXT":("Edit",0x810000),
    "LISTBOX":("ListBox",0x800001),"COMBOBOX":("ComboBox",0),"SCROLLBAR":("ScrollBar",0),
    "BEDIT":("BEDIT",0x810000),"HEDIT":("HEDIT",0x810000),"IEDIT":("IEDIT",0x810000),
}
TEXT_CONTROLS = {"PUSHBUTTON","DEFPUSHBUTTON","CHECKBOX","AUTOCHECKBOX","RADIOBUTTON","AUTORADIOBUTTON",
                 "STATE3","AUTO3STATE","GROUPBOX","PUSHBOX","USERBUTTON","LTEXT","CTEXT","RTEXT","ICON"}
MENU_FLAGS = {"GRAYED":1,"INACTIVE":2,"DISABLED":2,"CHECKED":8,"UNCHECKED":0,"ENABLED":0,
              "MENUBARBREAK":0x20,"MENUBREAK":0x40,"OWNERDRAW":0x100,"HELP":0x4000,"RIGHTJUSTIFY":0x4000}
ACCEL_FLAGS = {"VIRTKEY":1,"ASCII":0,"NOINVERT":2,"SHIFT":4,"CONTROL":8,"ALT":16}


def packed(fmt,*values):
    try:return struct.pack("<"+fmt,*values)
    except (struct.error,OverflowError) as exc:raise ConversionError(f"resource integer is out of range: {exc}") from exc


def zstring(value):
    if isinstance(value,int):return packed("HH",0xffff,value&0xffff)
    return str(value).encode("utf-16-le")+b"\0\0"


def align(data):return data+b"\0"*((-len(data))%4)


def fields(tokens):
    result=[[]];depth=0
    for token in tokens:
        if token.text=="(":depth+=1
        elif token.text==")":depth-=1
        if depth<0:raise ConversionError("unbalanced expression parentheses")
        if token.text=="," and depth==0:result.append([])
        else:result[-1].append(token)
    if depth:raise ConversionError("unbalanced expression parentheses")
    return result


class Parser:
    def __init__(self, path, include_dirs=(), defines=(), undefines=(), encoding=None):
        self.pp=Preprocessor(path,include_dirs,defines,undefines,encoding)
        self.tokens=self.pp.read(path);self.pos=0;self.language=0x409
        self.resources=[];self.origins=[];self.unparsed=[];self.string_blocks={}
        self.icon_id=1;self.context={};self.failure_token=None

    def peek(self, offset=0):return self.tokens[self.pos+offset].text if self.pos+offset<len(self.tokens) else ""
    def skip_newlines(self):
        while self.peek()=="\n":self.pos+=1
    def take(self):
        if self.pos>=len(self.tokens):raise ConversionError("unexpected end of resource script")
        token=self.tokens[self.pos];self.pos+=1;return token
    def expect(self, value):
        self.skip_newlines();token=self.take()
        if token.text.upper()!=value:raise ConversionError(f"expected {value}, found {token.text!r}")
    def begin(self):
        self.skip_newlines()
        if self.peek().upper() not in {"BEGIN","{"}:raise ConversionError("expected BEGIN/{")
        self.pos+=1
    def ended(self):
        self.skip_newlines()
        if self.peek().upper() in {"END","}"}:self.pos+=1;return True
        return False

    def statement(self):
        self.skip_newlines();result=[];depth=0
        if self.pos<len(self.tokens):self.failure_token=self.tokens[self.pos]
        while self.pos<len(self.tokens):
            token=self.tokens[self.pos]
            if token.text.upper() in {"BEGIN","END","{","}"} and depth==0:break
            if token.kind=="newline":
                following=self.tokens[self.pos+1] if self.pos+1<len(self.tokens) else None
                if depth or result and result[-1].text in {",","|","&","+","-","*","/","<<",">>"} or following and following.kind=="string" and result and result[-1].kind=="string":
                    self.pos+=1;continue
                self.pos+=1;break
            self.pos+=1;result.append(token)
            depth+=int(token.text=="(")-int(token.text==")")
        return result

    @staticmethod
    def integer(tokens, default=None, keywords=None):
        if not tokens:
            if default is not None:return default
            raise ConversionError("missing integer value")
        if keywords:tokens=[replace(t,text=str(keywords[t.text.upper()]),kind="number") if t.text.upper() in keywords else t for t in tokens]
        return Expression(tokens).evaluate()

    def identifier(self, tokens):
        if len(tokens)==1 and tokens[0].kind in {"string","id"}:
            return rc_text(tokens) if tokens[0].kind=="string" else tokens[0].text
        return self.integer(tokens)

    def style(self, tokens, default=0):
        if not tokens:return default
        groups=[];current=[];depth=0
        for token in tokens:
            depth+=int(token.text=="(")-int(token.text==")")
            if token.text=="|" and not depth:groups.append(current);current=[]
            else:current.append(token)
        groups.append(current);positive=0;negative=0
        for group in groups:
            if group and group[0].text.upper()=="NOT":negative|=self.integer(group[1:])
            else:positive|=self.integer(group)
        return ((default|positive)&~negative)&0xffffffff

    def location(self, token):return (token.path,token.line)
    def warn(self, code, token, message, *, severity="warning", resource_id=None, resource_type=None):
        self.pp.warn(code,self.location(token),message,severity=severity,
                     resource_id=self.context.get("name") if resource_id is None else resource_id,
                     resource_type=self.context.get("type") if resource_type is None else resource_type)
        self.pp.warnings[-1]["resource_line"]=token.line if resource_type=="STRINGTABLE_ENTRY" else self.context.get("line",token.line)

    def add(self, kind, name, language, data, token, source_format="normalized"):
        self.failure_token=token
        if any(r.type==kind and r.name==name and r.language==language for r in self.resources):
            raise ConversionError(f"duplicate resource type={kind!r}, ID={name!r}, language={language:#x}")
        self.resources.append(Resource(kind,name,language,data))
        self.origins.append({"type":kind,"name":name,"language":language,"file":token.path,"line":token.line,"format":source_format})

    def optional(self, language, initial=()):
        options={"language":language,"_locations":{}}
        while True:
            self.skip_newlines()
            if not initial and self.peek().upper() in {"BEGIN","{"}:break
            if initial:line=list(initial);initial=()
            else:line=self.statement()
            if not line:raise ConversionError("resource header is missing BEGIN")
            key=line[0].text.upper();value=line[1:]
            options["_locations"][key]=line[0]
            if value and value[0].text=="=":value=value[1:]
            if key=="LANGUAGE":options["language"]=self.lang(value)
            elif key in {"CAPTION","FONT","STYLE","EXSTYLE","CLASS","MENU","VERSION","CHARACTERISTICS"}:options[key]=value
            elif key not in MEMORY:raise ConversionError(f"unsupported resource header statement {key!r}")
        return options

    def lang(self, tokens):
        values=fields(tokens)
        if len(values)!=2:raise ConversionError("LANGUAGE requires language,sublanguage")
        primary,sub=self.integer(values[0]),self.integer(values[1])
        if not 0<=primary<=0x3ff or not 0<=sub<=0x3f:raise ConversionError("LANGUAGE values out of range")
        return primary|(sub<<10)

    def dialog(self, name, extended, header, token):
        values=fields(header)
        if not 4<=len(values)<=(5 if extended else 4):raise ConversionError("dialog requires x,y,width,height and optional DIALOGEX help ID")
        rect=[self.integer(value) for value in values[:4]]
        if min(rect[2:])<0:raise ConversionError("dialog width/height cannot be negative")
        options=self.optional(self.language);self.begin()
        style=self.style(options.get("STYLE",[]),0x80880000 if "STYLE" not in options else 0)
        exstyle=self.style(options.get("EXSTYLE",[]))
        title=rc_text(options.get("CAPTION",[]))
        if "CAPTION" in options:style|=0xc00000
        font=None
        if "FONT" in options:
            self.failure_token=options["_locations"]["FONT"]
            pieces=fields(options["FONT"])
            if not 2<=len(pieces)<=(5 if extended else 2) or any(not piece for piece in pieces):
                raise ConversionError("invalid FONT declaration; expected point size, quoted face name and optional DIALOGEX weight/italic/charset")
            font=(self.integer(pieces[0]),rc_text(pieces[1]),self.integer(pieces[2],400) if len(pieces)>2 else 400,self.integer(pieces[3],0) if len(pieces)>3 else 0,self.integer(pieces[4],1) if len(pieces)>4 else 1)
            if not 1<=font[0]<=32767 or not font[1] or any(ord(c)<32 for c in font[1]) or not 0<=font[2]<=1000 or font[3] not in {0,1} or not 0<=font[4]<=255:
                raise ConversionError("invalid FONT values: point size 1..32767, nonempty face, weight 0..1000, italic 0/1, charset 0..255 required")
            style|=0x40
        elif style&0x40:
            raise ConversionError("DS_SETFONT/DS_SHELLFONT requires a FONT declaration")
        menu=self.identifier(options["MENU"]) if "MENU" in options else ""
        native=self.identifier(options["CLASS"]) if "CLASS" in options else ""
        help_id=self.integer(values[4],0) if len(values)>4 else 0
        items=[]
        while not self.ended():
            control=self.statement()
            if not control:raise ConversionError("empty or malformed dialog control")
            items.append(self.control(control,extended))
        if extended:payload=packed("HHIIIHhhhh",1,0xffff,help_id&0xffffffff,exstyle,style,len(items),*rect)
        else:payload=packed("IIHhhhh",style,exstyle,len(items),*rect)
        payload+=zstring(menu)+zstring(native)+zstring(title)
        if font:
            payload+=packed("H",font[0])
            if extended:payload+=packed("HBB",*font[2:])
            payload+=zstring(font[1])
        for item in items:payload=align(payload)+item
        self.add(5,name,options["language"],payload,token)

    def control(self, tokens, extended):
        kind=tokens[0].text.upper();values=fields(tokens[1:]);title=""
        if kind=="CONTROL":
            if len(values)<8:raise ConversionError("CONTROL requires text,id,class,style,x,y,width,height")
            title=self.identifier(values[0]) if not all(t.kind=="string" for t in values[0]) else rc_text(values[0])
            identifier=self.integer(values[1]);native=self.identifier(values[2]);style=self.style(values[3],0x50000000)
            geometry=values[4:8];extra=values[8:]
        else:
            if kind not in CONTROL_DEFAULTS:raise ConversionError(f"unsupported dialog control/macro {kind!r}")
            native,default=CONTROL_DEFAULTS[kind]
            if kind in TEXT_CONTROLS:
                first=values.pop(0)
                title=self.identifier(first) if kind=="ICON" and not all(t.kind=="string" for t in first) else rc_text(first)
            if kind=="ICON" and len(values)==3:
                self.warn("RC_ICON_SIZE",tokens[0],"ICON omits dimensions; using 20x20 DLU. Specify width/height to preserve exact geometry.")
                values.extend([lex("20"),lex("20")])
            if len(values)<5:raise ConversionError(f"{kind} requires id,x,y,width,height")
            identifier=self.integer(values[0]);geometry=values[1:5]
            style=self.style(values[5] if len(values)>5 else [],0x50000000|default)
            extra=values[6:]
        rect=[self.integer(value) for value in geometry]
        if min(rect[2:])<0:raise ConversionError("control width/height cannot be negative")
        exstyle=self.style(extra[0]) if extra else 0
        help_id=self.integer(extra[1],0) if len(extra)>1 else 0
        self.skip_newlines();creation=b""
        if self.peek().upper() in {"BEGIN","{"}:creation=self.raw_data()
        if extended:payload=packed("IIIhhhhI",help_id&0xffffffff,exstyle,style,*rect,identifier&0xffffffff)
        else:payload=packed("IIhhhhH",style,exstyle,*rect,identifier&0xffff)
        payload+=zstring(native)+zstring(title)
        payload+=packed("H",len(creation)+(2 if creation and not extended else 0))+creation
        return payload

    def menu(self, name, extended, token, header=()):
        options=self.optional(self.language,header)
        def level():
            self.begin();items=[]
            while not self.ended():
                line=self.statement()
                if not line:raise ConversionError("malformed menu item")
                kind=line[0].text.upper();parts=fields(line[1:])
                if kind not in {"POPUP","MENUITEM"}:raise ConversionError(f"unsupported menu statement {kind!r}")
                popup=kind=="POPUP"
                if not popup and len(parts)==1 and len(parts[0])==1 and parts[0][0].text.upper()=="SEPARATOR":
                    items.append((False,"",0,0x800,0,0,[]));continue
                label=rc_text(parts[0]);command=0;item_type=0;state=0;help_id=0
                if extended:
                    command=self.integer(parts[1],0) if len(parts)>1 else 0
                    item_type=self.integer(parts[2],0) if len(parts)>2 else 0
                    state=self.integer(parts[3],0) if len(parts)>3 else 0
                    help_id=self.integer(parts[4],0) if len(parts)>4 else 0
                else:
                    start=1 if popup else 2
                    if not popup:command=self.integer(parts[1]) if len(parts)>1 else 0
                    flags=0
                    for part in parts[start:]:flags|=self.integer(part,0,MENU_FLAGS)
                    state=flags&(3|8|0x1000);item_type=flags&~(3|8|0x1000)
                children=level() if popup else []
                if popup and not children:
                    self.warn("RC_EMPTY_MENU",line[0],"empty popup retained with a separator placeholder")
                    children=[(False,"",0,0x800,0,0,[])]
                items.append((popup,label,command,item_type,state,help_id,children))
            return items
        items=level()
        payload=packed("HHI",1,4,0) if extended else packed("HH",0,0)
        def encode(items,data):
            for index,(popup,label,command,item_type,state,help_id,children) in enumerate(items):
                end=0x80 if index==len(items)-1 else 0
                if extended:
                    data=align(data)+packed("IIIH",item_type,state,command,(1 if popup else 0)|end)+zstring(label)
                    if popup:data=align(data)+packed("I",help_id)
                else:
                    data+=packed("H",item_type|state|(0x10 if popup else 0)|end)
                    if not popup:data+=packed("H",command)
                    data+=zstring(label)
                if popup:data=encode(children,data)
            return data
        self.add(4,name,options["language"],encode(items,payload),token)

    def toolbar(self,name,header,token):
        dimensions=fields(header)
        if len(dimensions)!=2:raise ConversionError("TOOLBAR requires width,height")
        width,height=[self.integer(value) for value in dimensions]
        options=self.optional(self.language);self.begin();items=[]
        while not self.ended():
            line=self.statement()
            if not line:raise ConversionError("malformed toolbar item")
            if line[0].text.upper()=="SEPARATOR":items.append(0)
            elif line[0].text.upper()=="BUTTON":items.append(self.integer(line[1:]))
            else:raise ConversionError("TOOLBAR expects BUTTON or SEPARATOR")
        self.add(241,name,options["language"],packed("HHHH",1,width,height,len(items))+packed("H"*len(items),*items),token)

    def accelerators(self,name,token,header=()):
        options=self.optional(self.language,header);self.begin();items=[]
        while not self.ended():
            line=self.statement();parts=fields(line)
            if len(parts)<2:raise ConversionError("accelerator requires key,command")
            if parts[0] and parts[0][0].kind=="string":
                key_text=rc_text(parts[0])
                if len(key_text)==2 and key_text[0]=="^":key=ord(key_text[1].upper())-64
                elif len(key_text)==1:key=ord(key_text)
                else:raise ConversionError("accelerator character must have length one (or ^X)")
            else:key=self.integer(parts[0])
            flags=0
            for part in parts[2:]:flags|=self.integer(part,0,ACCEL_FLAGS)
            items.append((flags,key,self.integer(parts[1])))
        payload=b"".join(packed("HHHH",flags|(0x80 if index==len(items)-1 else 0),key,command,0) for index,(flags,key,command) in enumerate(items))
        self.add(9,name,options["language"],payload,token)

    def strings(self,token,header=()):
        options=self.optional(self.language,header);self.begin()
        while not self.ended():
            line=self.statement()
            if not line:raise ConversionError("STRINGTABLE is missing END or contains an unexpected delimiter")
            identifier=line[0].text if line[0].kind!="string" else "<missing string ID>"
            try:
                if line[0].kind=="number":identifier=self.integer(line[:1])
                index=next((i for i,t in enumerate(line) if t.kind=="string"),None)
                if index is None:raise ConversionError("STRINGTABLE entry has no string")
                prefix=line[:index]
                if prefix and prefix[-1].text==",":prefix=prefix[:-1]
                identifier=self.integer(prefix)
                if not 0<=identifier<=0xffff:raise ConversionError("STRINGTABLE ID exceeds 16 bits")
                value=rc_text(line[index:]);key=(identifier//16+1,options["language"])
                block=self.string_blocks.setdefault(key,{})
                if identifier%16 in block:raise ConversionError(f"duplicate string ID {identifier}")
                block[identifier%16]=(value,token)
            except (ConversionError,ValueError,UnicodeError) as exc:
                self.warn("RC_STRING_ENTRY",line[0] if line else token,str(exc),severity="error",resource_id=identifier,resource_type="STRINGTABLE_ENTRY")
                self.unparsed.append({"type":"STRINGTABLE_ENTRY","file":token.path,"line":line[0].line if line else token.line,"source":" ".join(t.text for t in line)})

    def raw_data(self):
        self.begin();data=bytearray()
        while not self.ended():
            line=self.statement()
            if not line:raise ConversionError("malformed inline resource data")
            for value in fields(line):
                if not value:continue
                if all(t.kind=="string" for t in value):
                    for string in value:
                        text=rc_text([string])
                        data.extend(text.encode("utf-16-le") if string.text.startswith("L") else text.encode(string.encoding))
                else:
                    number=self.integer(value)
                    long=any(t.kind=="number" and "l" in t.text.lower() for t in value)
                    data.extend(packed("I" if long else "H",number&(0xffffffff if long else 0xffff)))
        return bytes(data)

    def asset(self,kind,name,header,token):
        path_tokens=[t for t in header if t.text.upper() not in MEMORY]
        if not path_tokens or not all(t.kind=="string" for t in path_tokens):raise ConversionError("file resource requires a quoted filename")
        requested=rc_text(path_tokens);path=self.pp.files.find(requested,token.path)
        self.pp.files.opened.add(str(path));data=path.read_bytes()
        if kind==2:
            if len(data)<14 or data[:2]!=b"BM":raise ConversionError(f"invalid BMP file {path}")
            size,reserved1,reserved2,offset=struct.unpack_from("<IHHI",data,2)
            if size!=len(data) or reserved1 or reserved2 or not 54<=offset<=len(data):
                raise ConversionError(f"invalid BMP file size/reserved fields/pixel offset in {path}")
            from rc_commands import decode_dib
            header_size=struct.unpack_from("<I",data,14)[0]
            if header_size not in {40,108,124} or len(data)<14+header_size:
                raise ConversionError(f"invalid/unsupported BMP DIB header in {path}")
            bits=struct.unpack_from("<H",data,28)[0]
            compression=struct.unpack_from("<I",data,30)[0]
            colors=struct.unpack_from("<I",data,46)[0]
            masks=(16 if compression==6 else 12) if header_size==40 and compression in {3,6} else 0
            expected=14+header_size+masks+4*(colors or (1<<bits if bits<=8 else 0))
            if offset<expected:raise ConversionError(f"BMP pixel offset overlaps its header/palette in {path}")
            data=data[14:expected]+data[offset:]  # Normalize a legal gap before pixels.
            decode_dib(data)  # Validate even an image not used by a control.
        elif kind==14:
            if len(data)<6:raise ConversionError("truncated ICO header")
            reserved,icon_type,count=struct.unpack_from("<HHH",data)
            if reserved or icon_type!=1 or len(data)<6+count*16:raise ConversionError("invalid ICO directory")
            group=packed("HHH",0,1,count)
            for index in range(count):
                w,h,colors,zero,planes,bits,size,offset=struct.unpack_from("<BBBBHHII",data,6+16*index)
                if offset+size>len(data):raise ConversionError("ICO image is truncated")
                identifier=self.icon_id;self.icon_id+=1
                self.add(3,identifier,self.language,data[offset:offset+size],token,"file")
                group+=packed("BBBBHHIH",w,h,colors,zero,planes,bits,size,identifier)
            data=group
        from mfc_ribbon import is_ribbon, xml_root
        if is_ribbon(Resource(kind,name,self.language,data)):xml_root(data)
        self.add(kind,name,self.language,data,token,"file")

    def recover(self,start,block):
        cursor=start;depth=0;found=False
        while cursor<len(self.tokens):
            current=self.tokens[cursor]
            if current.text.upper() in {"BEGIN","{"}:depth+=1;found=True
            elif current.text.upper() in {"END","}"}:
                depth-=1
                if found and depth==0:self.pos=cursor+1;return
            if not block and current.kind=="newline":self.pos=cursor+1;return
            cursor+=1
        self.pos=max(self.pos,start+1)

    def parse(self):
        while self.pos<len(self.tokens):
            self.skip_newlines()
            if not self.peek():break
            start=self.pos;token=self.tokens[start];block=False
            self.context={};self.failure_token=token
            resource_start=len(self.resources);origin_start=len(self.origins);icon_start=self.icon_id
            try:
                line=self.statement()
                if not line:raise ConversionError("unexpected resource delimiter")
                if line[0].text.upper()=="LANGUAGE":self.language=self.lang(line[1:]);continue
                if line[0].text.upper() in {"CHARACTERISTICS","VERSION"}:continue
                if line[0].text.upper()=="STRINGTABLE":
                    self.context={"name":"STRINGTABLE","type":6,"line":token.line}
                    block=True;self.strings(token,[t for t in line[1:] if t.text.upper() not in MEMORY]);continue
                if len(line)<2:raise ConversionError("resource declaration requires an ID and type")
                type_index=next((i for i,t in enumerate(line[1:],1) if t.text.upper() in RESOURCE_TYPES),1)
                name=self.identifier(line[:type_index])
                kind_text=line[type_index].text.upper()
                kind=RESOURCE_TYPES.get(kind_text)
                if kind is None:kind=self.identifier([line[type_index]])
                self.context={"name":name,"type":kind,"line":token.line}
                header=[t for t in line[type_index+1:] if t.text.upper() not in MEMORY]
                self.skip_newlines()
                if not header and kind not in {4,5,6,9,16,241,"DESIGNINFO"} and self.pos<len(self.tokens) and self.tokens[self.pos].kind=="string":
                    header=self.statement()
                block=(kind in {4,5,6,9,10,16,240,241,"DESIGNINFO"} and not (header and all(t.kind=="string" for t in header))) or self.peek().upper() in {"BEGIN","{"}
                if kind==5:self.dialog(name,kind_text=="DIALOGEX",header,token)
                elif kind==4:self.menu(name,kind_text=="MENUEX",token,header)
                elif kind==241:self.toolbar(name,header,token)
                elif kind==9:self.accelerators(name,token,header)
                elif header and all(t.kind=="string" for t in header):self.asset(kind,name,header,token)
                elif kind in {10,240}:
                    options=self.optional(self.language,header);data=self.raw_data();self.add(kind,name,options["language"],data,token)
                else:
                    self.recover(start,block)
                    raw=" ".join(t.text for t in self.tokens[start:self.pos])
                    self.unparsed.append({"id":name,"type":kind,"file":token.path,"line":token.line,"source":raw})
                    self.warn("RC_UNSUPPORTED_RESOURCE",token,f"resource {name!r}, type {kind!r} retained as RC source; no Qt conversion is implemented")
            except (ConversionError,ValueError,UnicodeError,struct.error,OSError) as exc:
                del self.resources[resource_start:];del self.origins[origin_start:];self.icon_id=icon_start
                self.warn("RC_PARSE",self.failure_token or token,str(exc),severity="error");self.recover(start,block)
                self.unparsed.append({"file":token.path,"line":token.line,"error":str(exc),"source":" ".join(t.text for t in self.tokens[start:self.pos])})
        for (name,language),values in sorted(self.string_blocks.items()):
            payload=b""
            for index in range(16):
                value=values.get(index,("",None))[0].encode("utf-16-le")
                payload+=packed("H",len(value)//2)+value
            self.add(6,name,language,payload,next(iter(values.values()))[1])
        return self.resources


def parse_project(path, include_dirs=(), defines=(), undefines=(), encoding=None):
    parser=Parser(path,include_dirs,defines,undefines,encoding)
    resources=parser.parse()
    metadata={"project_root":str(parser.pp.files.root),"dependencies":sorted(parser.pp.files.opened),
              "builtin_headers":parser.pp.builtin_headers,"resolved_paths":parser.pp.files.discovered,
              "conditions":parser.pp.conditions,"encodings":parser.pp.encodings,
              "resource_origins":parser.origins,"unparsed_resources":parser.unparsed}
    metadata["errors"]=[item for item in parser.pp.warnings if item["severity"]=="error"]
    return resources,parser.pp.symbols(),parser.pp.warnings,metadata
