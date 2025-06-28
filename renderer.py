import xml.etree.ElementTree as ET
from collections import defaultdict
from utils import Join, Buffered, MatchedFilter, Matcher
import re
from difflib import unified_diff
from arango import ArangoClient
from operator import itemgetter
import os.path
from abc import ABC, abstractmethod
from markdown import Document, Text, BlockquoteTag, CodeBlock, Details, Wrapper, Box

def unpackRange(range):
    return itemgetter(
        "startLineNumber",
        "startColumn",
        "endLineNumber",
        "endColumn"
    )(range)

class DB:
    @staticmethod
    def getDocument(coll, key):
        coll = ArangoClient("http://localhost:8529").db().collection(coll)
        return coll.get(key)

class File:
    def __init__(self, path, buffer = []):
        self.path = path
        self.buffer = buffer

    @classmethod
    def copy(cls, original):
        return cls(
            path = original.path,
            buffer = original.buffer.copy() 
        )
    
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
            
            lineEdits = []

            if startLineNumber == endLineNumber: 
                text = text.replace('\n', '')
                lineEdits = [text]
                if len(text) > endColumn - startColumn:
                    endColumn = startColumn + len(text)
            else:
                lineEdits = text.splitlines()
            
            if endColumn == 1 and len(lineEdits[-1]) > 0:
                endColumn = len(lineEdits[-1]) + 1

            self.insert(
                start=(startLineNumber, startColumn), 
                end=(endLineNumber, endColumn), 
                strings=lineEdits
            )

class Node:
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
    
    @classmethod
    def fromKey(cls, key):
        return cls(DB.getDocument("chat-logs", key))
    
    @staticmethod
    def extractAttachments(metadata_lst):
        for text in (
            txt
            for meta in metadata_lst if (msgs:=meta.get("renderedUserMessage", []))
                for msg in msgs if (txt:=msg.get("text", ""))
        ):
            # wrap attachment contents to avoid parser errors
            text = re.sub(
                r"(?P<tag_open><attachment [^>]*>)(?P<tag_content>[\s\S]*?)(?P<tag_close><\/attachment>)",
                r"\g<tag_open><![CDATA[\g<tag_content>]]>\g<tag_close>",
                text
            )
            text = "<root>" + text + "</root>"
            try: root = ET.fromstring(text)
            except ET.ParseError as e:
                print("Error parsing attachments:")
                print(e)
                with open("attachment.txt", "w") as f:
                    f.write(text)
                continue

            for attachment in root.findall(".//attachment"):
                filePath = attachment.attrib.get("filePath", None)
                if filePath is not None:
                    text = attachment.text
                    text = re.sub(r"\s*```\S*\s*", "", text)
                    yield File(
                        path = filePath,
                        buffer = text.splitlines()
                    )
                else:
                    print(f"Encountered unknown attachment\n{attachment}")

    def __init__(self, doc):
        Chat.instance = self
        self.requesterUsername = doc["requesterUsername"]
        self.responderUsername = doc["responderUsername"]
        self.files = defaultdict(list)
        self.requestedFiles = set()

        super().__init__(
            content_it=[Request(req) for req in doc["requests"]]
        )
        
        metadata_lst = (
            metadata
            for request in doc.get("requests", [])
            if (
                metadata:=request
                .get("result", {})
                .get("metadata", {})
            )
        )
        for file in filter(
            lambda f: f.path in self.requestedFiles,
            Chat.extractAttachments(metadata_lst)
        ):
            self.files[file.path].insert(0, file)
        
    
    def build(self):
        return Document(content_it=self.buildContent())
     
class VariableData:
    def __init__(self, doc):
        self.variables = []
        for var in doc["variables"]:
            kind = doc.get("kind", None)
            if kind == "file":
                self.variables.append(f"file: `{doc['name']}`")
            else:
                print(f"Unknown variable encountered:\n{doc}")
    def render(self):
        return ", ".join(self.variables)

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
        
        self.responseId = request["result"]["metadata"]["responseId"]
        self.model = Request.getModel(self.responseId)
        self.modes = request["agent"]["modes"]
        self.message = request["message"]["text"]
        self.response = Response(request["response"])
        self.variableData = VariableData(request["variableData"])
        
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
                Wrapper(content_it=self.buildContent())
            )
        )
    
