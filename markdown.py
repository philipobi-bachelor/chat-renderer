from abc import ABC, abstractmethod

class Node(ABC):
    @abstractmethod
    def render(self):
        pass

class Container(Node):
    def __init__(self, *content, content_it=None):
        self.content = content or content_it

    def flattenContent(self):
        return (
            line
            for item in self.content
                for line in item.render()
        )
    
    def renderContent(self, indentStr = "", prependEmptyLine=True):
        if prependEmptyLine:
            yield indentStr
        yield from (
            map(lambda line: indentStr + line,
                self.flattenContent())
            if indentStr
            else self.flattenContent()
        )
        

    def render(self):
        yield from self.renderContent()

class Wrapper(Container):
    def render(self):
        if self.content is None: return
        for item in self.content:
            yield from item.render()

class Document(Container):
    def renderContent(self):
        return map(
            lambda line: line + "\n",
            self.flattenContent()
        ) 

class Blockquote(Container):
    def renderContent(self):
        return super().renderContent(
            indentStr="> ",
            prependEmptyLine=False
        )

class BlockquoteTag(Container):
    def render(self):
        if self.content is None:
            yield "<blockquote></blockquote>"
            yield ""
            return
        yield "<blockquote>"
        yield from self.renderContent()
        yield "</blockquote>"
        yield ""

class Text(Container):
    class TextChunk(ABC):
        def __init__(self, content=""):
            self.content = content
        
        @abstractmethod
        def render(self):
            pass

    class Linebreak(TextChunk):
        instance = None
        def __new__(cls):
            if cls.instance is None:
                cls.instance =  object.__new__(cls)
                cls.instance.__init__()
            return cls.instance
        def render(self):
            pass
    
    class Text(TextChunk):
        def render(self):
            return self.content
        
    class Code(TextChunk):
        def render(self):
            return f"`{self.content}`"

    def renderContent(self):
        line = ""
        for chunk in self.content:
            if type(chunk) == Text.Linebreak:
                line += "  "
                yield line
                line = ""
            else:
                line += chunk.render()
        if line: yield line
        yield ""

class Details(Container):
    def __init__(self, *content, content_it=None, summary=None):
        super().__init__(*content, content_it=content_it)
        self.summaryObj = (
            f"<summary>{summary}</summary>" 
            if summary is not None
            else None
        )

    def renderContent(self):
        if self.summaryObj is not None:
            yield ""
            yield self.summaryObj
        yield from super().renderContent()
    
    def render(self):
        if self.content is None:
            yield "<details></details>"
            yield ""
            return
        yield "<details>"
        yield from self.renderContent()
        yield "</details>"
        yield ""

class CodeBlock(Node):
    def __init__(self, codeLines, lang=""):
        self.codeLines = codeLines
        self.lang=lang

    def render(self):
        yield "```" + self.lang
        yield from self.codeLines
        yield "```"
        yield ""
