"""Same exception names as pytrends.exceptions, so existing `except` blocks keep working."""


class ResponseError(Exception):
    """Google (or the hosted fallback) answered with an error. `response` has status_code, text and url."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response

    @classmethod
    def from_response(cls, response):
        return cls(f"The request failed: Google returned a response with code {response.status_code}", response)


class TooManyRequestsError(ResponseError):
    """HTTP 429: Google rate-limited this IP or session."""
