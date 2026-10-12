"""Only initialize, list loaded IDs, and supported thread/delete are used."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time

from .errors import ProtocolError


class StdioClient:
    def __init__(self, runtime, settings, timeout=180):
        self.timeout = timeout
        command = [
            str(runtime.binary),
            "app-server",
            "--listen",
            "stdio://",
            "-c",
            "sqlite_home=" + json.dumps(str(settings.sqlite_home)),
            "--disable",
            "local_thread_store_compression",
            "--disable",
            "background_paginated_rollout_migration",
        ]
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(settings.home)
        environment["CODEX_SQLITE_HOME"] = str(settings.sqlite_home)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        self.selector = None
        try:
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
        except BaseException:
            self.close()
            raise
        self.buffer = b""
        self.sequence = 0

    @property
    def pid(self):
        return self.process.pid

    def send(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        wanted = self.sequence
        self.send({"id": wanted, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise ProtocolError("Owned server exited before acknowledging a request.")
                self.buffer += chunk
                if len(self.buffer) > 1024**2:
                    raise ProtocolError("Oversized protocol frame.")
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                response = json.loads(line)
            except ValueError:
                raise ProtocolError("Malformed protocol frame.") from None
            if not isinstance(response, dict):
                raise ProtocolError("Protocol response must be an object.")
            if "id" in response and "method" in response:
                raise ProtocolError("Unexpected server request; never auto-approve.")
            if "id" in response and type(response["id"]) is not int:
                raise ProtocolError("Nonconforming response ID.")
            if response.get("id") != wanted:
                continue
            if "error" in response:
                # Do not print backend messages, which may contain private paths/text.
                raise ProtocolError(f"Supported {method} refused the request.")
            if "result" not in response:
                raise ProtocolError("Missing acknowledgement.")
            return response["result"]
        raise ProtocolError("Protocol timeout; deletion outcome may require review.")

    def initialize(self):
        initialized = self.request(
            "initialize",
            {
                "clientInfo": {"name": "codex_cleanup", "version": "0.1.0a1"},
                "capabilities": {"experimentalApi": True, "explicitGatewayOauth": True},
            },
        )
        if not isinstance(initialized, dict):
            raise ProtocolError("Invalid initialization response.")
        self.send({"method": "initialized", "params": {}})
        loaded = self.request("thread/loaded/list", {"limit": 1000})
        if not isinstance(loaded, dict) or loaded.get("data") != [] or loaded.get("nextCursor"):
            raise ProtocolError("Owned server has loaded threads; cleanup blocked.")

    def delete(self, tid):
        return self.request("thread/delete", {"threadId": tid})

    def close(self):
        try:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
            deadline = time.monotonic() + 10
            while self.selector and self.process.poll() is None and time.monotonic() < deadline:
                if self.selector.select(0.1):
                    if not os.read(self.process.stdout.fileno(), 65536):
                        break
            try:
                self.process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                # Only our own child; never a user's existing Codex process.
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        finally:
            if self.selector:
                self.selector.close()
            self.process.stdout.close()
