"""Loopback SSH test server only; never shipped as a runtime dependency."""
import os
from pathlib import Path
import socket
import re
import subprocess
import threading
import time

try:
    import paramiko
except ImportError:
    paramiko = None


class SSHFixture:
    def __init__(self, root, environment):
        self.root, self.environment = Path(root), environment
        self.key = paramiko.RSAKey.generate(2048)
        self.host_key = paramiko.RSAKey.generate(2048)
        self.key.write_private_key_file(str(self.root / "client.key"))
        (self.root / "client.key").chmod(0o600)
        if os.name == "nt":
            # Only the fresh fixture key, never the user's SSH files. Win32
            # OpenSSH correctly rejects the shared system TEMP directory ACL.
            identity = re.search(r"S-\d(?:-\d+)+", subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)).group()
            subprocess.run(["icacls", str(self.root / "client.key"), "/inheritance:r", "/grant:r", "*" + identity + ":(F)"], check=True, capture_output=True)
        self.socket = socket.socket(); self.socket.bind(("127.0.0.1", 0)); self.socket.listen(8); self.socket.settimeout(.2)
        self.port = self.socket.getsockname()[1]
        self.commands, self.transports, self.processes, self.threads = [], [], [], []
        self.stopped = threading.Event()
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text(f"[127.0.0.1]:{self.port} {self.host_key.get_name()} {self.host_key.get_base64()}\n", encoding="utf-8")
        self.config = self.root / "ssh_config"
        self.config.write_text(f'Host loopback-fixture\n HostName 127.0.0.1\n Port {self.port}\n User fixture\n IdentityFile "{(self.root / "client.key").as_posix()}"\n UserKnownHostsFile "{self.known_hosts.as_posix()}"\n IdentitiesOnly yes\n LogLevel ERROR\n', encoding="utf-8")
        self.thread = threading.Thread(target=self._accept, daemon=True); self.thread.start()

    def _accept(self):
        while not self.stopped.is_set():
            try: sock, _ = self.socket.accept()
            except socket.timeout: continue
            except OSError: break
            thread = threading.Thread(target=self._connection, args=(sock,), daemon=True)
            self.threads.append(thread); thread.start()

    def _connection(self, sock):
        owner = self
        class Server(paramiko.ServerInterface):
            def get_allowed_auths(self, username): return "publickey"
            def check_auth_publickey(self, username, key): return paramiko.AUTH_SUCCESSFUL if username == "fixture" and key == owner.key else paramiko.AUTH_FAILED
            def check_channel_request(self, kind, chanid): return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            def check_channel_exec_request(self, channel, command):
                thread = threading.Thread(target=owner._execute, args=(channel, command.decode("utf-8")), daemon=True)
                owner.threads.append(thread); thread.start(); return True
        transport = paramiko.Transport(sock); self.transports.append(transport)
        try:
            transport.add_server_key(self.host_key); transport.start_server(server=Server())
            while transport.is_active() and not self.stopped.wait(.05): pass
        except (OSError, EOFError, paramiko.SSHException): pass
        finally: transport.close()

    def _execute(self, channel, command):
        self.commands.append(command)
        argv = command if os.name == "nt" else ["/bin/sh", "-c", command]
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.environment, creationflags=0x08000000 if os.name == "nt" else 0)
        self.processes.append(process)
        def incoming():
            try:
                while chunk := channel.recv(65536): process.stdin.write(chunk); process.stdin.flush()
            except (OSError, EOFError): pass
            finally:
                try: process.stdin.close()
                except OSError: pass
        def output(stream, send):
            try:
                while chunk := os.read(stream.fileno(), 65536): send(chunk)
            except (OSError, EOFError): pass
        forward = threading.Thread(target=incoming, daemon=True)
        errors = threading.Thread(target=output, args=(process.stderr, channel.sendall_stderr), daemon=True)
        forward.start(); errors.start()
        output(process.stdout, channel.sendall)
        process.wait(); errors.join(5)
        try: channel.send_exit_status(process.returncode); channel.shutdown_write(); channel.close()
        except (OSError, EOFError): pass

    def disconnect(self):
        for transport in self.transports: transport.close()

    def close(self):
        self.stopped.set(); self.socket.close(); self.disconnect(); self.thread.join(5)
        for process in self.processes:
            try: process.wait(8)
            except subprocess.TimeoutExpired: process.kill(); process.wait(5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed: stream.close()
        deadline = time.monotonic() + 8
        for thread in self.threads: thread.join(max(0, deadline - time.monotonic()))
