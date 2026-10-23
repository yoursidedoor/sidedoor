"""A fake OS credential store for the tests (E2.T4): a dict behind the backend interface
credentials uses (read / write / delete by account). No test touches a real keychain."""

import credentials


class FakeStore:
    name = "Keychain"
    waiting_text = credentials.LOCKED_TEXT["darwin"]
    unavailable_text = credentials.UNAVAILABLE_TEXT

    def __init__(self):
        self.items = {}
        self.calls = []
        self.fail = {}        # operation -> exception to raise
        self.garble = False   # write stores a different value (a read-back that differs)

    def _maybe_fail(self, operation):
        self.calls.append(operation)
        error = self.fail.get(operation)
        if error is not None:
            raise error

    def read(self, account):
        self._maybe_fail("read")
        return self.items.get(account)

    def write(self, account, value):
        self._maybe_fail("write")
        self.items[account] = value + "x" if self.garble else value

    def delete(self, account):
        self._maybe_fail("delete")
        self.items.pop(account, None)

    def values(self):
        return sorted(self.items.values())


def use_fake(testcase, store=None):
    """Make a FakeStore the store until `testcase` ends; returns it."""
    store = store or FakeStore()
    previous = credentials.use_backend(store)
    testcase.addCleanup(credentials.use_backend, previous)
    return store
