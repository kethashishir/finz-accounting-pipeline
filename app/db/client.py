"""Asynchronous MongoDB connection management."""

from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

MongoDocument = dict[str, Any]


class MongoDatabase:
    """Own the MongoDB client and selected application database."""

    def __init__(self, uri: str, database_name: str) -> None:
        self.client: AsyncMongoClient[MongoDocument] = AsyncMongoClient(
            uri,
            connect=False,
            serverSelectionTimeoutMS=3000,
            tz_aware=True,
            uuidRepresentation="standard",
        )
        self.database: AsyncDatabase[MongoDocument] = self.client[database_name]

    async def ping(self) -> bool:
        """Return whether MongoDB responds to a lightweight ping command."""

        result = await self.client.admin.command("ping")
        return result.get("ok") == 1.0

    async def close(self) -> None:
        """Release MongoDB sockets and monitoring resources."""

        await self.client.close()
