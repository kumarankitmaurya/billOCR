"""Visual search errors, kept free of heavy imports so the router can map
them to HTTP statuses without loading onnxruntime on servers that have
visual search switched off."""


class NotReady(RuntimeError):
    """Visual search can't serve right now; the message says why."""


class ImageDecodeError(ValueError):
    """The uploaded bytes aren't an image we can read."""


class ImageTooLarge(ValueError):
    """The upload is over the configured byte limit."""
