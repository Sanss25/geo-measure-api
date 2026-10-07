class IngestError(Exception):
    """The uploaded file is unusable. The message is safe to show to the client."""


class UploadTooLarge(Exception):
    """The upload exceeded the configured size limit."""
