"""Exceptions shared across modules."""


class PortalError(Exception):
    """Something on the page was not as expected. The check is treated as failed."""


class SessionExpired(PortalError):
    """We landed on a login page: the saved session is no longer valid."""


class RateLimited(PortalError):
    """HTTP 429 / 503 from the portal."""


class CaptchaDetected(PortalError):
    """A CAPTCHA / bot challenge is showing. We back off; we never try to solve it."""


class BrowserClosed(Exception):
    """The user closed the browser window."""
