"""Database models for PhotoPost tracker."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class TrackedUser(Base):
    __tablename__ = "tracked_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(255), unique=True, nullable=False)
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_polled = Column(DateTime, nullable=True)

    photos = relationship("Photo", back_populates="user", cascade="all, delete-orphan")


class Photo(Base):
    __tablename__ = "photos"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("tracked_users.id"), nullable=False)
    filename = Column(String(500), nullable=False)
    description = Column(Text, default="")
    thumb_url = Column(Text, default="")
    full_url = Column(Text, default="")
    upload_date = Column(DateTime, nullable=True)
    size_bytes = Column(Integer, default=0)
    mime_type = Column(String(100), default="")
    categories = Column(Text, default="")  # JSON array stored as text
    first_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("TrackedUser", back_populates="photos")
    usages = relationship("PhotoUsage", back_populates="photo", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_photos_user_filename", "user_id", "filename", unique=True),
        Index("ix_photos_user_id", "user_id"),
    )


class PhotoUsage(Base):
    __tablename__ = "photo_usages"

    id = Column(Integer, primary_key=True)
    photo_id = Column(Integer, ForeignKey("photos.id"), nullable=False)
    article_title = Column(String(500), nullable=False)
    wiki = Column(String(255), nullable=False)
    article_url = Column(Text, default="")
    first_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True)

    photo = relationship("Photo", back_populates="usages")

    __table_args__ = (
        Index("ix_usages_photo_article", "photo_id", "article_title", "wiki", unique=True),
        Index("ix_usages_active", "is_active"),
        Index("ix_usages_photo_active", "photo_id", "is_active"),
        Index("ix_usages_active_wiki", "is_active", "wiki"),
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id = Column(Integer, primary_key=True)
    photo_id = Column(Integer, ForeignKey("photos.id"), nullable=False)
    article_title = Column(String(500), nullable=False)
    wiki = Column(String(255), nullable=False)
    event_type = Column(String(20), nullable=False)  # "added" or "removed"
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    photo = relationship("Photo")

    __table_args__ = (
        Index("ix_events_photo", "photo_id"),
        Index("ix_events_timestamp", "timestamp"),
    )


def init_db(db_url="sqlite:///photopost.db"):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session