class Response(Container):
    @staticmethod
    def processChunks(lst):
        it = Buffered(lst)
        for chunk in it:
            obj = None
            
            kind = chunk.get("kind", None)
            if kind is not None:
                if kind == "prepareToolInvocation": continue
                #tool invocation
                elif kind=="toolInvocationSerialized":
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
                    
            #text block
            elif (
                ("value" in chunk and kind is None) or
                kind == "inlineReference"
            ):
                it.enqueue(chunk)
                obj = ResponseText(it)
            
            if obj:
                yield obj
            else:
                print(f"Unknown chunk encountered:\n{chunk}")
   
    def __init__(self, lst):
        super().__init__(*self.processChunks(lst))
    
class ResponseText(Container):
    class Text(Node):
        def __init__(self, doc):
            self.text = doc["value"]
        def build(self):
            return Text.Text(self.text)
        
    class InlineReference(Node):
        def __init__(self, doc):
            ref = doc["inlineReference"]
            self.text = ""
            #file reference
            if "path" in ref:
                self.text = ref["path"]
            #symbol reference
            elif "name" in ref:
                self.text =ref["name"]
            
        def build(self):
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

    def build(self):
        return Text(content_it=self.buildContent())

class ToolReadFile(Node):
    def __init__(self, doc):
        self.files = doc["pastTenseMessage"]["uris"].values() 
    def build(self):
        return Wrapper(
            content_it=(
                BlockquoteTag(Text(
                    Text.Text("Read "), Text.Code(os.path.basename(file["path"]))
                )) for file in self.files
            )
        )
        

class ToolFindTextInFiles(Container):
    def __init__(self, doc):
        self.message = doc["pastTenseMessage"]["value"]
        self.resultDetails = doc["resultDetails"]
    
    def buildContent(self):
        def func(result):
            sl, sc, el, ec = unpackRange(result["range"])
            fname = os.path.basename(result["uri"]["path"])
            return Text.Code(f"{fname}:{sl}:{sc}-{el}:{ec}")
            
        return Join(
            map(func, self.resultDetails),
            Text.Linebreak()
        )

    def build(self):
        return BlockquoteTag(
            Text(Text.Text(self.message)),
            *(
                [Details(Text(content_it=self.buildContent()))]
                if self.resultDetails
                else []
            )
        )

class ToolFindFiles(Node):
    def __init__(self, doc):
        self.message = doc["pastTenseMessage"]["value"]
        self.resultDetails = doc["resultDetails"]

    def build(self):
        return BlockquoteTag(
            Text(Text.Text(self.message)),
            *([Details(Text(
                content_it=Join(
                    (Text.Code(os.path.basename(result["path"])) for result in self.resultDetails),
                    Text.Linebreak()
                )
            ))] 
            if self.resultDetails else [])
        )

class ToolGetErrors(Node):
    def __init__(self, doc):
        obj = doc["pastTenseMessage"]
        self.message = obj["value"]
        self.uris = obj["uris"]
    
    def build(self):
        fmt = lambda obj : os.path.basename(obj["path"])
        repl = lambda match : f"`{fmt(self.uris[match.group('uri')])}`"
        pattern = r"\[\]\((?P<uri>[^)]*)\)"
        self.message = re.sub(pattern, repl, self.message)
        return BlockquoteTag(Text(
            Text.Text(self.message)
        ))

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
            yield Text(Text.Text("Edited "), Text.Code(os.path.basename(path)))
            prev = fileVersions[-1] if fileVersions else None
            if prev is not None:
                yield Details(
                    CodeBlock(
                        lang="diff",
                        codeLines=unified_diff(
                            prev.buffer,
                            file.buffer,
                            lineterm="",
                            fromfile="before",
                            tofile="after"
                        )
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
            file = File(path)
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
                file = File.copy(prev) if prev else File(path)
            file.applyEdits(fileEdit["edits"])
            editedFiles[path] = file
        return editedFiles
        

