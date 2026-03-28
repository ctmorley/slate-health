"""Cross-database compatible column types.

Provides GUID and JSONType that use PostgreSQL-native types (UUID, JSONB)
when running on PostgreSQL and fall back to portable types (String(36), JSON)
on other databases such as SQLite.
"""

import uuid

from sqlalchemy import String, types
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID


class GUID(types.TypeDecorator):
    """Platform-independent UUID type.

    Uses PostgreSQL's UUID column type when available, otherwise stores
    as a 36-character string.
    """

    impl = types.String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        else:
            return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
        else:
            return str(value) if isinstance(value, uuid.UUID) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


class JSONType(types.TypeDecorator):
    """Platform-independent JSON type.

    Uses PostgreSQL's JSONB column type when available, otherwise falls
    back to the generic JSON type.
    """

    impl = types.JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB)
        else:
            return dialect.type_descriptor(types.JSON())
