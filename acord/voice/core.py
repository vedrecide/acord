# Voice websocket connection
from __future__ import annotations

from asyncio import (
    AbstractEventLoop,
    Event
)
from aiohttp import ClientSession, WSMsgType

# For handling voice packets
from struct import pack_into, pack
import nacl.secret
from acord.core.heartbeat import VoiceKeepAlive
from .udp import UDPConnection

from acord.bases import _C

import logging

global CONNECTIONS
CONNECTIONS = 0
logger = logging.getLogger(__name__)


class VoiceWebsocket(object):
    supported_modes = (
        'xsalsa20_poly1305_lite',
        'xsalsa20_poly1305_suffix',
        'xsalsa20_poly1305',
    )

    def __init__(self, voice_packet: dict, loop: AbstractEventLoop, client, channel_id, **kwargs) -> None:
        # Defined in an async enviro so this is fine
        self._session = ClientSession(loop=loop, **kwargs)
        self._packet = voice_packet
        self._connect = False
        self._loop = loop
        self._client = client
        self.channel_id = channel_id

        self._ws = None
        self._keep_alive = None
        self._ready_packet = None
        self._sock = None
        self._listener = None

        self.sequence: int = 0
        self.timestamp: int = 0
        self.timeout: float = 0
        self.ssrc: int = 0
        self._lite_nonce: int = 0
        self.mode = None

        self.connect_event = Event()
        self.send_event = Event()
        self.disconnected = True

    async def wait_until_connected(self):
        await self.connect_event.wait()

    async def wait_until_ready(self):
        await self.wait_until_connected()
        await self.send_event.wait()

    async def connect(self, *, v: int = 4) -> None:
        global CONNECTIONS

        # connects to desired endpoint creating new websocket connection
        logger.debug(f"Attempting to connect to {self._packet['d']['endpoint']}")
        ws = await self._session.ws_connect(
           f"wss://{self._packet['d']['endpoint']}?v={v}"
        )
        CONNECTIONS += 1
        self._conn_id = CONNECTIONS
        logger.info(f"Successfully connected to {self._packet['d']['endpoint']}, awaiting UDP handshake")

        self._ws = ws
        self.disconnected = False

    async def disconnect(self, *, message: bytes = b"") -> None:
        if self.disconnected:
            logger.warn(f"Disconnect called on disconnected socket, conn_id={self._conn_id}")
            return
        if not self._keep_alive:
            raise ConnectionError("Keepalive doesn't exist, failed to disconnect ws")

        logger.debug(f"Client disconnected from VC conn_id={self._conn_id}, ending operations")
        self._keep_alive.end()

        try:
            await self._ws.close(code=4000, message=message)
        except Exception:
            pass
        # WS already closed or anything along them lines

        # Disconnect called before sock was intialised
        if self._sock:
            await self._sock.close()
            logger.info(f"Disconnected from {self._sock._sock}")

        self._ws = None
        self._sock = None
        self._keep_alive = None

        if self._listener:
            self._listener.cancel("Disconnect called to end conn")
            self._listener = None
            logger.debug("Ended listener task")

        logger.info("Disconnected from voice, Closed ws & socket and ended heartbeats")
        self.disconnected = True

    async def reconnect(self) -> None:
        logger.info(f"Disconnecting from {self._sock._sock}")
        await self.disconnect()

        self._ws = None

        await self.connect()

    async def resume(self) -> None:
        await self._ws.send_json({
            "op": 7,
            "d": {
                "server_id": self._packet["d"]["guild_id"],
                "session_id": self._packet["d"]["session_id"],
                "token": self._packet["d"]["token"]
            }
        })

    def identity(self):
        return {
            "op": 0,
            "d": {
                "server_id": self._packet["d"]["guild_id"],
                "user_id": self._packet["d"]["user_id"],
                "session_id": self._packet["d"]["session_id"],
                "token": self._packet["d"]["token"]
            }
        }

    def udp_payload(self, *, mode: str = None):
        if not mode:
            mode = self._ready_packet["d"]["modes"][0]

        if mode not in self.supported_modes:
            raise ValueError("Encountered unknown mode")
        self.mode = mode

        return {
            "op": 1,
            "d": {
                "protocol": "udp",
                "data": {
                    "address": self._ready_packet["d"]["ip"],
                    "port": self._ready_packet["d"]["port"],
                    "mode": mode
                }
            }
        }

    def checked_add(self, attr, value, limit):
        val = getattr(self, attr)
        if (val + value) > limit:
            setattr(self, attr, 0)
        else:
            setattr(self, attr, (val + value))

    async def upd_connect(self, addr: str, port: int, **kwargs) -> None:
        # Finishes handshake whilst connected to vc
        # self._sock will be a tuple with the transport and protocol

        logger.debug(f"Attempting to complete UDP connection for conn_id={self._conn_id}")
        
        conn = UDPConnection(
            self._ready_packet["d"]["ip"], 
            self._ready_packet["d"]["port"],
            self._loop, **kwargs)
        await conn.connect()

        logger.info(f"Successfully connected to {addr}:{port} for conn_id={self._conn_id}")

        self._sock = conn

    def _get_audio_packet(self, data: bytes) -> bytes:
        header = bytearray(12)
        
        header[0] = 0x80
        header[1] = 0x70

        pack_into('>H', header, 2, self.sequence)
        pack_into('>I', header, 4, self.timestamp)
        pack_into('>I', header, 8, self.ssrc)

        encrypter = getattr(self, f"_encrypt_{self.mode}")
        return encrypter(header, data)

    async def send_audio_packet(self, 
        data: bytes, flags: int = 5, 
        *, 
        has_header: bool = False,
        sock_flags: int = 0,
        delay: int = 0
    ) -> None:
        """|coro|

        Sends an audio packet to discord

        Parameters
        ----------
        data: :class:`bytes`
            Bytes of data to send to discord
        has_header: :class:`bool`
            Whether the data has an RTC header attached to it.
            Defaults to False and should only be True if you know what your doing.
        """
        if not has_header:
            data = self._get_audio_packet(data)

        await self._ws.send_json({
            "op": 5,
            "d": {
                "speaking": flags,
                "delay": delay,
                "ssrc": self.ssrc
            }
        })

        await self._sock.write(data, flags=sock_flags)

        # Sequence should be incremented after each packet is sent
        self.sequence += 1

    async def listen(self, **kwargs) -> None:
        """ Begins to listen for websocket events, 
        to terminate this simply end generated task """
        tsk = self._loop.create_task(self._handle_voice(**kwargs))

        self._listener = tsk  

    async def _handle_voice(self, *, after: _C = None, **kwargs) -> None:
        """ Handles incoming data from websocket """
        if not self._ws:
            raise ValueError("Not established websocket connecting")
        await self._ws.send_json(self.identity())
        logger.info(f"Sent identity packet for voice ws conn_id={self._conn_id}")

        while True:
            try:
                message = await self._ws.receive()
            except ConnectionResetError:
                break
            try:
                data = message.json()
            except TypeError:
                if message.type == WSMsgType.ERROR:
                    logger.error(f"Voice WS for conn_id={self._conn_id} has closed", exc_info=(
                        type(message.extra), message.extra, message.extra.__traceback__
                    ))
                else:
                    logger.info(f"Received invalid json data for voice ws conn_id={self._conn_id}, closing ws")
                
                await self.disconnect()
                break

            if data["op"] == 8:
                self._keep_alive = VoiceKeepAlive(self, data)
                self._keep_alive.start()

            if data["op"] == 2:
                self._ready_packet = data
                await self.upd_connect(
                    data["d"]["ip"],
                    data["d"]["port"],
                    client=self._client,
                    vc_Ws=self,
                    conn_id=self._conn_id
                )
                await self._ws.send_json(self.udp_payload(**kwargs))
                self.connect_event.set()

                self.ssrc = data["d"]["ssrc"]

                if after:
                    await after()

            if data["op"] == 4:
                self._decode_key = data["d"]["secret_key"]
                self.send_event.set()

            if data["op"] == 13:
                await self.disconnect()
                break

    # NOTE: encryption methods

    def _encrypt_xsalsa20_poly1305(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self._decode_key))
        nonce = bytearray(24)
        nonce[:12] = header

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext

    def _encrypt_xsalsa20_poly1305_suffix(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self._decode_key))
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)

        return header + box.encrypt(bytes(data), nonce).ciphertext + nonce

    def _encrypt_xsalsa20_poly1305_lite(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self._decode_key))
        nonce = bytearray(24)

        nonce[:4] = pack('>I', self._lite_nonce)
        self.checked_add('_lite_nonce', 1, 4294967295)

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext + nonce[:4]

    # NOTE: properties and what not

    @property
    def guild_id(self) -> str:
        return self._packet["d"]["guild_id"]
