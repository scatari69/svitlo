from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared metadata and a deliberately field-free representation."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"
