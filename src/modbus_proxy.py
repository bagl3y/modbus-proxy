# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.


import asyncio
import pathlib
import argparse
import warnings
import contextlib
import logging.config
from urllib.parse import urlparse

__version__ = "0.8.0"


def modbus_crc(data: bytes) -> int:
    """Calculate Modbus CRC16 for the given data.

    Uses polynomial 0xA001 and processes bytes LSB first.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc(frame_without_crc: bytes) -> bytes:
    """Append Modbus CRC16 to a frame.

    Args:
        frame_without_crc: The frame data without CRC

    Returns:
        Frame with CRC appended in little-endian format (low byte first)
    """
    crc = modbus_crc(frame_without_crc)
    return frame_without_crc + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def verify_crc(frame: bytes) -> bool:
    """Verify Modbus CRC16 in a frame.

    Args:
        frame: Complete frame including CRC

    Returns:
        True if CRC is valid, False otherwise
    """
    if len(frame) < 2:
        return False
    data = frame[:-2]
    expected_crc = int.from_bytes(frame[-2:], "little")
    return modbus_crc(data) == expected_crc


DEFAULT_LOG_CONFIG = {
    "version": 1,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)8s %(name)s: %(message)s"}
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"}
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

log = logging.getLogger("modbus-proxy")


def parse_url(url):
    if "://" not in url:
        url = f"tcp://{url}"
    result = urlparse(url)

    # Validate supported schemes
    if result.scheme not in ("tcp", "rtu+tcp"):
        raise ValueError(f"Unsupported URL scheme: {result.scheme}. Supported schemes: tcp, rtu+tcp")

    if not result.hostname:
        url = result.geturl().replace("://", "://0")
        result = urlparse(url)
    return result


class Connection:
    def __init__(self, name, reader, writer):
        self.name = name
        self.reader = reader
        self.writer = writer
        self.log = log.getChild(name)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        await self.close()

    @property
    def opened(self):
        return (
            self.writer is not None
            and not self.writer.is_closing()
            and not self.reader.at_eof()
        )

    async def close(self):
        if self.writer is not None:
            self.log.info("closing connection...")
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as error:
                self.log.info("failed to close: %r", error)
            else:
                self.log.info("connection closed")
            finally:
                self.reader = None
                self.writer = None

    async def _write(self, data):
        self.log.debug("sending %r", data)
        self.writer.write(data)
        await self.writer.drain()

    async def write(self, data):
        try:
            await self._write(data)
        except Exception as error:
            self.log.error("writting error: %r", error)
            await self.close()
            return False
        return True

    async def _read(self):
        """Read ModBus TCP message"""
        # TODO: Handle Modbus RTU and ASCII
        header = await self.reader.readexactly(6)
        size = int.from_bytes(header[4:], "big")
        reply = header + await self.reader.readexactly(size)
        self.log.debug("received %r", reply)
        return reply

    async def read(self):
        try:
            return await self._read()
        except asyncio.IncompleteReadError as error:
            if error.partial:
                self.log.error("reading error: %r", error)
            else:
                self.log.info("client closed connection")
            await self.close()
        except Exception as error:
            self.log.error("reading error: %r", error)
            await self.close()


class Client(Connection):
    def __init__(self, reader, writer):
        peer = writer.get_extra_info("peername")
        super().__init__(f"Client({peer[0]}:{peer[1]})", reader, writer)
        self.log.info("new client connection")


class RtuOverTcpClient(Connection):
    """Client for Modbus RTU over TCP connections.
    
    This class handles conversion between Modbus TCP (MBAP) and Modbus RTU framing.
    It maintains a single persistent connection to the RTU-over-TCP server and
    serializes all requests using an asyncio.Lock to ensure proper Modbus operation.
    
    Security considerations:
    - Debug logging exposes raw Modbus frames (may contain sensitive register data)
    - No authentication/encryption (relies on network security)
    
    Known limitations:
    - Only supports function codes: 0x01-0x06, 0x0F, 0x10, 0x17
    - Connection is closed on any unsupported function code to prevent desync
    - No inter-frame timeout detection (relies on TCP framing)
    """

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        super().__init__(f"RTU-TCP({host}:{port})", None, None)
        self._host = host
        self._port = port
        self._timeout = timeout
        self._lock = asyncio.Lock()  # Serialize requests to ensure RTU master/slave order
        # Metrics
        self._request_count = 0
        self._crc_error_count = 0
        self._timeout_count = 0
        self._protocol_error_count = 0

    async def connect(self):
        """Connect or reconnect to the RTU-over-TCP server.
        
        Checks if the connection is still alive and reconnects if necessary.
        This handles cases where the server closes the connection after idle timeout.
        """
        # Check if connection appears open but is actually dead
        if self.opened and self.writer and self.writer.is_closing():
            self.log.warning("connection appears closed, reconnecting...")
            await self.close()
        
        if not self.opened:
            self.log.info("connecting to RTU-over-TCP server...")
            self.reader, self.writer = await asyncio.open_connection(
                self._host, self._port
            )
            self.log.info("connected!")

    async def write_read(self, mbap_request: bytes) -> bytes:
        """Convert MBAP request to RTU, send, receive RTU response, convert back to MBAP."""
        self._request_count += 1
        
        # Parse MBAP request
        if len(mbap_request) < 7:
            self._protocol_error_count += 1
            raise ValueError("Invalid MBAP request - too short")

        transaction_id = int.from_bytes(mbap_request[0:2], "big")
        protocol_id = int.from_bytes(mbap_request[2:4], "big")
        length = int.from_bytes(mbap_request[4:6], "big")
        unit_id = mbap_request[6]
        pdu = mbap_request[7:]

        if protocol_id != 0:
            raise ValueError(f"Invalid protocol ID: {protocol_id}")

        # MBAP length field specifies the number of bytes following (unit_id + PDU)
        # Total MBAP message length = 6 (header) + length
        if len(mbap_request) != 6 + length:
            raise ValueError(f"MBAP length mismatch: expected {length} bytes after header, got {len(mbap_request) - 6}")

        # Convert to RTU request: unit_id + pdu + crc
        rtu_request = append_crc(bytes([unit_id]) + pdu)
        self.log.debug("RTU request: %r", rtu_request.hex())

        async with self._lock:
            # Send RTU request
            await self.connect()
            await self._write(rtu_request)

            # Receive RTU response with proper framing
            rtu_response = await self._read_rtu_response()
            self.log.debug("RTU response: %r", rtu_response.hex())

            # Verify CRC
            if not verify_crc(rtu_response):
                self._crc_error_count += 1
                raise RuntimeError("Invalid CRC in RTU response")

        # Convert RTU response back to MBAP
        # RTU response format: unit_id(1) + function_code(1) + payload + crc(2)
        if len(rtu_response) < 3:
            raise RuntimeError("RTU response too short")

        response_unit_id = rtu_response[0]
        function_code = rtu_response[1]
        payload = rtu_response[2:-2]  # Exclude unit_id, function_code, and CRC

        # Verify unit_id matches request
        if response_unit_id != unit_id:
            self._protocol_error_count += 1
            raise RuntimeError(f"Unit ID mismatch: expected {unit_id}, got {response_unit_id}")

        # Check for exception response (function_code | 0x80)
        if function_code & 0x80:
            # Exception response: unit_id + (function_code | 0x80) + exception_code + crc
            if len(payload) != 1:
                raise RuntimeError(f"Invalid exception response length: {len(payload)}")
        else:
            # Normal response - validate based on function code
            if function_code in (0x03, 0x04):  # Read Holding/Input Registers
                if len(payload) < 1:
                    raise RuntimeError("Read response missing byte count")
                byte_count = payload[0]
                if len(payload) != 1 + byte_count:
                    raise RuntimeError(f"Read response length mismatch: expected {1 + byte_count}, got {len(payload)}")
            elif function_code in (0x06, 0x10):  # Write Single/Register
                if len(payload) != 4:
                    raise RuntimeError(f"Write response length mismatch: expected 4, got {len(payload)}")
            else:
                self.log.warning("Unsupported function code in response: 0x%02x", function_code)

        # Build MBAP response
        pdu_response = bytes([function_code]) + payload
        mbap_length = len(pdu_response)
        mbap_response = (
            transaction_id.to_bytes(2, "big") +
            b"\x00\x00" +  # Protocol ID = 0
            mbap_length.to_bytes(2, "big") +
            bytes([response_unit_id]) +
            pdu_response
        )

        return mbap_response

    async def _read_rtu_response(self) -> bytes:
        """Read RTU response with proper framing based on function code."""
        # Read address and function code first
        addr_func = await self._read_exactly(2)
        addr = addr_func[0]
        func_code = addr_func[1]

        if func_code & 0x80:
            # Exception response: addr(1) + func(1) + exc_code(1) + crc(2)
            payload = await self._read_exactly(1)
            crc = await self._read_exactly(2)
            return addr_func + payload + crc
        else:
            if func_code in (0x01, 0x02, 0x03, 0x04):  # Read Coils/Discrete Inputs/Holding/Input Registers
                # addr(1) + func(1) + byte_count(1) + data(byte_count) + crc(2)
                byte_count = await self._read_exactly(1)
                byte_count_value = byte_count[0]
                if byte_count_value > 250:  # Modbus RTU max payload ~250 bytes
                    raise RuntimeError(f"Invalid byte count: {byte_count_value}")
                data = await self._read_exactly(byte_count_value)
                crc = await self._read_exactly(2)
                return addr_func + byte_count + data + crc
            elif func_code in (0x05, 0x06):  # Write Single Coil/Register
                # addr(1) + func(1) + address(2) + value(2) + crc(2)
                rest = await self._read_exactly(4)
                crc = await self._read_exactly(2)
                return addr_func + rest + crc
            elif func_code in (0x0F, 0x10):  # Write Multiple Coils/Registers
                # addr(1) + func(1) + start_addr(2) + qty(2) + crc(2)
                rest = await self._read_exactly(4)
                crc = await self._read_exactly(2)
                return addr_func + rest + crc
            elif func_code == 0x17:  # Read/Write Multiple Registers
                # addr(1) + func(1) + byte_count(1) + read_data(byte_count) + crc(2)
                byte_count = await self._read_exactly(1)
                byte_count_value = byte_count[0]
                if byte_count_value > 250:
                    raise RuntimeError(f"Invalid byte count: {byte_count_value}")
                data = await self._read_exactly(byte_count_value)
                crc = await self._read_exactly(2)
                return addr_func + byte_count + data + crc
            else:
                # Unknown function code - close connection to prevent stream desynchronization
                self.log.error("Unsupported function code 0x%02x, closing connection to prevent desync", func_code)
                await self.close()
                raise RuntimeError(f"Unsupported Modbus function code: 0x{func_code:02x}")

    async def _read_exactly(self, n: int) -> bytes:
        """Read exactly n bytes with timeout."""
        if not self.reader:
            raise RuntimeError("Not connected to RTU-over-TCP server")
        try:
            return await asyncio.wait_for(
                self.reader.readexactly(n),
                timeout=self._timeout
            )
        except asyncio.TimeoutError:
            self._timeout_count += 1
            raise
    
    @property
    def metrics(self) -> dict:
        """Return connection metrics for monitoring."""
        return {
            "requests": self._request_count,
            "crc_errors": self._crc_error_count,
            "timeouts": self._timeout_count,
            "protocol_errors": self._protocol_error_count,
        }

    async def _write(self, data):
        """Write data to the connection."""
        self.log.debug("sending %r", data.hex())
        self.writer.write(data)
        await self.writer.drain()


class ModBus(Connection):
    def __init__(self, config):
        modbus = config["modbus"]
        url = parse_url(modbus["url"])
        bind = parse_url(config["listen"]["bind"])
        super().__init__(f"ModBus({url.hostname}:{url.port})", None, None)
        self.host = bind.hostname
        self.port = 502 if bind.port is None else bind.port
        self.modbus_host = url.hostname
        self.modbus_port = url.port
        self.timeout = modbus.get("timeout", None)
        self.connection_time = modbus.get("connection_time", 0)
        self.unit_id_remapping = config.get("unit_id_remapping") or {}
        self.server = None
        self.lock = asyncio.Lock()

        # Support for RTU-over-TCP
        self.is_rtu_over_tcp = url.scheme == "rtu+tcp"
        if self.is_rtu_over_tcp:
            self.rtu_client = RtuOverTcpClient(
                self.modbus_host,
                self.modbus_port,
                timeout=self.timeout or 5.0
            )

    @property
    def address(self):
        if self.server is not None:
            return self.server.sockets[0].getsockname()

    async def open(self):
        self.log.info("connecting to modbus...")
        self.reader, self.writer = await asyncio.open_connection(
            self.modbus_host, self.modbus_port
        )
        self.log.info("connected!")

    async def connect(self):
        if not self.opened:
            await asyncio.wait_for(self.open(), self.timeout)
            if self.connection_time > 0:
                self.log.info("delay after connect: %s", self.connection_time)
                await asyncio.sleep(self.connection_time)

    async def write_read(self, data, attempts=2):
        # Lock rationale:
        # - For TCP mode: protects connection state during reconnect
        # - For RTU mode: redundant with RtuOverTcpClient._lock but harmless
        # Both locks ensure only one request in-flight at a time per device
        async with self.lock:
            for i in range(attempts):
                try:
                    if self.is_rtu_over_tcp:
                        # Use RTU-over-TCP client
                        return await asyncio.wait_for(
                            self.rtu_client.write_read(data),
                            timeout=self.timeout
                        )
                    else:
                        # Use regular TCP Modbus
                        await self.connect()
                        coro = self._write_read(data)
                        return await asyncio.wait_for(coro, self.timeout)
                except (asyncio.TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError) as error:
                    # Network errors - retryable
                    self.log.warning(
                        "network error [%s/%s], will retry: %r", i + 1, attempts, error
                    )
                    if self.is_rtu_over_tcp:
                        await self.rtu_client.close()
                    else:
                        await self.close()
                except (ValueError, RuntimeError) as error:
                    # Protocol errors - not retryable, fail immediately
                    self.log.error(
                        "protocol error, aborting: %r", error
                    )
                    if self.is_rtu_over_tcp:
                        await self.rtu_client.close()
                    else:
                        await self.close()
                    raise
                except Exception as error:
                    # Unknown errors - log and retry
                    self.log.error(
                        "unexpected error [%s/%s]: %r", i + 1, attempts, error
                    )
                    if self.is_rtu_over_tcp:
                        await self.rtu_client.close()
                    else:
                        await self.close()

    async def _write_read(self, data):
        await self._write(data)
        return await self._read()

    def _transform_request(self, request):
        uid = request[6]
        new_uid = self.unit_id_remapping.setdefault(uid, uid)
        if uid != new_uid:
            request = bytearray(request)
            request[6] = new_uid
            self.log.debug("remapping unit ID %s to %s in request", uid, new_uid)
        return request

    def _transform_reply(self, reply):
        uid = reply[6]
        inverse_unit_id_map = {v: k for k, v in self.unit_id_remapping.items()}
        new_uid = inverse_unit_id_map.setdefault(uid, uid)
        if uid != new_uid:
            reply = bytearray(reply)
            reply[6] = new_uid
            self.log.debug("remapping unit ID %s to %s in reply", uid, new_uid)
        return reply

    async def handle_client(self, reader, writer):
        async with Client(reader, writer) as client:
            while True:
                request = await client.read()
                if not request:
                    break
                reply = await self.write_read(self._transform_request(request))
                if not reply:
                    break
                result = await client.write(self._transform_reply(reply))
                if not result:
                    break

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port, start_serving=True
        )

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.is_rtu_over_tcp:
            await self.rtu_client.close()
        await self.close()

    async def serve_forever(self):
        if self.server is None:
            await self.start()
        async with self.server:
            self.log.info("Ready to accept requests on %s:%d", self.host, self.port)
            await self.server.serve_forever()


def load_config(file_name):
    file_name = pathlib.Path(file_name)
    ext = file_name.suffix
    if ext.endswith("toml"):
        from toml import load
    elif ext.endswith("yml") or ext.endswith("yaml"):
        import yaml

        def load(fobj):
            return yaml.load(fobj, Loader=yaml.Loader)

    elif ext.endswith("json"):
        from json import load
    else:
        raise NotImplementedError
    with open(file_name) as fobj:
        return load(fobj)


def prepare_log(config):
    cfg = config.get("logging")
    if not cfg:
        cfg = DEFAULT_LOG_CONFIG
    if cfg:
        cfg.setdefault("version", 1)
        cfg.setdefault("disable_existing_loggers", False)
        logging.config.dictConfig(cfg)
    warnings.simplefilter("always", DeprecationWarning)
    logging.captureWarnings(True)
    return log


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="ModBus proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-c", "--config-file", default=None, type=str, help="config file"
    )
    parser.add_argument("-b", "--bind", default=None, type=str, help="listen address")
    parser.add_argument(
        "--modbus",
        default=None,
        type=str,
        help="modbus device address (ex: tcp://plc.acme.org:502)",
    )
    parser.add_argument(
        "--modbus-connection-time",
        type=float,
        default=0,
        help="delay after establishing connection with modbus before first request",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10,
        help="modbus connection and request timeout in seconds",
    )
    options = parser.parse_args(args=args)

    if not options.config_file and not options.modbus:
        parser.exit(1, "must give a config-file or/and a --modbus")
    return options


def create_config(args):
    if args.config_file is None:
        assert args.modbus
    config = load_config(args.config_file) if args.config_file else {}
    prepare_log(config)
    log.info("Starting...")
    devices = config.setdefault("devices", [])
    if args.modbus:
        listen = {"bind": ":502" if args.bind is None else args.bind}
        devices.append(
            {
                "modbus": {
                    "url": args.modbus,
                    "timeout": args.timeout,
                    "connection_time": args.modbus_connection_time,
                },
                "listen": listen,
            }
        )
    return config


def create_bridges(config):
    return [ModBus(cfg) for cfg in config["devices"]]


async def start_bridges(bridges):
    coros = [bridge.start() for bridge in bridges]
    await asyncio.gather(*coros)


async def run_bridges(bridges, ready=None):
    async with contextlib.AsyncExitStack() as stack:
        coros = [stack.enter_async_context(bridge) for bridge in bridges]
        await asyncio.gather(*coros)
        await start_bridges(bridges)
        if ready is not None:
            ready.set(bridges)
        coros = [bridge.serve_forever() for bridge in bridges]
        await asyncio.gather(*coros)


async def run(args=None, ready=None):
    args = parse_args(args)
    config = create_config(args)
    bridges = create_bridges(config)
    await run_bridges(bridges, ready=ready)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.warning("Ctrl-C pressed. Bailing out!")


if __name__ == "__main__":
    main()
