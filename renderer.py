import xml.etree.ElementTree as ET
from collections import defaultdict
from utils import flatten, buffered
import re
from difflib import unified_diff
from arango import ArangoClient
from operator import itemgetter

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
                buffer[i-1] = line[:startColumn] + string + line[endColumn:]
            elif i == startLineNumber: 
                buffer[i-1] = line[:startColumn] + string
            elif i == endLineNumber:
                buffer[i-1] = string + line[endColumn:]
            else: 
                buffer[i-1] = string

    def applyEdits(self, edits):
        for edit in (
            obj 
            for lst in edits
                for obj in lst
        ):
            text = edit["text"]
            (
                startLineNumber, startColumn, endLineNumber, endColumn
            ) = itemgetter(
                "startLineNumber", "startColumn", "endLineNumber", "endColumn"
            )(edit["range"])
            
            lineEdits = []

            if startLineNumber == endLineNumber: 
                text = text.replace('\n', '')
                lineEdits = [text]
                if len(text) > endColumn - startColumn:
                    print("Inconsistent edit:")
                    print(edit)
                    endColumn = startColumn + len(text)
            else:
                lineEdits = text.splitlines()
            
            if endColumn == 1 and len(lineEdits[-1]) > 0:
                print("Inconsistent edit:")
                print(edit)
                endColumn = len(lineEdits[-1]) + 1

            self.insert(
                start=(startLineNumber, startColumn), 
                end=(endLineNumber, endColumn), 
                strings=lineEdits
            )

class Chat: 
    instance = None   
    
    @classmethod
    def fromKey(cls, key):
        return cls(DB.get_document("chat-logs", key))
    
    @classmethod
    def extractAttachments(cls, metadata_lst):
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
                    cls.files[filePath].insert(
                        0, 
                        File(
                            path = filePath,
                            buffer = text.splitlines()
                        )
                    )
                else:
                    print(f"Encountered unknown attachment\n{attachment}")


    def __init__(self, doc):
        Chat.instance = self
        self.requesterUsername = doc["requesterUsername"]
        self.responderUsername = doc["responderUsername"]
        self.files = defaultdict(list)
        self.editedFiles = set()
        self.requests = [Request(req) for req in doc["requests"]]
        
        metadata_lst = (
            metadata
            for request in doc.get("requests", [])
            if (
                metadata:=request
                .get("result", {})
                .get("metadata", {})
            )
        )
        self.extractAttachments(metadata_lst)
        
        
    def render(self):
        return map(
            lambda line: line + "\n",
            flatten(
                map(
                    lambda req: req.render(),
                    self.requests
                )
            )
        )
     
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

class Request:
    @staticmethod
    def get_model(responseId):
        try:
            coll = ArangoClient("http://localhost:8529").db().collection("chat-ids")
            cursor = coll.find({"request_id": responseId}, skip=0, limit=1)
            if cursor.empty(): return None
            else: return cursor.next()["model"]
        except:
            return None
    
    def __init__(self, doc):
        self.responseId = doc["result"]["metadata"]["responseId"]
        self.model = self.get_model(self.responseId)
        self.modes = doc["agent"]["modes"]
        self.message = doc["message"]["text"]
        self.response = Response(doc["response"])
        self.variableData = VariableData(doc["variableData"])

    def render(self):
        return [
            map(
                lambda s: s + "\n",
                [
                    f"> {Chat.instance.requesterUsername}:",
                    self.message,
                    *([s] if (s:=self.variableData.render()) else []),
                    "",
                    "> " + Chat.instance.responderUsername + (f" ({self.model})" if self.model else "") + ':'   
                ]
            ),
            self.response.render(),
            (
                [
                    f"Edited file `{path}`",
                    "```diff",
                    unified_diff(lst[0].buffer, lst[-1].buffer, lineterm=""),
                    "```"
                ] for path, lst in Chat.files.items() if len(lst) > 1
            )
        ]
    
class Response:
    def __init__(self, lst):
        self.chunks = []
        it = buffered(lst)
        for chunk in it:
            obj = None
            
            kind = chunk.get("kind", None)
            #tool invocation
            if kind=="toolInvocationSerialized":
                if chunk["toolId"] == "copilot_insertEdit":
                    it.enqueue(chunk)
                    obj = ToolInsertEdit(it)
                
            #text block
            elif (
                ("value" in chunk and kind is None) or
                kind == "inlineReference"
            ):
                it.enqueue(chunk)
                obj = TextBlock(it)
            
            if obj:
                self.chunks.append(obj)
            else:
                print(f"Unknown chunk encountered:\n{chunk}")
    
    def render(self):
        return map(lambda chunk: chunk.render(), self.chunks)

class TextBlock:
    def __init__(self, it):
        self.chunks = []
        for chunk in it:
            kind = chunk.get("kind", None)
            if kind == "inlineReference":
                self.chunks.append(InlineReference(chunk))
            elif "value" in chunk and kind is None:
                self.chunks.append(TextChunk(chunk))
            else:
                it.enqueue(chunk)
                break

    def render(self):
        return (
            ''.join( 
                map(
                    lambda chunk: chunk.render(),
                    self.chunks
                ))
            ).splitlines()
        
class TextChunk:
    def __init__(self, doc):
        self.text = doc["value"]
    def render(self):
        return self.text

class ToolInsertEdit:
    @staticmethod
    def getFileEdits(chunks):
        return filter(
            lambda chunk: chunk.get("kind", None) == "textEditGroup",
            chunks
        )

    def __init__(self, it):
        self.chunks = []
        for chunk in it:
            self.chunks.append(chunk)
            if (
                chunk.get("kind", None) == "textEditGroup" and
                chunk["done"] == True
            ): 
                break

        for fileEdit in self.getFileEdits(self.chunks):
            Chat.instance.editedFiles.add(
                fileEdit["uri"]["path"]
            )
            
        # self.fileEdits = []

        # for fileEdit in editGroups:
        #     path = fileEdit["uri"]["path"]
        #     lst = Chat.files[path]
        #     lst.append(File.from_editList(
        #         path = path,
        #         editList= fileEdit["edits"]
        #     ))
        #     self.fileEdits.append((path, len(lst)-1))        

    def render(self):
        for fileEdit in self.getFileEdits(self.chunks):
            path = fileEdit["uri"]["path"]
            fileVersions = Chat.instance.files[path]
            prev = fileVersions[-1] if fileVersions else None
            file = File.copy(prev) if prev else File(path)
            file.applyEdits(fileEdit["edits"])

class InlineReference:
    def __init__(self, doc):
        ref = doc["inlineReference"]
        self.text = ""
        #file reference
        if "path" in ref:
            self.text = "file `" + ref["path"] + '`'
        #symbol reference
        elif "name" in ref:
            self.text = '`' + ref["name"] + '`'
        
    def render(self):
        return self.text