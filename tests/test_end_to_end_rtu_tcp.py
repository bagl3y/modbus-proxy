# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

"""End-to-end tests for RTU-over-TCP functionality."""

import asyncio
import pytest

from modbus_proxy import run, modbus_crc, append_crc
from .test_modbus_proxy import Ready


class MockRtuTcpServer:
    """Mock RTU-over-TCP server for end-to-end testing."""

    def __init__(self, host='127.0.0.1', port=0):
        self.host = host
        self.port = port
        self.server = None

    async def start(self):
        """Start the mock RTU-over-TCP server."""
        self.server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        """Stop the mock server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle_client(self, reader, writer):
        """Handle RTU-over-TCP client connection."""
        try:
            while True:
                # Read RTU frame (read until we get a complete frame)
                data = await reader.read(1024)
                if not data:
                    break

                # Parse RTU frame: unit_id(1) + function(1) + payload + crc(2)
                if len(data) < 4:  # Minimum: unit + func + crc
                    continue

                # Verify CRC
                if not self._verify_crc(data):
                    continue

                unit_id = data[0]
                function_code = data[1]
                payload = data[2:-2]  # Exclude unit_id, function_code, crc

                # Generate response based on function code
                response_data = self._generate_response(unit_id, function_code, payload)
                if response_data:
                    writer.write(response_data)
                    await writer.drain()

        except asyncio.CancelledError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    def _verify_crc(self, frame):
        """Verify CRC of RTU frame."""
        if len(frame) < 2:
            return False
        data = frame[:-2]
        expected_crc = int.from_bytes(frame[-2:], "little")
        return modbus_crc(data) == expected_crc

    def _generate_response(self, unit_id, function_code, payload):
        """Generate RTU response based on function code."""
        if function_code == 0x03:  # Read Holding Registers
            if len(payload) != 4:  # start_addr(2) + quantity(2)
                return None

            start_addr = int.from_bytes(payload[0:2], "big")
            quantity = int.from_bytes(payload[2:4], "big")

            # Generate dummy register values
            registers = []
            for i in range(quantity):
                value = (start_addr + i) * 10 + 1  # Simple pattern
                registers.extend(value.to_bytes(2, "big"))

            byte_count = len(registers)
            response_payload = bytes([byte_count]) + bytes(registers)
            response = bytes([unit_id, function_code]) + response_payload

        elif function_code == 0x06:  # Write Single Register
            if len(payload) != 4:  # addr(2) + value(2)
                return None

            # Echo back the same data
            response = bytes([unit_id, function_code]) + payload

        elif function_code == 0x10:  # Write Multiple Registers
            if len(payload) < 5:  # start_addr(2) + quantity(2) + byte_count(1)
                return None

            start_addr = int.from_bytes(payload[0:2], "big")
            quantity = int.from_bytes(payload[2:4], "big")
            byte_count = payload[4]

            if len(payload) != 5 + byte_count:
                return None

            # Echo back address and quantity
            response_payload = payload[0:4]
            response = bytes([unit_id, function_code]) + response_payload

        else:
            # Unknown function or error - return exception
            return bytes([unit_id, function_code | 0x80, 0x01])  # Illegal function

        return append_crc(response)


@pytest.mark.asyncio
async def test_end_to_end_rtu_tcp_proxy():
    """Test complete RTU-over-TCP proxy functionality."""
    # Start mock RTU-over-TCP server
    rtu_server = MockRtuTcpServer()
    await rtu_server.start()

    try:
        # Start proxy with RTU-over-TCP upstream
        proxy_args = [
            "--modbus", f"rtu+tcp://{rtu_server.host}:{rtu_server.port}",
            "--bind", "127.0.0.1:0",
            "--timeout", "5"
        ]

        # Ready is imported at the top
        ready = Ready()

        # Start proxy in background
        proxy_task = asyncio.create_task(run(proxy_args, ready))

        try:
            # Wait for proxy to be ready
            await asyncio.wait_for(ready.wait(), timeout=10.0)
            proxy_address = ready.data[0].address

            # Connect to proxy as a Modbus TCP client
            reader, writer = await asyncio.open_connection(*proxy_address[:2])

            # Test Read Holding Registers (0x03)
            # MBAP: tid(2) + pid(2) + len(2) + unit(1) + func(1) + start(2) + qty(2)
            request = b'\x12\x34\x00\x00\x00\x06\x01\x03\x00\x01\x00\x02'
            writer.write(request)
            await writer.drain()

            # Read response
            response = await reader.readexactly(13)  # Expected length

            # Verify response structure
            assert response[0:2] == b'\x12\x34'  # Transaction ID preserved
            assert response[2:4] == b'\x00\x00'  # Protocol ID
            assert response[4:6] == b'\x00\x06'  # Length
            assert response[6] == 0x01  # Unit ID
            assert response[7] == 0x03  # Function code
            assert response[8] == 0x04  # Byte count (2 registers * 2 bytes)
            assert response[9:13] == b'\x00\x0A\x00\x0B'  # Register values (10, 11 in hex)

            writer.close()
            await writer.wait_closed()

            # Test Write Single Register (0x06)
            reader2, writer2 = await asyncio.open_connection(*proxy_address[:2])

            # Write register at address 0x0001 with value 0x1234
            write_request = b'\x56\x78\x00\x00\x00\x06\x01\x06\x00\x01\x12\x34'
            writer2.write(write_request)
            await writer2.drain()

            # Read response
            write_response = await reader2.readexactly(12)  # Expected length

            # Verify write response
            assert write_response[0:2] == b'\x56\x78'  # Transaction ID preserved
            assert write_response[6] == 0x01  # Unit ID
            assert write_response[7] == 0x06  # Function code
            assert write_response[8:12] == b'\x00\x01\x12\x34'  # Echo of address and value

            writer2.close()
            await writer2.wait_closed()

        finally:
            # Stop proxy
            for bridge in ready.data:
                await bridge.stop()
            try:
                await proxy_task
            except asyncio.CancelledError:
                pass

    finally:
        await rtu_server.stop()


@pytest.mark.asyncio
async def test_end_to_end_rtu_tcp_concurrent_clients():
    """Test RTU-over-TCP proxy with concurrent clients."""
    rtu_server = MockRtuTcpServer()
    await rtu_server.start()

    try:
        # Start proxy
        proxy_args = [
            "--modbus", f"rtu+tcp://{rtu_server.host}:{rtu_server.port}",
            "--bind", "127.0.0.1:0",
            "--timeout", "5"
        ]

        # Ready is imported at the top
        ready = Ready()
        proxy_task = asyncio.create_task(run(proxy_args, ready))

        try:
            await asyncio.wait_for(ready.wait(), timeout=10.0)
            proxy_address = ready.data[0].address

            # Create multiple concurrent clients
            async def client_task(client_id):
                reader, writer = await asyncio.open_connection(*proxy_address[:2])

                # Each client makes multiple requests
                for i in range(3):
                    # Read different register ranges
                    start_addr = client_id * 10 + i
                    request = b'\x12\x34\x00\x00\x00\x06\x01\x03' + \
                             start_addr.to_bytes(2, 'big') + b'\x02'
                    writer.write(request)
                    await writer.drain()

                    response = await reader.readexactly(13)
                    assert response[7] == 0x03  # Function code

                writer.close()
                await writer.wait_closed()

            # Run 3 concurrent clients
            tasks = [client_task(i) for i in range(3)]
            await asyncio.gather(*tasks)

        finally:
            # Stop proxy
            for bridge in ready.data:
                await bridge.stop()
            try:
                await proxy_task
            except asyncio.CancelledError:
                pass

    finally:
        await rtu_server.stop()


@pytest.mark.asyncio
async def test_end_to_end_rtu_tcp_exception_handling():
    """Test RTU-over-TCP proxy exception handling."""
    rtu_server = MockRtuTcpServer()
    await rtu_server.start()

    try:
        # Start proxy
        proxy_args = [
            "--modbus", f"rtu+tcp://{rtu_server.host}:{rtu_server.port}",
            "--bind", "127.0.0.1:0",
            "--timeout", "5"
        ]

        # Ready is imported at the top
        ready = Ready()
        proxy_task = asyncio.create_task(run(proxy_args, ready))

        try:
            await asyncio.wait_for(ready.wait(), timeout=10.0)
            proxy_address = ready.data[0].address

            # Connect and send invalid request (function code 0xFF)
            reader, writer = await asyncio.open_connection(*proxy_address[:2])

            # Invalid function code
            invalid_request = b'\x12\x34\x00\x00\x00\x03\x01\xFF\x00\x01'
            writer.write(invalid_request)
            await writer.drain()

            # Should get exception response
            response = await reader.readexactly(9)

            # Verify exception response
            assert response[0:2] == b'\x12\x34'  # Transaction ID
            assert response[6] == 0x01  # Unit ID
            assert response[7] == 0xFF  # Original function code | 0x80
            assert response[8] == 0x01  # Exception code (illegal function)

            writer.close()
            await writer.wait_closed()

        finally:
            # Stop proxy
            for bridge in ready.data:
                await bridge.stop()
            try:
                await proxy_task
            except asyncio.CancelledError:
                pass

    finally:
        await rtu_server.stop()
