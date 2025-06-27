from abc import ABC, abstractmethod
from collections.abc import Iterable
class Iterator(ABC):
    @abstractmethod
    def __next__(self):
        pass
    def __iter__(self):
        return self

class flatten(Iterator):
    def __init__(self, it):
        self.its = [iter(it)]

    def __next__(self):
        try:
            obj = next(self.its[-1])
            if isinstance(obj, Iterable) and not type(obj) == str:
                self.its.append(iter(obj))
            else:
                return obj
        except StopIteration:
            self.its.pop()
            if not self.its: raise
        return next(self)

class buffered(Iterator):
    def __init__(self, it):
        self.it = iter(it)
        self.queue = []

    def __next__(self):
        if self.queue:
            return self.queue.pop()
        else:
            return next(self.it)

    def enqueue(self, obj):
        self.queue.append(obj)

class Matcher:
    def __init__(self, check, n=1):
        self.check = check
        self.n = n
    def match(self, obj):
        matched = self.check(obj)
        if matched: self.n -= 1
        return matched
    
class MatchedFilter(Iterator):
    def __init__(self, it: buffered, matchers):
        self.it = iter(it)
        self.matchers = iter(matchers)
        self.matcher = None
        self.error = False
        self.errorObj = None
    def __next__(self):
        if self.matcher is None or self.matcher.n == 0:
            self.matcher = next(self.matchers)
        obj = next(self.it)
        matched = self.matcher.match(obj)
        if matched:
            return obj
        else:
            self.it.enqueue(obj)
            if self.matcher.n > 0:
                self.error = True
                self.errorObj = obj
                raise StopIteration
            else:
                self.matcher = None
                return next(self)