from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Enum, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import UTCDateTime

# PostgreSQL uses BIGINT; SQLite needs exactly INTEGER for generated primary keys.
id_type = BigInteger().with_variant(Integer(), "sqlite")


def enum_type(enum: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now()
    )
