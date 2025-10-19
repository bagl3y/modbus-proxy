# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

"""Tests for RTU-over-TCP functionality."""

import asyncio
import pytest

from modbus_proxy import RtuOverTcpClient, modbus_crc, append_crc, verify_crc


class MockRtuServer:
    """Mock RTU-over-TCP server for testing."""

    def __init__(self, host='127.0.0.1', port=0):
        self.host = host
        self.port = port
        self.server = None
        self.responses = {}

    async def start(self):
        """Start the mock server."""
        self.server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        """Stop the mock server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def set_response(self, request_pattern: bytes, response: bytes):
        """Set response for a specific request pattern."""
        self.responses[request_pattern] = response

    async def _handle_client(self, reader, writer):
        """Handle client connection."""
        try:
            while True:
                # Read RTU request (without knowing the length, read until we have enough)
                data = await reader.read(1024)
                if not data:
                    break

                # Find matching response
                response = None
                for pattern, resp in self.responses.items():
                    if data.startswith(pattern):
                        response = resp
                        break

                if response:
                    writer.write(response)
                    await writer.drain()
                else:
                    # Default: echo back with error
                    writer.write(b'\x01\x83\x01')  # Unit 1, exception 1
                    await writer.drain()
        except asyncio.CancelledError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_crc_functions():
    """Test CRC functions with comprehensive test vectors."""
    # Test vectors verified with Modbus RTU standard
    # Format: (data_without_crc, expected_crc_value)
    test_cases = [
        (b'\x01\x03\x00\x01\x00\x04', 0xC915),  # Read 4 holding registers from address 1
        (b'\x01\x06\x00\x01\x00\x02', 0xCB59),  # Write single register
        (b'\x01\x10\x00\x01\x00\x02\x04\x00\x01\x00\x02', 0x62E2),  # Write multiple registers
    ]

    for data, expected_crc in test_cases:
        assert modbus_crc(data) == expected_crc
        frame = append_crc(data)
        assert verify_crc(frame) is True
        assert frame == data + bytes([expected_crc & 0xFF, (expected_crc >> 8) & 0xFF])


@pytest.mark.asyncio
async def test_rtu_over_tcp_read_holding_registers():
    """Test RTU-over-TCP read holding registers (function 0x03)."""
    # Mock server responses
    server = MockRtuServer()

    # Request: Unit 1, Read Holding Registers, start=1, quantity=4
    # RTU: 01 03 00 01 00 04 CRC
    request_rtu = append_crc(b'\x01\x03\x00\x01\x00\x04')

    # Response: Unit 1, Function 3, Byte count 8, 4 registers, CRC
    response_rtu = append_crc(b'\x01\x03\x08\x00\x01\x00\x02\x00\x03\x00\x04')

    server.set_response(request_rtu, response_rtu)

    await server.start()
    try:
        # Create RTU-over-TCP client
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # MBAP request for the same operation
        mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x04'

        # Should get back proper MBAP response
        mbap_response = await client.write_read(mbap_request)

        # Verify MBAP response structure
        assert len(mbap_response) == 13  # 7 MBAP + 6 PDU
        assert mbap_response[0:2] == b'\x12\x34'  # Transaction ID preserved
        assert mbap_response[2:4] == b'\x00\x00'  # Protocol ID
        assert mbap_response[4:6] == b'\x00\x06'  # Length = 6 (unit + pdu)
        assert mbap_response[6] == 0x01  # Unit ID
        assert mbap_response[7] == 0x03  # Function code
        assert mbap_response[8] == 0x08  # Byte count
        assert mbap_response[9:13] == b'\x00\x01\x00\x02\x00\x03\x00\x04'  # 4 registers

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_write_single_register():
    """Test RTU-over-TCP write single register (function 0x06)."""
    server = MockRtuServer()

    # Request: Unit 1, Write Single Register, address=1, value=0x0002
    # RTU: 01 06 00 01 00 02 CRC
    request_rtu = append_crc(b'\x01\x06\x00\x01\x00\x02')

    # Response: Unit 1, Function 6, address=1, value=0x0002, CRC
    response_rtu = append_crc(b'\x01\x06\x00\x01\x00\x02')

    server.set_response(request_rtu, response_rtu)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # MBAP request
        mbap_request = b'\x56\x78\x00\x00\x00\x06\x01\x06\x00\x01\x00\x02'

        mbap_response = await client.write_read(mbap_request)

        # Verify response
        assert len(mbap_response) == 12  # 7 MBAP + 5 PDU (func + 4 bytes data)
        assert mbap_response[0:2] == b'\x56\x78'  # Transaction ID
        assert mbap_response[6] == 0x01  # Unit ID
        assert mbap_response[7] == 0x06  # Function code
        assert mbap_response[8:12] == b'\x00\x01\x00\x02'  # Echo of address and value

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_exception_response():
    """Test RTU-over-TCP exception response handling."""
    server = MockRtuServer()

    # Request: Unit 1, Read Holding Registers, invalid address
    request_rtu = append_crc(b'\x01\x03\xFF\xFF\x00\x01')

    # Exception response: Unit 1, Function 0x83 (0x03 | 0x80), Exception code 0x02 (illegal data address)
    response_rtu = append_crc(b'\x01\x83\x02')

    server.set_response(request_rtu, response_rtu)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # MBAP request
        mbap_request = b'\x9A\xBC\x00\x00\x00\x06\x01\x03\xFF\xFF\x00\x01'

        mbap_response = await client.write_read(mbap_request)

        # Verify exception response
        assert len(mbap_response) == 9  # 7 MBAP + 2 PDU (func + exc code)
        assert mbap_response[0:2] == b'\x9A\xBC'  # Transaction ID
        assert mbap_response[6] == 0x01  # Unit ID
        assert mbap_response[7] == 0x83  # Exception function code
        assert mbap_response[8] == 0x02  # Exception code

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_timeout():
    """Test RTU-over-TCP timeout handling."""
    server = MockRtuServer()

    # Set up a slow response
    async def slow_handler(reader, writer):
        # Read request but don't respond immediately
        data = await reader.read(1024)
        if data:
            await asyncio.sleep(3)  # Sleep longer than client timeout
            writer.write(append_crc(b'\x01\x03\x02\x00\x01'))
            await writer.drain()

    server.server = await asyncio.start_server(slow_handler, server.host, server.port)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=1.0)  # 1 second timeout

        mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x01'

        # Should timeout
        with pytest.raises(asyncio.TimeoutError):
            await client.write_read(mbap_request)

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_crc_error():
    """Test RTU-over-TCP CRC error handling."""
    server = MockRtuServer()

    # Set up server to return frame with bad CRC
    async def bad_crc_handler(reader, writer):
        data = await reader.read(1024)
        if data:
            # Send response with wrong CRC
            writer.write(b'\x01\x03\x02\x00\x01\xFF\xFF')  # Wrong CRC
            await writer.drain()

    server.server = await asyncio.start_server(bad_crc_handler, server.host, server.port)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x01'

        # Should raise RuntimeError due to bad CRC
        with pytest.raises(RuntimeError, match="Invalid CRC"):
            await client.write_read(mbap_request)

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_concurrent_requests():
    """Test RTU-over-TCP with concurrent requests (serialization)."""
    server = MockRtuServer()

    # Set up server to handle multiple requests but respond slowly
    request_count = 0

    async def slow_sequential_handler(reader, writer):
        nonlocal request_count
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break

                request_count += 1
                request_num = request_count

                # Respond slowly to test serialization
                await asyncio.sleep(0.1)

                # Respond with request number in data
                if data.startswith(append_crc(b'\x01\x03\x00\x01\x00\x01')):
                    # Read 1 register starting at 1
                    response = append_crc(b'\x01\x03\x02\x00' + request_num.to_bytes(2, 'big'))
                else:
                    response = append_crc(b'\x01\x83\x01')  # Exception

                writer.write(response)
                await writer.drain()

        except asyncio.CancelledError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server.server = await asyncio.start_server(slow_sequential_handler, server.host, server.port)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # Send multiple concurrent requests
        mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x01'

        # Create multiple concurrent requests
        tasks = []
        for i in range(5):
            task = asyncio.create_task(client.write_read(mbap_request))
            tasks.append(task)

        # All requests should succeed and be serialized
        responses = await asyncio.gather(*tasks)

        # Verify all responses are valid
        for i, response in enumerate(responses):
            assert len(response) == 10  # 7 MBAP + 3 PDU
            assert response[0:2] == b'\x12\x34'  # Same transaction ID for all
            assert response[6] == 0x01  # Unit ID
            assert response[7] == 0x03  # Function code
            assert response[8] == 0x02  # Byte count
            # Data should be sequential (1, 2, 3, 4, 5)
            expected_value = (i + 1)
            assert response[9:11] == expected_value.to_bytes(2, 'big')

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_unit_id_mismatch():
    """Test that unit_id mismatch is detected and raises error."""
    server = MockRtuServer()

    # Request for unit 1
    request_rtu = append_crc(b'\x01\x03\x00\x01\x00\x01')

    # Response from unit 2 (mismatch!)
    response_rtu = append_crc(b'\x02\x03\x02\x00\x01')

    server.set_response(request_rtu, response_rtu)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # MBAP request for unit 1
        mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x01'

        # Should raise RuntimeError due to unit_id mismatch
        with pytest.raises(RuntimeError, match="Unit ID mismatch"):
            await client.write_read(mbap_request)

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_read_coils():
    """Test RTU-over-TCP read coils (function 0x01)."""
    server = MockRtuServer()

    # Request: Unit 1, Read Coils, start=0, quantity=8
    request_rtu = append_crc(b'\x01\x01\x00\x00\x00\x08')

    # Response: Unit 1, Function 1, Byte count 1, coils data (0xFF = all on)
    response_rtu = append_crc(b'\x01\x01\x01\xFF')

    server.set_response(request_rtu, response_rtu)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # MBAP request
        mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x01\x00\x00\x00\x08'

        mbap_response = await client.write_read(mbap_request)

        # Verify response
        assert mbap_response[7] == 0x01  # Function code
        assert mbap_response[8] == 0x01  # Byte count
        assert mbap_response[9] == 0xFF  # Coils data

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_write_single_coil():
    """Test RTU-over-TCP write single coil (function 0x05)."""
    server = MockRtuServer()

    # Request: Unit 1, Write Single Coil, address=10, value=0xFF00 (ON)
    request_rtu = append_crc(b'\x01\x05\x00\x0A\xFF\x00')

    # Response: Echo of request
    response_rtu = append_crc(b'\x01\x05\x00\x0A\xFF\x00')

    server.set_response(request_rtu, response_rtu)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # MBAP request
        mbap_request = b'\x56\x78\x00\x00\x00\x06\x01\x05\x00\x0A\xFF\x00'

        mbap_response = await client.write_read(mbap_request)

        # Verify response
        assert mbap_response[7] == 0x05  # Function code
        assert mbap_response[8:12] == b'\x00\x0A\xFF\x00'

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_write_multiple_coils():
    """Test RTU-over-TCP write multiple coils (function 0x0F)."""
    server = MockRtuServer()

    # Request: Unit 1, Write Multiple Coils, start=10, quantity=8, byte_count=1, data=0xFF
    request_rtu = append_crc(b'\x01\x0F\x00\x0A\x00\x08\x01\xFF')

    # Response: Unit 1, Function 0x0F, start=10, quantity=8
    response_rtu = append_crc(b'\x01\x0F\x00\x0A\x00\x08')

    server.set_response(request_rtu, response_rtu)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # MBAP request
        mbap_request = b'\x56\x78\x00\x00\x00\x08\x01\x0F\x00\x0A\x00\x08\x01\xFF'

        mbap_response = await client.write_read(mbap_request)

        # Verify response
        assert mbap_response[7] == 0x0F  # Function code
        assert mbap_response[8:12] == b'\x00\x0A\x00\x08'

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_invalid_byte_count():
    """Test that invalid byte count (>250) is rejected."""
    server = MockRtuServer()

    # Set up server to return invalid byte count
    async def invalid_handler(reader, writer):
        data = await reader.read(1024)
        if data:
            # Send response with byte_count = 255 (too large)
            writer.write(b'\x01\x03\xFF')  # addr + func + byte_count=255
            await writer.drain()
            # Don't send the rest to trigger error

    server.server = await asyncio.start_server(invalid_handler, server.host, server.port)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x01'

        # Should raise RuntimeError due to invalid byte count
        with pytest.raises(RuntimeError, match="Invalid byte count"):
            await client.write_read(mbap_request)

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_unsupported_function():
    """Test that unsupported function codes close connection and raise error."""
    server = MockRtuServer()

    # Request with standard function
    request_rtu = append_crc(b'\x01\x03\x00\x01\x00\x01')

    # Response with unsupported function code 0x42
    response_rtu = b'\x01\x42\x00\x01'  # No CRC yet, will be sent by handler

    async def unsupported_func_handler(reader, writer):
        data = await reader.read(1024)
        if data:
            # Send response with unsupported function
            writer.write(b'\x01\x42')  # addr + unsupported func
            await writer.drain()
            # Connection should be closed by client

    server.server = await asyncio.start_server(unsupported_func_handler, server.host, server.port)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x01'

        # Should raise RuntimeError due to unsupported function
        with pytest.raises(RuntimeError, match="Unsupported Modbus function code"):
            await client.write_read(mbap_request)

        # Connection should be closed
        assert not client.opened

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_read_write_multiple_registers():
    """Test RTU-over-TCP read/write multiple registers (function 0x17)."""
    server = MockRtuServer()

    # Request: Unit 1, Function 0x17, read_addr=0, read_qty=2, write_addr=10, write_qty=2, byte_count=4, data
    # 01 17 00 00 00 02 00 0A 00 02 04 12 34 56 78 CRC
    request_rtu = append_crc(b'\x01\x17\x00\x00\x00\x02\x00\x0A\x00\x02\x04\x12\x34\x56\x78')

    # Response: Unit 1, Function 0x17, Byte count 4, read data (2 registers)
    response_rtu = append_crc(b'\x01\x17\x04\xAA\xBB\xCC\xDD')

    server.set_response(request_rtu, response_rtu)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # MBAP request
        mbap_request = b'\x12\x34\x00\x00\x00\x0F\x01\x17\x00\x00\x00\x02\x00\x0A\x00\x02\x04\x12\x34\x56\x78'

        mbap_response = await client.write_read(mbap_request)

        # Verify response
        assert mbap_response[7] == 0x17  # Function code
        assert mbap_response[8] == 0x04  # Byte count
        assert mbap_response[9:13] == b'\xAA\xBB\xCC\xDD'  # Read data

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_metrics():
    """Test that metrics are properly tracked."""
    server = MockRtuServer()

    # Set up some responses
    request1 = append_crc(b'\x01\x03\x00\x01\x00\x01')
    response1 = append_crc(b'\x01\x03\x02\x00\x01')
    server.set_response(request1, response1)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # Initial metrics
        metrics = client.metrics
        assert metrics['requests'] == 0
        assert metrics['crc_errors'] == 0
        assert metrics['timeouts'] == 0
        assert metrics['protocol_errors'] == 0

        # Make a successful request
        mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x01'
        await client.write_read(mbap_request)

        # Check metrics updated
        metrics = client.metrics
        assert metrics['requests'] == 1
        assert metrics['crc_errors'] == 0
        assert metrics['timeouts'] == 0

        # Make another request
        await client.write_read(mbap_request)
        metrics = client.metrics
        assert metrics['requests'] == 2

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rtu_over_tcp_connection_reuse():
    """Test that RTU-over-TCP client reuses connections properly."""
    server = MockRtuServer()

    connection_count = 0

    async def connection_tracking_handler(reader, writer):
        nonlocal connection_count
        connection_count += 1
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break

                # Simple echo response
                if len(data) >= 4 and data[1] == 0x03:
                    # Read request - respond with dummy data
                    response = append_crc(b'\x01\x03\x02\x12\x34')
                else:
                    response = append_crc(b'\x01\x83\x01')

                writer.write(response)
                await writer.drain()

        except asyncio.CancelledError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server.server = await asyncio.start_server(connection_tracking_handler, server.host, server.port)

    await server.start()
    try:
        client = RtuOverTcpClient(server.host, server.port, timeout=2.0)

        # Make multiple requests - should reuse the same connection
        initial_connections = connection_count

        for i in range(3):
            mbap_request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x01'
            response = await client.write_read(mbap_request)
            assert len(response) == 10

        # Should have created only one connection (or very few due to retries)
        assert connection_count <= 2  # Allow for 1-2 connections max

    finally:
        await server.stop()
