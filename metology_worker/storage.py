"""Private S3 object transfers. Object names always come from the claim."""
from contextlib import closing

from .runtime import WorkerError


class Storage:
    def __init__(self, client):
        self.client = client

    def download(self, a, path, check):
        request = dict(Bucket=a['input_bucket'], Key=a['input_object_key'])
        size = a['input_size_bytes']
        try:
            check()
            if self.client.head_object(**request)['ContentLength'] != size:
                raise WorkerError('input_object_invalid', False)
            response = self.client.get_object(**request)
            with closing(response['Body']) as body:
                if response['ContentLength'] != size:
                    raise WorkerError('input_object_invalid', False)
                with path.open('wb') as output:
                    count = 0
                    while True:
                        check()
                        chunk = body.read(min(1024 * 1024, size - count + 1))
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > size:
                            raise WorkerError('input_object_invalid', False)
                        output.write(chunk)
                    if count != size:
                        raise WorkerError('input_object_invalid', False)
        except Exception as error:
            self._translate(error, 'input_download_failed')

    def upload(self, a, path, check):
        from boto3.s3.transfer import TransferConfig
        try:
            self.client.upload_file(
                str(path), a['result_bucket'], a['result_object_key'],
                ExtraArgs={'ContentType': 'application/octet-stream'},
                Callback=lambda amount: check(),
                Config=TransferConfig(use_threads=False),
            )
            check()
            head = self.client.head_object(Bucket=a['result_bucket'], Key=a['result_object_key'])
            if head['ContentLength'] != path.stat().st_size:
                raise WorkerError('result_upload_invalid', True)
        except Exception as error:
            self._translate(error, 'result_upload_failed')

    def delete(self, a):
        self.client.delete_object(Bucket=a['result_bucket'], Key=a['result_object_key'])

    @staticmethod
    def _translate(error, code):
        # Preserve cancellation, disk failures and programmer exceptions.
        response = getattr(error, 'response', {})
        status = response.get('ResponseMetadata', {}).get('HTTPStatusCode')
        if status == 404 and code == 'input_download_failed':
            raise WorkerError('input_object_invalid', False) from None
        if type(error).__module__.startswith(('botocore', 'boto3', 's3transfer')):
            raise WorkerError(code, True) from None
        raise error


def create_storage(config):
    import boto3
    from botocore.config import Config
    client = boto3.client(
        's3', endpoint_url=config.s3_endpoint, region_name=config.s3_region,
        aws_access_key_id=config.s3_access_key, aws_secret_access_key=config.s3_secret_key,
        config=Config(connect_timeout=5, read_timeout=10,
                      retries={'mode': 'standard', 'total_max_attempts': 3},
                      s3={'addressing_style': config.s3_addressing_style}),
    )
    return Storage(client)
