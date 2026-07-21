"""Allow pure unit tests to run before third-party dependencies are installed."""

import sys
import types


try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")

    class Response:  # only used by postponed type annotations
        pass

    def dependency_missing(*args, **kwargs):
        raise RuntimeError("requests is not installed")

    requests_stub.Response = Response
    requests_stub.get = dependency_missing
    requests_stub.post = dependency_missing
    sys.modules["requests"] = requests_stub
