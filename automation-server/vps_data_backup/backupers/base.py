import socks
import socket
import paramiko
from pathlib import Path


class Backuper:

    sock = socks.socksocket()
    sock.set_proxy(
        proxy_type=socks.SOCKS5,
        addr="127.0.0.1",
        port=10808
    )
    server_ip = None
    server_port = 22
    server_user = None
    private_key_file = None
    backup_services = []
    local_path = Path("vps_data_backup/backup-datas")

    def __init__(self):
        self.sock.connect((self.server_ip, self.server_port))
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        private_key = paramiko.Ed25519Key.from_private_key_file(
            self.private_key_file
        )
        self.ssh.connect(
            hostname=self.server_ip,
            username=self.server_user,
            pkey=private_key,
            sock=self.sock
        )

    def backup(self):
        for service in self.backup_services:
            cmd = "tar czf - -C '{}' .".format(service["data-path"])
            self.stdin, self.stdout, self.stderr = self.ssh.exec_command(cmd)
            with open(LOCAL_FILE, "wb") as f:
                while True:
                    data = stdout.channel.recv(1024 * 1024)
                    if not data:
                        break
                    f.write(data)