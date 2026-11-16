"""Exception types used across the project."""


class DBAAgentError(Exception):
    """Base class for all expected, user-reportable errors."""


class ConfigError(DBAAgentError):
    """Missing or invalid configuration."""


class PromptError(DBAAgentError):
    """Prompt file missing or template variable missing."""


class SessionError(DBAAgentError):
    """Session cannot be found, read, or written."""


class LLMError(DBAAgentError):
    """LLM call failed (connection, HTTP, empty reply...)."""

    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class InvalidResponseError(LLMError):
    """The model answered, but not with usable/valid content."""


class MalformedResponseError(InvalidResponseError):
    """The provider answered with something that is not usable as a model reply (empty, non-JSON, wrong shape).
    Distinct from a *valid* reply that a local validator rejected (plain InvalidResponseError)."""


class ProviderError(LLMError):
    """The API request itself failed (HTTP error, timeout, network). The model never evaluated anything.

    kind: auth | forbidden | quota | rate_limit | unavailable | server | http | timeout | network
    str(e) is concise and never contains credentials.
    """

    def __init__(self, message, retryable=True, kind="http", status=None, request_id=None, retry_after=None):
        super().__init__(message, retryable=retryable)
        self.retry_after = retry_after                    # seconds the provider asked us to wait (429/503), or None
        self.kind = kind
        self.status = status
        self.request_id = request_id
