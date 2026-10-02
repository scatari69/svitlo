from sqlalchemy import BigInteger, CheckConstraint, Identity
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import Timestamps, id_type


class User(Timestamps, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("telegram_user_id > 0", name="ck_users_telegram_id"),)

    id: Mapped[int] = mapped_column(id_type, Identity(), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
