from __future__ import annotations

import pydantic
import datetime

from acord.bases import Hashable, File
from acord.core.abc import Route
from acord.models import User, Emoji, Snowflake
from acord.errors import APIObjectDepreciated

from typing import Any, List, Optional, Union


async def _clean_reaction(string):
    if isinstance(string, str):
        string = string[0]
        # UNICODE chars are only 1 character long
        if string.isascii():
            raise ValueError("Incorrect unicode emoji provided")
    elif isinstance(string, Emoji):
        string = str(string)
    else:
        raise ValueError("Unknown emoji")

    return string


class Message(pydantic.BaseModel, Hashable):
    conn: Any
    # Connection Object - For internal use

    activity: Any
    """ sent with Rich Presence-related chat embeds """  # TODO: Message Activity
    application: Any
    """ sent with Rich Presence-related chat embeds """  # TODO: Application Object
    attachments: List[Any]
    """ List of file objects """  # TODO: Asset object
    author: User
    """ User object of who sent the message """
    channel_id: int
    """ id of the channel were the message was send """
    components: List[Any]
    """ List of all components in the message """
    content: str
    """ Message content """

    edited_timestamp: Optional[
        Union[
            bool, datetime.datetime
        ]  # If not false contains timestamp of edited message
    ]
    embeds: List[Any]
    """ List of embeds """  # TODO: Embed object
    flags: int
    """ Message flags """
    id: Snowflake
    """ Message ID """
    interaction: Optional[Any]
    """ Message Interaction """  # TODO: Interaction object
    guild_id: Optional[int]
    """ Guild ID of were message was sent """
    member: Optional[Any]
    """ Member object of who sent the message """  # TODO: Member object
    mentions: List[Union[User, Any]]
    """ List of mentioned users """
    mention_everyone: bool
    """ If message mentioned @everyone """
    mention_roles: List[Any]
    """ If message mentioned any roles """
    mention_channels: Optional[List[Any]]
    """ List of mentioned channels """  # TODO: Channel Object
    nonce: Optional[int]
    """ Message nonce: used for verifying if message was sent """
    pinned: bool
    """ Message pinned in channel or not """
    reactions: Optional[List[Any]] = list()
    """ List of reactions """  # TODO: reaction object
    referenced_message: Optional[Union[Message, Any]]
    """ Replied message """  # TODO: partial message
    thread: Optional[Any]
    """ Thread were message was sent """  # TODO: Channel Thread Object
    timestamp: datetime.datetime
    """ Timestamp of when message was sent """
    tts: bool
    """ Is a text to speech message """
    type: int
    """ Message type, e.g. DEFAULT, REPLY """
    sticker_items: Optional[List[Any]]
    """ List of stickers """  # TODO: Sticker object
    stickers: Optional[List[Any]]
    # Depreciated raises error if provided
    webhook_id: Optional[int]
    """ Webhook message ID """

    """ Extra's """
    channel: Optional[Any]  # type: ignore
    # Returns the :class:`TextChannel` object were the message was sent from.
    # Not the actual object as cyclic imports are a pain

    class Config:
        arbitrary_types_allowed = True

    @pydantic.validator("timestamp")
    def _timestamp_validator(cls, timestamp):
        # :meta private:
        try:
            return datetime.datetime.fromisoformat(timestamp)
        except TypeError:
            if isinstance(timestamp, datetime.datetime):
                return timestamp
            raise

    @pydantic.validator("stickers")
    def _stickers_depr_error(cls, _):
        # :meta private:
        raise APIObjectDepreciated(
            '"stickers" attribute has been dropped, please use "sticker_items"'
        )

    @pydantic.validator("author")
    def _validate_author(cls, data: User, **kwargs):
        # :meta private:
        data = data.dict()
        conn = kwargs["values"]["conn"]

        data["conn"] = conn

        return User(**data)

    @pydantic.validator("channel")
    def _validate_channel(cls, _, **kwargs):
        # :meta private:
        if _ is not None:
            raise ValueError("Channel provided, when expected None")
        conn = kwargs["values"]["conn"]
        channel_id = kwargs["values"]["channel_id"]

        print(repr(channel_id))

        return conn.client.get_channel(int(channel_id))

    def __init__(self, **data):

        super().__init__(**data)

    async def refetch(self) -> Optional[Message]:
        """Attempts to fetch the same message from the API again"""
        resp = await self.conn.request(
            Route(
                "GET",
                path=f"/channels/{self.channel_id}/messages/{self.id}",
                bucket={"channel_id": self.channel_id, "guild_id": self.guild_id},
            )
        )
        return Message(conn=self.conn, **(await resp.json()))

    async def delete(self, *, reason: str = None) -> None:
        """
        Deletes the message from the channel.
        Raises 403 is you don't have sufficient permissions or 404 is the message no longer exists.

        Parameters
        ----------
        reason: :class:`str`
            Reason for deleting message, shows up in AUDIT-LOGS
        """
        await self.conn.request(
            Route("DELETE", path=f"/channels/{self.channel_id}/messages/{self.id}"),
            headers={
                "X-Audit-Log-Reason": reason,
            },
            bucket={
                "channel_id": self.channel_id,
                "guild_id": self.guild_id,
            },
        )

    async def add_reaction(self, emoji: Union[str, Emoji]) -> None:
        """
        Add an emoji to the message.
        Raises 403 if you lack permissions or 404 if message not found.

        Parameters
        ----------
        emoji: Union[:class:`str`, :class:`Emoji`]
            The emoji to add, if already on message does nothing
        """
        emoji = await _clean_reaction(emoji)

        # if self.has_reacted(self.conn.client):
        #     return

        await self.conn.request(
            Route(
                "PUT",
                path=f"/channels/{self.channel_id}/messages/{self.id}/reactions/{emoji}/@me",
                bucket={"channel_id": self.channel_id, "guild_id": self.guild_id},
            ),
        )

    async def remove_reaction(
        self, emoji: Union[str, Emoji], user_id: Union[str, int] = "@me"
    ) -> None:
        """
        Removes a reaction on a message set by a specified user.
        Raises 403 if you lack permissions or 404 if message not found.

        Parameters
        ----------
        emoji: Union[:class:`str`, :class:`Emoji`]
            Reaction to remove
        """
        emoji = await _clean_reaction(emoji)

        await self.conn.request(
            Route(
                "DELETE",
                path=f"/channels/{self.channel_id}/messages/{self.id}/reactions/{emoji}/{user_id}",
                bucket={"channel_id": self.channel_id, "guild_id": self.guild_id},
            ),
        )

    async def clear_reactions(self, *, emoji: Union[str, Emoji] = None) -> None:
        """
        Clear all reactions/x reactions on a message.
        Raises 403 if you lack permissions or 404 if message not found.

        Parameters
        ----------
        emoji: Union[:class:`str`, :class:`Emoji`]
            Emoji to clear, defaults to None meaning all
        """
        if emoji:
            emoji = await _clean_reaction(emoji)
            extension = f"/{emoji}"
        else:
            extension = ""

        await self.conn.request(
            Route(
                "DELETE",
                path=f"/channels/{self.channel_id}/messages/{self.id}/reactions{extension}",
                bucket={"channel_id": self.channel_id, "guild_id": self.guild_id},
            ),
        )

    async def reply(self, verify: Optional[bool] = True, **data) -> Message:
        """Shortcut for `Message.Channel.send(..., reference=self, verify=verify)`"""
        return await self.channel.send(reference=self, verify=verify, **data)
