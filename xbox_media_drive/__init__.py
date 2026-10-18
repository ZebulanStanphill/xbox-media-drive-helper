"""Xbox media preparation. File data and partition tables are never edited."""

__version__ = "0.1.0"


class DriveError(Exception):
    """An actionable failure that is safe to display to the user."""
