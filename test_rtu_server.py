#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple RTU-over-TCP server for testing the modbus-proxy RTU-over-TCP functionality.
"""

import asyncio
import argparse
from modbus_proxy import modbus_crc, append_crc


class SimpleRtuTcpServer:
    """Simple RTU-over-TCP server for testing."""

    def __init__(self, host='127.0.0.1', port=8899):
        self.host = host
        self.port = port
        self.server = None
        # Simple register storage (unit_id -> register_index -> value)
        self.registers = {}

    async def start(self):
        """Start the RTU-over-TCP server."""
        self.server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        print(f"RTU-over-TCP server listening on {self.host}:{self.port}")

    async def stop(self):
        """Stop the server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            print("RTU-over-TCP server stopped")

    async def _handle_client(self, reader, writer):
        """Handle a client connection."""
        peer = writer.get_extra_info('peername')
        print(f"New RTU-over-TCP client connected: {peer}")

        try:
            while True:
                # Read RTU frame (we don't know the length, so read chunks)
                data = await reader.read(1024)
                if not data:
                    break

                # Process the RTU frame
                response = self._process_rtu_frame(data)
                if response:
                    writer.write(response)
                    await writer.drain()
                    print(f"Sent RTU response: {response.hex()}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Error handling client {peer}: {e}")
        finally:
            writer.close()
            await writer.wait_closed()
            print(f"RTU-over-TCP client disconnected: {peer}")

    def _process_rtu_frame(self, frame):
        """Process an RTU frame and return response."""
        if len(frame) < 4:
            print(f"Frame too short: {frame.hex()}")
            return None

        # Verify CRC
        if not self._verify_crc(frame):
            print(f"CRC error in frame: {frame.hex()}")
            return None

        unit_id = frame[0]
        function_code = frame[1]
        payload = frame[2:-2]  # Exclude unit_id, function_code, crc

        print(f"RTU request: unit={unit_id}, func={function_code:02x}, payload={payload.hex()}")

        # Initialize registers for this unit if needed
        if unit_id not in self.registers:
            self.registers[unit_id] = {i: i + 1 for i in range(100)}  # Default values

        # Handle different function codes
        if function_code == 0x03:  # Read Holding Registers
            return self._handle_read_registers(unit_id, payload)
        elif function_code == 0x06:  # Write Single Register
            return self._handle_write_single_register(unit_id, payload)
        elif function_code == 0x10:  # Write Multiple Registers
            return self._handle_write_multiple_registers(unit_id, payload)
        else:
            # Unknown function - return exception
            print(f"Unknown function code: {function_code"02x"}")
            return append_crc(bytes([unit_id, function_code | 0x80, 0x01]))

    def _verify_crc(self, frame):
        """Verify CRC of RTU frame."""
        if len(frame) < 2:
            return False
        data = frame[:-2]
        expected_crc = int.from_bytes(frame[-2:], "little")
        return modbus_crc(data) == expected_crc

    def _handle_read_registers(self, unit_id, payload):
        """Handle Read Holding Registers (0x03)."""
        if len(payload) != 4:
            return append_crc(bytes([unit_id, 0x03 | 0x80, 0x02]))  # Illegal data address

        start_addr = int.from_bytes(payload[0:2], "big")
        quantity = int.from_bytes(payload[2:4], "big")

        if start_addr + quantity > 100:
            return append_crc(bytes([unit_id, 0x03 | 0x80, 0x02]))  # Illegal data address

        # Read registers
        values = []
        for i in range(quantity):
            reg_addr = start_addr + i
            value = self.registers[unit_id].get(reg_addr, 0)
            values.extend(value.to_bytes(2, "big"))

        byte_count = len(values)
        response_payload = bytes([byte_count]) + bytes(values)
        response = bytes([unit_id, 0x03]) + response_payload

        return append_crc(response)

    def _handle_write_single_register(self, unit_id, payload):
        """Handle Write Single Register (0x06)."""
        if len(payload) != 4:
            return append_crc(bytes([unit_id, 0x06 | 0x80, 0x02]))  # Illegal data address

        reg_addr = int.from_bytes(payload[0:2], "big")
        value = int.from_bytes(payload[2:4], "big")

        self.registers[unit_id][reg_addr] = value
        print(f"Written register {reg_addr} = {value}")

        # Echo back
        response = bytes([unit_id, 0x06]) + payload
        return append_crc(response)

    def _handle_write_multiple_registers(self, unit_id, payload):
        """Handle Write Multiple Registers (0x10)."""
        if len(payload) < 5:
            return append_crc(bytes([unit_id, 0x10 | 0x80, 0x02]))  # Illegal data address

        start_addr = int.from_bytes(payload[0:2], "big")
        quantity = int.from_bytes(payload[2:4], "big")
        byte_count = payload[4]

        if len(payload) != 5 + byte_count:
            return append_crc(bytes([unit_id, 0x10 | 0x80, 0x02]))  # Illegal data address

        # Write registers
        values = payload[5:]
        for i in range(quantity):
            value = int.from_bytes(values[i*2:(i+1)*2], "big")
            reg_addr = start_addr + i
            self.registers[unit_id][reg_addr] = value

        print(f"Written {quantity} registers starting at {start_addr}")

        # Echo back start address and quantity
        response_payload = payload[0:4]
        response = bytes([unit_id, 0x10]) + response_payload
        return append_crc(response)


async def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Simple RTU-over-TCP server for testing")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8899, help="Port to bind to")

    args = parser.parse_args()

    server = SimpleRtuTcpServer(args.host, args.port)

    try:
        await server.start()

        # Run until interrupted
        print("Press Ctrl+C to stop the server")
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
