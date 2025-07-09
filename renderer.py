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
import logging
import sys

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
    addr = "http://project-db:8529"
    
    @staticmethod
    def getCollection(coll):
        return ArangoClient(DB.addr).db().collection(coll)
    
    @staticmethod
    def getDocument(coll, key):
        return DB.getCollection(coll).get(key)
    

class Path:
    home = PurePath("/home/p/Philip.Obi")
    b2root = PurePath("/project/agkuhr/users/pobi/b2")
    roots = [
        home,
        b2root,
        b2root / "basf2",
        b2root / "basf2-v1",
        b2root / "basf2-v2",
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

    """
    Ranges are [startLine, endLine] [startColumn, endColumn)
    """

    @classmethod
    def copy(cls, original):
        return cls(original.buffer.copy())
    
    def replaceString(self, obj):
        (startLineNumber, startColumn, endLineNumber, endColumn) = unpackRange(obj["range"])
        if (nLinesAdded:= endLineNumber-len(self.buffer)) > 0: self.buffer.extend([""]*nLinesAdded)
        
        text = obj["text"]
        lines = text.split('\n')
        lines[0] = self.buffer[startLineNumber-1][:startColumn-1] + lines[0]
        lines[-1] = lines[-1] + self.buffer[endLineNumber-1][endColumn-1:]
        self.buffer = self.buffer[:startLineNumber-1] + lines + self.buffer[endLineNumber:]

    def insertEdit(self, obj):
        obj["text"] = obj["text"].lstrip("\n")
        self.replaceString(obj)

class Node(ABC):
    @abstractmethod
    def build(self):
        pass

class Container(Node):
    def __init__(self, *content, content_it=None):
        self.content = content or content_it or []
    
    def buildContent(self):
        return map(
            lambda item: item.build(),
            self.content
        )
    
    def build(self):
        return Wrapper(content_it=self.buildContent())

class Logger:
    @staticmethod
    def config():
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        logger.addHandler(handler)
        return logger
    
    logger = config()

class Chat(Container): 
    instance = None

    def __init__(self, doc, header=None):
        Chat.instance = self
        self.header = header
        self.requesterUsername = doc["requesterUsername"]
        self.responderUsername = doc["responderUsername"]
        self.files = defaultdict(list)
        self.requestedFiles = set()
        self.editedFiles = set()

        super().__init__(content_it=[Request(req) for req in doc["requests"]])

        for path in self.requestedFiles:
            resPath = Path.resolve(path)
            try:
                with open(resPath, "r") as f:
                   content = f.read()
                   self.files[path].insert(0, File(buffer=content.split('\n'))) 
            except FileNotFoundError as e:
                self.files[path].insert(0, File())
                Logger.logger.warning(f"Could not find requested file {path} (resolved to: {resPath}), used empty file instead")
                Logger.logger.exception(e, exc_info=True)
                continue
    
    @classmethod
    def fromKey(cls, key):
        return cls(
            doc = DB.getDocument("chat-logs", key), 
            header = Text(Text.Text("Document ID: "), Text.Code(f"chat-logs/{key}"))
        )
    
    def build(self):
        editedFilesBlock = None
        
        editedFiles = self.editedFiles & set(self.files.keys())

        if len(editedFiles) > 0:
            def func(path):
                fmtPath = Path.format(path)
                fileA = self.files[path][0]
                fileB = self.files[path][-1]
                fromfile = f"a/{fmtPath}"
                tofile = f"b/{fmtPath}"
                nLines = max(len(fileA.buffer), len(fileB.buffer))
                return Wrapper(
                    Text(Text.Code(fmtPath), Text.Text(":")),
                    Details(
                        CodeBlock(
                        lang="diff",
                        codeLines=unified_diff(
                            fileA.buffer,
                            fileB.buffer,
                            fromfile=fromfile,
                            tofile=tofile,
                            lineterm="")
                        ),
                        summary="Squashed changes (short)"
                    ),
                    Details(
                        CodeBlock(
                        lang="diff",
                        codeLines=unified_diff(
                            fileA.buffer,
                            fileB.buffer,
                            fromfile=fromfile,
                            tofile=tofile,
                            n=nLines,
                            lineterm="")
                        ),
                        summary="Squashed changes (full)"
                    )
                )

            editedFilesBlock = BlockquoteTag(
                Text(Text.Heading(5, "Edited Files:")),
                Wrapper(content_it=map(func, editedFiles))
            )

        return Document(
            self.header,
            Wrapper(content_it=self.buildContent()),
            editedFilesBlock
        )

class Request(Container):
    @staticmethod
    def getModel(responseId):
        try:
            coll = DB.getCollection("chat-ids")
            cursor = coll.find({"request_id": responseId}, skip=0, limit=1)
            if cursor.empty(): return None
            else: return cursor.next()["model"]
        except:
            return None
    
    def __init__(self, request):
        
        result = request["result"]
        responseId = result["metadata"]["responseId"]
        self.model = Request.getModel(responseId)
        self.error = result.get("errorDetails", None)
        self.timeMs = result["timings"]["totalElapsed"]
        self.message = request["message"]["text"]
        self.response = Response(request["response"])
        self.variables = list(dict.fromkeys(map(lambda varObj: varObj["name"], request["variableData"]["variables"])))
        super().__init__(self.response)
    
    def build(self):
        return Wrapper(
            BlockquoteTag(
                Text(
                    Text.Heading(4, Chat.instance.requesterUsername + ':'),
                    Text.Text(self.message)
                ),
                Text(
                    Text.Text("Variables: "),
                    *Join(map(lambda varName: Text.Code(varName), self.variables), Text.Text(", "))
                ) if self.variables else None
            ),
            BlockquoteTag(
                Text(Text.Heading(
                        level = 4,
                        content = Chat.instance.responderUsername + (f" ({self.model}):" if self.model else ":")
                )),
                Wrapper(content_it=self.buildContent()),
                BlockquoteTag(
                    Text(Text.Text("Error: "), Text.Text(self.error.get("message", "Unknown Error")))
                ) if self.error is not None else None,
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
                    case "progressTaskSerialized":
                        obj = ProgressTaskSerialized(chunk)
                    case "toolInvocationSerialized":
                        toolId = chunk["toolId"]
                        match toolId:
                            case "copilot_createFile":
                                it.enqueue(chunk)
                                obj = ToolCreateFile(it)
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
                            case "copilot_searchCodebase":
                                obj = ToolSearchCodebase(chunk)
                            case "copilot_findFiles":
                                obj = ToolFindFiles(chunk)
                            case "copilot_getErrors":
                                obj = ToolGetErrors(chunk)
                            case "copilot_runInTerminal":
                                obj = ToolRunInTerminal(chunk)
            
            if obj:
                yield obj
            else:
                Logger.logger.info("Unknown chunk encountered:")
                Logger.logger.info(str(chunk))
   
    def __init__(self, lst):
        super().__init__(*self.processChunks(lst))

class Confirmation(Node):
    def __init__(self, doc):
        self.message = doc["message"]

    def build(self):
        return BlockquoteTag(Text(
            Text.Text(self.message)
        ))
    
class ProgressTaskSerialized(Node):
    def __init__(self, doc):
        self.text = doc["content"]["value"]
    def build(self):
        return BlockquoteTag(Text(
            Text.Text(self.text)
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

class ToolSearch(Container):
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

class ToolFindTextInFiles(ToolSearch):
    pass

class ToolSearchCodebase(ToolSearch):
    pass

class ToolRunInTerminal(Node):
    def __init__(self, doc):
        self.obj = doc
            
    def build(self):
        toolSpecificData = self.obj.get("toolSpecificData", None)
        if toolSpecificData is None:
            Logger.logger.warning("copilot_runInTerminal invocation has no toolSpecificData")
            Logger.logger.warning(self.obj)
            return None
        
        return BlockquoteTag(
            Text(Text.Text("Run in Terminal:")),
            CodeBlock(
                codeLines=toolSpecificData.get("command", "").split('\n'),
                lang=toolSpecificData.get("language", "")
            ),
            Text(
                Text.Text("Executed: "), 
                Text.Code("true" if self.obj.get("isConfirmed", False) else "false")
            )
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
            path = fileEdit["uri"]["path"]
            Chat.instance.requestedFiles.add(path)
            Chat.instance.editedFiles.add(path)
    
    def build(self):
        return BlockquoteTag(content_it=self.buildContent())
    
    def buildContent(self):
        editedFiles = self.editFiles()
        
        for path, file in editedFiles.items():
            fileVersions = Chat.instance.files[path]
            yield Text(Text.Text("Edited "), Text.Code(Path.format(path)))
            prev = fileVersions[-1] if fileVersions else None
            if prev is not None:
                fmtPath = Path.format(path)
                yield Details(
                    CodeBlock(
                        lang="diff",
                        codeLines=unified_diff(
                            prev.buffer,
                            file.buffer,
                            lineterm="",
                            fromfile="a/" + fmtPath,
                            tofile="b/" + fmtPath
                        )
                    )
                )
            fileVersions.append(file)


    def editFile(self, file:File, edits):
        pass
    
    def editFiles(self):
        editedFiles = {}
        for fileEdit in self.getFileEdits(self.chunks):
            path = fileEdit["uri"]["path"]
            file = editedFiles.get(path, None)
            if file is None:
                fileVersions = Chat.instance.files[path]
                prev = fileVersions[-1] if fileVersions else None
                file = File.copy(prev) if prev else File()
            edits = (
                edit
                for lst in fileEdit["edits"]
                    for edit in lst
            )
            self.editFile(file, edits)
            editedFiles[path] = file

        return editedFiles

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
            Logger.logger.warning("Error extracting copilot_insertEdit information")
            Logger.logger.warning(matchedFilter.errorObj)
        return chunks
    
    def editFile(self, file:File, edits):
        for edit in edits:
            file.insertEdit(edit)

class ToolReplaceString(ToolEdit):
    @staticmethod
    def makeChunks(it:Buffered):
        matchedFilter = MatchedFilter(
            it,
            (
                Matcher(lambda c: c.get("toolId", "") == "copilot_replaceString", n=-1),
                Matcher(lambda c: c.get("value", "") == "\n```\n"),
                Matcher(lambda c: c.get("kind", "") == "undoStop"),
                Matcher(lambda c: c.get("kind", "") == "codeblockUri"),
                Matcher(lambda c: c.get("kind", "") == "textEditGroup", n=-1),
                Matcher(lambda c: c.get("value", "") == "\n```\n"),
            )
        )
        chunks = list(matchedFilter)
        if matchedFilter.error:
            Logger.logger.warning("Error extracting copilot_replaceString information")
            Logger.logger.warning(matchedFilter.errorObj)
        return chunks

    def editFile(self, file:File, edits):
        for edit in edits:
            file.replaceString(edit)

class ToolCreateFile(ToolEdit):
    @staticmethod
    def makeChunks(it: Buffered):
        matchedFilter = MatchedFilter(
            it,
            (
                Matcher(lambda c: c.get("toolId", "") == "copilot_createFile"),
                Matcher(lambda c: c.get("kind", "") == "textEditGroup", n=-1) ,
            )
        )
        chunks = list(matchedFilter)
        if matchedFilter.error:
            Logger.logger.warning("Error extracting copilot_createFile information")
            Logger.logger.warning(matchedFilter.errorObj)
        return chunks
    
    def editFile(self, file:File, edits):
        for edit in edits:
            file.insertEdit(edit)

    def __init__(self, it):
        self.chunks = self.makeChunks(it)

        for fileEdit in self.getFileEdits(self.chunks):
            self.createdFilePath = fileEdit["uri"]["path"]
            #initialize empty file
            Chat.instance.files[self.createdFilePath].insert(0, File())
            Chat.instance.editedFiles.add(self.createdFilePath)

    def buildContent(self):
        yield Text(Text.Text("Created "), Text.Code(Path.format(self.createdFilePath)))
        yield from super().buildContent()

