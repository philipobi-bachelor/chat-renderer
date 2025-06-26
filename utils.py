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
