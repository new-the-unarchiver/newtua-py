"""
Exception hierarchy for newtua.

The classes mirror the engine's own error variants, so callers can branch on
the kind of failure instead of parsing message text.
"""

from typing import NoReturn

__all__ = [
    "CorruptArchiveError",
    "EntryNotFoundError",
    "MissingVolumeError",
    "NewtuaError",
    "PasswordRequiredError",
    "UnknownFormatError",
    "UnsafePathError",
    "UnsupportedError",
    "WrongPasswordError",
]


class NewtuaError(Exception):
    """Base class for every error raised by the newtua engine."""


class UnknownFormatError(NewtuaError):
    """The archive format could not be recognised."""


class UnsupportedError(NewtuaError):
    """The format is known, but this particular feature is not supported."""


class PasswordRequiredError(NewtuaError):
    """The archive is encrypted and no password was supplied."""


class WrongPasswordError(NewtuaError):
    """The supplied password did not decrypt the archive."""


class CorruptArchiveError(NewtuaError):
    """The archive data is damaged."""


class MissingVolumeError(NewtuaError):
    """A volume of a multi-part archive is missing."""


class UnsafePathError(NewtuaError):
    """An entry's path would escape the destination directory."""


class EntryNotFoundError(KeyError):
    """
    No entry with that name.

    Inherits `KeyError` only, so code around `archive["name"]` reads exactly
    like code around a dict.
    """


_BY_KIND: dict[str, type[BaseException]] = {
    "unknown_format": UnknownFormatError,
    "unsupported": UnsupportedError,
    "encrypted": PasswordRequiredError,
    "wrong_password": WrongPasswordError,
    "corrupt": CorruptArchiveError,
    "missing_volume": MissingVolumeError,
    "path_traversal": UnsafePathError,
    "io": OSError,
    "invalid_index": IndexError,
}


def raise_for(exc: BaseException) -> NoReturn:
    """
    Re-raise an error from the compiled module as its typed counterpart.

    Falls back to `NewtuaError` for a kind this version does not know, so a
    newer engine never surfaces as a bare, unrecognised exception.
    """
    kind = getattr(exc, "kind", None)
    cls = _BY_KIND.get(kind, NewtuaError) if isinstance(kind, str) else NewtuaError
    raise cls(str(exc)) from exc
