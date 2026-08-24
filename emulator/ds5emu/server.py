"""USB/IP server: the TCP shell around `DualSenseDevice`.

Deliberately thin. All protocol semantics live in `wire.py` and all device
semantics in `device.py`, both of which are pure and unit-tested; this module
only moves bytes and decides when to wait.

Connection lifecycle (usbip-win2 client, verified against its sources):

  1. client sends op_common{version=0x0111, code=OP_REQ_DEVLIST, status=0} and
     **nothing else** — there is no request body
     (userspace/libusbip/src/remote.cpp: `send_op_common(s, OP_REQ_DEVLIST)`).
     We reply with the device list and close.
  2. or client sends op_common{OP_REQ_IMPORT} + busid[32]. We reply
     op_common + usbip_usb_device, echoing the busid verbatim — the driver
     compares it and aborts on mismatch (drivers/ude/vhci_ioctl.cpp). The same
     socket then carries USBIP_CMD_SUBMIT / USBIP_CMD_UNLINK until it closes.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

from . import wire as W
from .backend import Backend
from .device import DualSenseDevice
from . import descriptors as D

log = logging.getLogger("ds5emu.server")

DEFAULT_BUSID = "1-1"


class UsbIpServer:
    def __init__(
        self,
        backend: Backend,
        host: str = "127.0.0.1",
        port: int = W.TCP_PORT,
        busid: str = DEFAULT_BUSID,
        hid_in_timeout: float = 0.200,
        hid_in_poll: float = 0.001,
        pace_iso: bool = True,
    ):
        self.backend = backend
        self.host = host
        self.port = port
        self.busid = busid
        # How long an interrupt-IN URB waits for a report before completing
        # empty. The real endpoint is serviced every 4 ms and Bluetooth feeds
        # us at ~476 Hz (docs/STATUS.md §5.3), so a report should almost always
        # be waiting.
        self.hid_in_timeout = hid_in_timeout
        self.hid_in_poll = hid_in_poll

        self.device = DualSenseDevice(backend, pace_iso=pace_iso)
        self._server: asyncio.AbstractServer | None = None
        self.connections = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self.backend.start()
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port, reuse_address=True
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        log.info("USB/IP server listening on %s (busid %s)", addrs, self.busid)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.backend.stop()

    # -- connection handling ----------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        peer = writer.get_extra_info("peername")
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:  # pragma: no cover - platform dependent
                pass
        log.info("connection from %s", peer)
        try:
            await self._op_phase(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            log.info("connection from %s closed", peer)
        except Exception:  # pragma: no cover - defensive
            log.exception("connection from %s failed", peer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover
                pass

    async def _op_phase(self, reader, writer) -> None:
        head = await reader.readexactly(W.OP_COMMON_SIZE)
        version, code, status = W.unpack_op_common(head)
        if version != W.USBIP_VERSION:
            log.warning("client protocol version %#06x != %#06x", version, W.USBIP_VERSION)

        if code == W.OP_REQ_DEVLIST:
            info = self.device.usbip_device_info(busid=self.busid)
            writer.write(W.pack_devlist_reply([info]))
            await writer.drain()
            log.info("OP_REQ_DEVLIST -> 1 device")
            return

        if code == W.OP_REQ_IMPORT:
            body = await reader.readexactly(W.BUS_ID_SIZE)
            busid = W.unpack_import_request_body(body)
            if busid != self.busid:
                log.warning("OP_REQ_IMPORT for unknown busid %r", busid)
                writer.write(W.pack_import_reply(None))
                await writer.drain()
                return
            info = self.device.usbip_device_info(busid=self.busid)
            writer.write(W.pack_import_reply(info))
            await writer.drain()
            log.info("OP_REQ_IMPORT %s -> attached", busid)
            await self._urb_phase(reader, writer)
            return

        log.warning("unexpected op code %#06x", code)

    # -- URB phase ---------------------------------------------------------

    async def _urb_phase(self, reader, writer) -> None:
        send_lock = asyncio.Lock()
        pending: dict[int, asyncio.Task] = {}

        async def send(data: bytes) -> None:
            async with send_lock:
                writer.write(data)
                await writer.drain()

        try:
            while True:
                head = await reader.readexactly(W.HEADER_SIZE)
                command, parsed = W.unpack_header(head)

                if command == W.CMD_SUBMIT:
                    payload = b""
                    n = parsed.payload_size()
                    if n:
                        payload = await reader.readexactly(n)
                    task = asyncio.create_task(self._submit(parsed, payload, send, pending))
                    pending[parsed.seqnum] = task

                elif command == W.CMD_UNLINK:
                    victim = pending.pop(parsed.unlink_seqnum, None)
                    if victim is not None and not victim.done():
                        victim.cancel()
                        status = -W.ECONNRESET
                    else:
                        status = 0  # already completed; nothing to unlink
                    await send(
                        W.pack_ret_unlink(
                            seqnum=parsed.seqnum,
                            devid=parsed.devid,
                            direction=parsed.direction,
                            ep=parsed.ep,
                            status=status,
                        )
                    )
                else:
                    log.warning("unexpected command %d from client", command)
                    return
        finally:
            for task in pending.values():
                task.cancel()

    async def _submit(self, cmd: W.CmdSubmit, payload: bytes, send, pending) -> None:
        try:
            reply = await self._run_submit(cmd, payload)
            await send(reply)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            log.exception("CMD_SUBMIT seq %d ep %#x failed", cmd.seqnum, cmd.ep)
        finally:
            pending.pop(cmd.seqnum, None)

    async def _run_submit(self, cmd: W.CmdSubmit, payload: bytes) -> bytes:
        res = self.device.handle_submit_ex(cmd, payload)

        # Isochronous: the device reserved service intervals for this URB and
        # told us when the last packet is due. Completing early makes the audio
        # stack run fast (see ds5emu/timing.py); completing late glitches it.
        if res.deadline is not None:
            delay = res.deadline - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            return res.reply

        reply = res.reply
        # Interrupt IN with nothing to send: hold the URB rather than completing
        # a zero-length transfer, which is what a real endpoint does (it simply
        # NAKs until data is available).
        #
        # Two different reasons to wait, and they want different waits:
        #
        #   res.retry_at set — the endpoint's 4 ms service interval has not come
        #     round yet, and the device knows exactly when it will. Sleep to
        #     that instant. Polling every `hid_in_poll` instead cost 10 % of the
        #     endpoint's reports on the live driver (220.2/s against a nominal
        #     250): `asyncio.sleep(0.001)` overshoots on Windows, so the open
        #     interval was found late enough that it had already lapsed.
        #
        #   retry_at None — the backend genuinely has nothing (no Bluetooth
        #     report has ever arrived, say). Nobody knows when that changes, so
        #     fall back to polling until `hid_in_timeout`.
        if cmd.is_in and (cmd.ep & 0x7F) == (D.EP_HID_IN & 0x7F) and not cmd.is_iso:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.hid_in_timeout
            while self._is_empty(reply) and loop.time() < deadline:
                if res.retry_at is not None:
                    delay = res.retry_at - time.perf_counter()
                    # Cap the sleep at the remaining hold time so a bad clock
                    # can never park this URB past `hid_in_timeout`.
                    await asyncio.sleep(max(0.0, min(delay, deadline - loop.time())))
                else:
                    await asyncio.sleep(self.hid_in_poll)
                res = self.device.handle_submit_ex(cmd, payload)
                reply = res.reply
        return reply

    @staticmethod
    def _is_empty(reply: bytes) -> bool:
        info = W.unpack_ret_submit(reply)
        return info["status"] == 0 and info["actual_length"] == 0


async def run(backend: Backend, host: str = "127.0.0.1", port: int = W.TCP_PORT,
              busid: str = DEFAULT_BUSID) -> None:
    server = UsbIpServer(backend, host=host, port=port, busid=busid)
    await server.start()
    try:
        await server.serve_forever()
    finally:
        await server.stop()
