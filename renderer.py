import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from difflib import unified_diff
from arango import ArangoClient
from operator import itemgetter
from abc import ABC, abstractmethod
from pathlib import PurePath
from .utils import Join, Buffered, MatchedFilter, Matcher
from .markdown import Document, Text, BlockquoteTag, CodeBlock, Details, Wrapper

def unpackRange(range):
    return itemgetter(
        "startLineNumber",
        "startColumn",
        "endLineNumber",
        "endColumn"
    )(range)

def fmtDuration(durationMs):
    durationS = durationMs/1000
    nMin, nSec = (int(durationS/60), durationS%60)
    output = ""
    if nMin > 0: output += f"{nMin} min, "
    output += f"{nSec:.3f} s"
    return output

class DB:
    @staticmethod
    def getDocument(coll, key):
        coll = ArangoClient("http://localhost:8529").db().collection(coll)
        return coll.get(key)

class Path:
    root = PurePath("/project/agkuhr/users/pobi/b2")
    roots = [
        root,
        root / "basf2",
        root / "basf2-v1",
        root / "basf2-v2",
    ]

    @staticmethod
    def splitRoot(path:PurePath):
        result = None
        for root in Path.roots:
            try: relpath = path.relative_to(root)
            except ValueError: continue
            result = relpath
        return result or path
    
    @staticmethod
    def format(pathStr:str):
        return str(Path.splitRoot(PurePath(pathStr)))

    @staticmethod
    def resolve(pathStr):
        return pathStr

class File:
    def __init__(self, buffer = None):
        self.buffer = buffer or []

    @classmethod
    def copy(cls, original):
        return cls(original.buffer.copy())
    
    def insert(self, start, end, strings):
        buffer = self.buffer
        (startLineNumber, startColumn) = start
        (endLineNumber, endColumn) = end
        
        if (nLines:=(endLineNumber - len(buffer))) > 0: buffer.extend([""]*nLines)
        
        for i, string in zip(
            range(startLineNumber, endLineNumber + 1),
            strings
        ):
            line = buffer[i-1]
            if startLineNumber == endLineNumber:
                buffer[i-1] = line[:startColumn-1] + string + line[endColumn-1:]
            elif i == startLineNumber: 
                buffer[i-1] = line[:startColumn-1] + string
            elif i == endLineNumber:
                buffer[i-1] = string + line[endColumn-1:]
            else: 
                buffer[i-1] = string

    def applyEdits(self, edits):
        for edit in (
            obj 
            for lst in edits
                for obj in lst
        ):
            text = edit["text"]
            (startLineNumber, startColumn, endLineNumber, endColumn) = unpackRange(edit["range"])
            
            if startLineNumber == endLineNumber: 
                text = text.lstrip("\n")
            
            lineEdits = text.split('\n')
            
            if (nChars := len(lineEdits[-1])) > endColumn - startColumn:
                endColumn = startColumn + nChars
            else:
                lineEdits = text.split('\n')
            
            endLineNumber = startLineNumber + len(lineEdits) - 1

            self.insert(
                start=(startLineNumber, startColumn), 
                end=(endLineNumber, endColumn), 
                strings=lineEdits
            )

class Node(ABC):
    @abstractmethod
    def build(self):
        pass

class Container(Node):
    def __init__(self, *content, content_it=None):
        self.content = content or content_it
    
    def buildContent(self):
        return map(
            lambda item: item.build(),
            self.content
        )
    
    def build(self):
        return Wrapper(content_it=self.buildContent())
        
class Chat(Container): 
    instance = None   

    def __init__(self, doc, header=None):
        Chat.instance = self
        self.header = header
        self.requesterUsername = doc["requesterUsername"]
        self.responderUsername = doc["responderUsername"]
        self.files = defaultdict(list)
        self.requestedFiles = set()

        super().__init__(content_it=[Request(req) for req in doc["requests"]])

        for path in self.requestedFiles:
            try:
                with open(Path.resolve(path), "r") as f:
                   content = f.read()
                   self.files[path].insert(0, File(buffer=content.split('\n'))) 
            except FileNotFoundError:
                print("Could not find requested file " + path)
                continue
    
    @classmethod
    def fromKey(cls, key):
        return cls(
            doc = DB.getDocument("chat-logs", key), 
            header = Text(Text.Text("Document ID: "), Text.Code(f"chat-logs/{key}"))
        )

    def build(self):
        return Document(
            self.header,
            Wrapper(content_it=self.buildContent())
        )

