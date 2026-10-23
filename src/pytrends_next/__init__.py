"""Maintained drop-in replacement for pytrends: `from pytrends_next.request import TrendReq`."""
from .exceptions import ResponseError, TooManyRequestsError
from .request import TrendReq

__version__ = "0.1.0"
__all__ = ["TrendReq", "ResponseError", "TooManyRequestsError"]
