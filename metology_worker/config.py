import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