class Request(Container):
    @staticmethod
    def getModel(responseId):
        try:
            coll = ArangoClient("http://localhost:8529").db().collection("chat-ids")
            cursor = coll.find({"request_id": responseId}, skip=0, limit=1)
            if cursor.empty(): return None
            else: return cursor.next()["model"]
        except:
            return None
    
    def __init__(self, request):
        
        result = request["result"]
        responseId = result["metadata"]["responseId"]
        self.model = Request.getModel(responseId)
        self.timeMs = result["timings"]["totalElapsed"]
        self.message = request["message"]["text"]
        self.response = Response(request["response"])
        
        super().__init__(self.response)
    
    def build(self):
        return Wrapper(
            BlockquoteTag(
                Text(
                    Text.Heading(4, Chat.instance.requesterUsername + ':'),
                    Text.Text(self.message)
                )
            ),
            BlockquoteTag(
                Text(Text.Heading(
                        level = 4,
                        content = Chat.instance.responderUsername + (f" ({self.model}):" if self.model else ":")
                )),
                Wrapper(content_it=self.buildContent()),
                Text(Text.Code(f"({fmtDuration(self.timeMs)})"))
            )
        )
    
class Response(Container):
    @staticmethod
    def processChunks(lst):
        it = Buffered(lst)
        for chunk in it:
            obj = None
            
            kind = chunk.get("kind", None)
            
            #text block
            if (
                ("value" in chunk and kind is None) or
                kind == "inlineReference"
            ):
                it.enqueue(chunk)
                obj = ResponseText(it)
            elif kind is not None:
                match kind:
                    case "prepareToolInvocation": continue
                    case "confirmation":
                        obj = Confirmation(chunk)      
                    case "toolInvocationSerialized":
                        toolId = chunk["toolId"]
                        match toolId:
                            case "copilot_insertEdit":
                                it.enqueue(chunk)
                                obj = ToolInsertEdit(it)
                            case "copilot_replaceString":
                                it.enqueue(chunk)
                                obj = ToolReplaceString(it)
                            case "copilot_readFile":
                                obj = ToolReadFile(chunk)
                            case "copilot_findTextInFiles":
                                obj = ToolFindTextInFiles(chunk)
                            case "copilot_findFiles":
                                obj = ToolFindFiles(chunk)
                            case "copilot_getErrors":
                                obj = ToolGetErrors(chunk)
            
            if obj:
                yield obj
            else:
                print(f"Unknown chunk encountered:\n{chunk}")
   
    def __init__(self, lst):
        super().__init__(*self.processChunks(lst))

class Confirmation(Node):
    def __init__(self, doc):
        self.message = doc["message"]

    def build(self):
        return BlockquoteTag(Text(
            Text.Text(self.message)
        ))

class ResponseText(Container):
    class Text(Node):
        def __init__(self, doc):
            self.text = doc["value"]
        def build(self):
            return Text.Text(self.text)
        
    class InlineReference(Node):
        def __init__(self, doc):
            self.ref = doc["inlineReference"]
            self.kind = None
            self.text = ""
            #file reference
            if "path" in self.ref:
                self.kind = "path"
                self.text = self.ref["path"]
            #symbol reference
            elif "name" in self.ref:
                self.text = self.ref["name"]
            
        def build(self):
            if self.kind == "path":
                return Text.Code(Path.format(self.text))
            else:
                return Text.Code(self.text)

    def __init__(self, it):
        chunks = []
        for chunk in it:
            kind = chunk.get("kind", None)
            if kind == "inlineReference":
                chunks.append(ResponseText.InlineReference(chunk))
            elif "value" in chunk and kind is None:
                chunks.append(ResponseText.Text(chunk))
            else:
                it.enqueue(chunk)
                break
        super().__init__(content_it=chunks)

    @staticmethod    
    def fixInlineRefs(it):
        it = iter(it)
        def fix(buffer):
            [obj, ref, obj1] = buffer
            
            if not isinstance(obj, ResponseText.Text): return
            
            inCode = False
            txt = obj
            for _ in filter(lambda c: c=='`', txt.text): inCode ^= True
            
            if not inCode: return

            [txt.text, code] = txt.text.rsplit('`', 1)
            txt1 = obj1
            [code1, txt1.text] = txt1.text.split('`', 1)
            
            buffer[1] = ResponseText.Text({"value": '`' + code + ref.text + code1 + '`'})
    
        buffer = deque(maxlen=3)
        try:
            while True:
                while len(buffer) < 3: buffer.append(next(it))
                iInlineRef = -1
                for i, item in enumerate(buffer):
                    if isinstance(item, ResponseText.InlineReference):
                        iInlineRef = i
                        break
                if iInlineRef == 1:
                    fix(buffer)
                    yield buffer.popleft()
                    yield buffer.popleft()
                else:
                    yield buffer.popleft()
        except StopIteration:
            yield from buffer
            buffer.clear()

    def build(self):
        self.content = list(self.fixInlineRefs(self.content))
        return Text(content_it=self.buildContent())

class MessageNode(Node):
    def __init__(self, doc):
        self.messageObj = doc["pastTenseMessage"]
        self.message = self.messageObj["value"]

    def replaceUriLinks(self, message):
        repl = lambda match : f"`{Path.format(self.messageObj["uris"][match.group('uri')]["path"])}`"
        pattern = r"\[\]\((?P<uri>[^)]*)\)"
        return re.sub(pattern, repl, message)

    def build(self):
        self.message = self.replaceUriLinks(self.message)
        return BlockquoteTag(Text(Text.Text(self.message)))

