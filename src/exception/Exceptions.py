class CustomException(Exception):
    pass

class InvalidTokenError(Exception):
    def __init__(self, message, errors):
        super().__init__(message)
        self.errors = errors