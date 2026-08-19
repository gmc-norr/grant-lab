"""SQLAlchemy-backed port implementations. Tool code may NOT import this package.

The split is the whole mechanism: `ports/` is stdlib-only and tool-facing, this
package holds every session, model and statement. F3c installs the import gate
that enforces it; F3b establishes the shape.
"""