class ToolReadFile(MessageNode):
    pass        
        
class ToolFindTextInFiles(Container):
    def __init__(self, doc):
        self.message = doc["pastTenseMessage"]["value"]
        self.resultDetails = doc["resultDetails"]
    
    def buildContent(self):
        def func(result):
            sl, sc, el, ec = unpackRange(result["range"])
            path = Path.format(result["uri"]["path"])
            return Text.Code(f"{path}:{sl}:{sc}-{el}:{ec}")
            
        return Join(
            map(func, self.resultDetails),
            Text.Linebreak()
        )

    def build(self):
        return BlockquoteTag(
            Text(Text.Text(self.message)),
            Details(Text(content_it=self.buildContent())) if self.resultDetails else None
        )

class ToolFindFiles(Node):
    def __init__(self, doc):
        self.message = doc["pastTenseMessage"]["value"]
        self.resultDetails = doc["resultDetails"]

    def build(self):
        return BlockquoteTag(
            Text(Text.Text(self.message)),
            Details(Text(
                content_it=Join(
                    (Text.Code(Path.format(result["path"])) for result in self.resultDetails),
                    Text.Linebreak()
                )
            )) if self.resultDetails else None
        )

class ToolGetErrors(MessageNode):
    pass

class ToolEdit(Container):
    @staticmethod
    def getFileEdits(chunks):
        return filter(
            lambda chunk: chunk.get("kind", None) == "textEditGroup",
            chunks
        )
    
    @staticmethod
    def makeChunks(it:Buffered):
        pass
    
    def __init__(self, it):
        self.chunks = self.makeChunks(it)

        for fileEdit in self.getFileEdits(self.chunks):
            Chat.instance.requestedFiles.add(
                fileEdit["uri"]["path"]
            )
    
    def build(self):
        return BlockquoteTag(content_it=self.buildContent())

    def editFiles(self):
        pass
    
    def buildContent(self):
        editedFiles = self.editFiles()
        
        for path, file in editedFiles.items():
            fileVersions = Chat.instance.files[path]
            yield Text(Text.Text("Edited "), Text.Code(Path.format(path)))
            prev = fileVersions[-1] if fileVersions else None
            if prev is not None:
                diffLines = list(
                    unified_diff(
                            prev.buffer,
                            file.buffer,
                            lineterm="",
                            fromfile="before",
                            tofile="after"
                    )
                )
                yield Details(
                    CodeBlock(
                        lang="diff",
                        codeLines=diffLines
                    )
                )
            fileVersions.append(file)

class ToolInsertEdit(ToolEdit):
    @staticmethod
    def makeChunks(it: Buffered):
        matchedFilter = MatchedFilter(
            it,
            (
                Matcher(lambda c: c.get("toolId", "") == "copilot_insertEdit"),
                Matcher(lambda c: c.get("toolId", "") == "vscode_editFile_internal"),
                Matcher(lambda c: c.get("value", "") == "\n````\n"),
                Matcher(lambda c: c.get("kind", "") == "undoStop"),
                Matcher(lambda c: c.get("kind", "") == "codeblockUri"),
                Matcher(lambda c: c.get("value", "") == "\n````\n"),
                Matcher(lambda c: c.get("kind", "") == "textEditGroup", n=-1)
            )
        )
        chunks = list(matchedFilter)
        if matchedFilter.error:
            print("Error extracting copilot_insertEdit information")
            print(matchedFilter.errorObj)
        return chunks
    
    def editFiles(self):
        editedFiles = {}
        for fileEdit in self.getFileEdits(self.chunks):
            path = fileEdit["uri"]["path"]
            file = File()
            file.applyEdits(fileEdit["edits"])
            editedFiles[path] = file
        return editedFiles

class ToolReplaceString(ToolEdit):
    @staticmethod
    def makeChunks(it:Buffered):
        matchedFilter = MatchedFilter(
            it,
            (
                Matcher(lambda c: c.get("toolId", "") == "copilot_replaceString"),
                Matcher(lambda c: c.get("value", "") == "\n```\n"),
                Matcher(lambda c: c.get("kind", "") == "undoStop"),
                Matcher(lambda c: c.get("kind", "") == "codeblockUri"),
                Matcher(lambda c: c.get("kind", "") == "textEditGroup", n=-1),
                Matcher(lambda c: c.get("value", "") == "\n```\n"),
            )
        )
        chunks = list(matchedFilter)
        if matchedFilter.error:
            print("Error extracting copilot_replaceString information")
            print(matchedFilter.errorObj)
        return chunks

    def editFiles(self):
        editedFiles = {}
        for fileEdit in self.getFileEdits(self.chunks):
            path = fileEdit["uri"]["path"]
            file = editedFiles.get(path, None)
            if file is None:
                fileVersions = Chat.instance.files[path]
                prev = fileVersions[-1] if fileVersions else None
                file = File.copy(prev) if prev else File()
            file.applyEdits(fileEdit["edits"])
            editedFiles[path] = file
        return editedFiles
