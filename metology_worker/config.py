import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWN_HOSTS = Path.home() / '.ssh/known_hosts'


@dataclass
class SSHConfig:
    enabled: bool = False
    host: str = ''
    port: int = 22
    username: str = ''
    key_path: Path | None = None
    known_hosts: Path = field(default_factory=lambda: DEFAULT_KNOWN_HOSTS)
    passphrase: str | None = field(default=None, repr=False)
    local_port: int = 15432
    remote_host: str = '127.0.0.1'
    remote_port: int = 5432
    database_sslmode: str = 'disable'

    def __post_init__(self):
        if not (1 <= self.port <= 65535 and 0 <= self.local_port <= 65535
                and 1 <= self.remote_port <= 65535):
            raise ValueError('invalid_ssh_port')
        if self.database_sslmode not in {'disable', 'verify-full'}:
            raise ValueError('invalid_ssh_database_sslmode')
        if self.enabled:
            if not all((self.host, self.username, self.key_path, self.remote_host)):
                raise ValueError('missing_ssh_configuration')
            if self.database_sslmode == 'disable' and self.remote_host not in {'127.0.0.1', '::1', 'localhost'}:
                raise ValueError('ssh_remote_database_requires_tls')

    @classmethod
    def load(cls):
        env = os.environ
        key = env.get('SSH_KEY_PATH', '')
        return cls(
            enabled=env.get('SSH_TUNNEL_ENABLED') == '1', host=env.get('SSH_HOST', ''),
            port=int(env.get('SSH_PORT', '22')), username=env.get('SSH_USER', ''),
            key_path=Path(key).expanduser().resolve() if key else None,
            known_hosts=Path(env.get('SSH_KNOWN_HOSTS_PATH', DEFAULT_KNOWN_HOSTS)).expanduser().resolve(),
            passphrase=env.get('SSH_KEY_PASSPHRASE') or None,
            local_port=int(env.get('SSH_LOCAL_PORT', '15432')),
            remote_host=env.get('SSH_REMOTE_HOST', '127.0.0.1'),
            remote_port=int(env.get('SSH_REMOTE_PORT', '5432')),
            database_sslmode=env.get('SSH_DATABASE_SSLMODE', 'disable'),
        )


@dataclass
class Config:
    facelift_path: Path
    temp_root: Path
    database_url: str = field(repr=False)
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: str = field(repr=False)
    s3_secret_key: str = field(repr=False)
    s3_region: str = 'us-east-1'
    s3_addressing_style: str = 'path'
    insecure: bool = False
    shutdown_seconds: int = 300
    notify_channel: str = 'metology_jobs'
    ssh: SSHConfig = field(default_factory=SSHConfig)

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', self.notify_channel):
            raise ValueError('invalid_notify_channel')

    @classmethod
    def load(cls):
        env = os.environ
        return cls(
            Path(env.get('FACELIFT_PATH', PROJECT_ROOT / 'worker_processors/FaceLift')).expanduser().resolve(),
            Path(env.get('WORKER_TEMP_ROOT', PROJECT_ROOT / 'worker-temp')).expanduser().resolve(),
            env.get('DATABASE_URL', ''), env.get('S3_ENDPOINT_URL', ''), env.get('S3_BUCKET', ''),
            env.get('S3_ACCESS_KEY_ID', ''), env.get('S3_SECRET_ACCESS_KEY', ''),
            env.get('S3_REGION', 'us-east-1'), env.get('S3_ADDRESSING_STYLE', 'path'),
            env.get('WORKER_ALLOW_INSECURE') == '1', int(env.get('WORKER_SHUTDOWN_SECONDS', '300')),
            notify_channel=env.get('DATABASE_NOTIFY_CHANNEL', 'metology_jobs'),
            ssh=SSHConfig.load(),
        )

    def validate_remote(self):
        if not all((self.database_url, self.s3_endpoint, self.s3_bucket, self.s3_access_key, self.s3_secret_key)):
            raise ValueError('missing_database_or_s3_configuration')
        if self.s3_addressing_style not in {'path', 'virtual', 'auto'}:
            raise ValueError('invalid_s3_addressing_style')
        endpoint = urlsplit(self.s3_endpoint)
        if not endpoint.hostname or endpoint.username or endpoint.password:
            raise ValueError('invalid_s3_endpoint')
        if endpoint.scheme not in ({'http', 'https'} if self.insecure else {'https'}):
            raise ValueError('s3_https_required')
        if not 1 <= self.shutdown_seconds <= 3600:
            raise ValueError('invalid_shutdown_seconds')
