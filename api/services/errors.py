class ScoutError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class FetchError(ScoutError):
    def __init__(self, code: str, message: str, *, retryable: bool):
        self.retryable = retryable
        super().__init__(code, message)
