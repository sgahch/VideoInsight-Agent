class VideoInsightError(Exception):
    """Base class for expected workflow failures."""


class ExternalDependencyError(VideoInsightError):
    """Raised when a required local command, SDK or API key is missing."""


class WorkflowError(VideoInsightError):
    """Raised when the analysis workflow cannot continue."""

