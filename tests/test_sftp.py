"""Download against an in-process SFTP server (password or keyboard-interactive only)."""

import os
import socket
import threading

import paramiko
import pytest

from xplan_extract import sftp

USER, PASSWORD = "au-dm-1234", "Li9=x?K@a5*"


class _Server(paramiko.ServerInterface):
    def __init__(self, auth_type):
        self.auth_type = auth_type

    def get_allowed_auths(self, username):
        return self.auth_type

    def check_auth_password(self, username, password):
        if self.auth_type == "password" and (username, password) == (USER, PASSWORD):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        if self.auth_type != "keyboard-interactive":
            return paramiko.AUTH_FAILED
        query = paramiko.InteractiveQuery()
        query.add_prompt("Password: ", False)
        return query

    def check_auth_interactive_response(self, responses):
        return paramiko.AUTH_SUCCESSFUL if list(responses) == [PASSWORD] else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class _Sftp(paramiko.SFTPServerInterface):
    root = None

    def _path(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def list_folder(self, path):
        out = []
        for name in os.listdir(self._path(path)):
            attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(self._path(path), name)))
            attr.filename = name
            out.append(attr)
        return out

    def stat(self, path):
        return paramiko.SFTPAttributes.from_stat(os.stat(self._path(path)))

    lstat = stat

    def open(self, path, flags, attr):
        handle = paramiko.SFTPHandle(flags)
        handle.readfile = open(self._path(path), "rb")
        handle.filename = self._path(path)
        return handle


@pytest.fixture
def server(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "extract.zip").write_bytes(b"z" * 100_000)
    _Sftp.root = str(remote)
    host_key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    state = {"auth": "password"}

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(host_key)
            t.set_subsystem_handler("sftp", paramiko.SFTPServer, _Sftp)
            t.start_server(server=_Server(state["auth"]))

    threading.Thread(target=serve, daemon=True).start()
    yield listener.getsockname()[1], state, tmp_path
    listener.close()


@pytest.mark.parametrize("auth", ["password", "keyboard-interactive"])
def test_download(server, auth):
    port, state, tmp = server
    state["auth"] = auth
    files = sftp.download("127.0.0.1", port, USER, PASSWORD, tmp / "dl",
                          known_hosts=tmp / "known_hosts", accept_new_host_key=True,
                          progress=lambda m: None)
    assert [f.name for f in files] == ["extract.zip"]
    assert files[0].stat().st_size == 100_000


@pytest.mark.parametrize("auth", ["password", "keyboard-interactive"])
def test_wrong_password(server, auth):
    port, state, tmp = server
    state["auth"] = auth
    with pytest.raises(sftp.SftpError, match="Authentication failed"):
        sftp.download("127.0.0.1", port, USER, "wrong", tmp / "dl",
                      known_hosts=tmp / "known_hosts", accept_new_host_key=True,
                      progress=lambda m: None)


def test_unknown_host_key_refused(server):
    port, _, tmp = server
    with pytest.raises(sftp.SftpError, match="not yet trusted"):
        sftp.download("127.0.0.1", port, USER, PASSWORD, tmp / "dl",
                      known_hosts=tmp / "known_hosts", progress=lambda m: None)
