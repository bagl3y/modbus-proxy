#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Integration test for RTU-over-TCP functionality.
"""

import asyncio
import sys
sys.path.insert(0, 'src')

from modbus_proxy import RtuOverTcpClient


async def test_rtu_over_tcp_integration():
    """Test RTU-over-TCP integration with the test server."""
    # Start test server in background
    server_process = await asyncio.create_subprocess_exec(
        sys.executable, 'test_rtu_server.py', '--port', '8899',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    # Wait a bit for server to start
    await asyncio.sleep(2)

    try:
        # Create RTU-over-TCP client
        client = RtuOverTcpClient('127.0.0.1', 8899, timeout=2.0)

        # Test read holding registers
        print("Testing read holding registers...")
        mbap_request = b'\x12\x34\x00\x00\x00\x05\x01\x03\x00\x01\x00\x02'  # PDU is 5 bytes
        mbap_response = await client.write_read(mbap_request)
        print(f'MBAP response: {mbap_response.hex()}')

        # Verify response structure
        assert len(mbap_response) == 13  # 7 MBAP + 6 PDU
        assert mbap_response[0:2] == b'\x12\x34'  # Transaction ID preserved
        assert mbap_response[6] == 0x01  # Unit ID
        assert mbap_response[7] == 0x03  # Function code
        assert mbap_response[8] == 0x04  # Byte count
        print("✓ Read holding registers test passed")

        # Test write single register
        print("Testing write single register...")
        write_request = b'\x56\x78\x00\x00\x00\x05\x01\x06\x00\x05\x12\x34'  # PDU is 5 bytes
        write_response = await client.write_read(write_request)
        print(f'Write response: {write_response.hex()}')

        # Verify write response
        assert len(write_response) == 12  # 7 MBAP + 5 PDU
        assert write_response[0:2] == b'\x56\x78'  # Transaction ID preserved
        assert write_response[6] == 0x01  # Unit ID
        assert write_response[7] == 0x06  # Function code
        print("✓ Write single register test passed")

        # Test exception handling
        print("Testing exception handling...")
        invalid_request = b'\x9A\xBC\x00\x00\x00\x03\x01\xFF\x00\x01'  # PDU is 3 bytes
        exception_response = await client.write_read(invalid_request)
        print(f'Exception response: {exception_response.hex()}')

        # Verify exception response
        assert len(exception_response) == 9  # 7 MBAP + 2 PDU
        assert exception_response[7] == 0xFF  # Exception function code
        assert exception_response[8] == 0x01  # Exception code (illegal function)
        print("✓ Exception handling test passed")

        print("All RTU-over-TCP integration tests passed!")

    finally:
        # Stop the server
        server_process.terminate()
        try:
            await asyncio.wait_for(server_process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            server_process.kill()
            await server_process.wait()


if __name__ == "__main__":
    asyncio.run(test_rtu_over_tcp_integration())
