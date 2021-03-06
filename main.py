import logging
import socket
from datetime import datetime
from os import path, remove
from subprocess import DEVNULL, CalledProcessError, check_call
from time import sleep
from typing import Union
from urllib.parse import urlparse

import redis
from azure.storage.blob import BlobServiceClient
from pythonjsonlogger import jsonlogger

from settings import settings

logger = logging.getLogger()
logHandler = logging.StreamHandler()
logFmt = jsonlogger.JsonFormatter(timestamp=True)
logHandler.setFormatter(logFmt)
logger.addHandler(logHandler)


def is_leader(dbname):
    if settings.leader_election_enabled:
        r = redis.Redis.from_url(settings.redis_url)
        lock_key = f"postgres-to-azureblob-{dbname}"
        hostname = socket.gethostname()
        is_leader = False

        with r.pipeline() as pipe:
            try:
                pipe.watch(lock_key)
                leader_host = pipe.get(lock_key)
                if leader_host in (hostname.encode(), None):
                    pipe.multi()
                    pipe.setex(lock_key, 10, hostname)
                    pipe.execute()
                    is_leader = True
            except redis.WatchError:
                pass
    else:
        is_leader = True
    return is_leader


def cleanup(filename: str) -> None:
    try:
        logging.warning(msg="Performing Cleanup", extra={"file": filename})
        remove(filename)
    except FileNotFoundError:
        pass
    return None


def upload_blob(filename: str, date: datetime) -> None:
    client = BlobServiceClient.from_connection_string(settings.blob_storage_connection_string)
    blobname = f"{date.year}/{date.month}/{date.day}/{path.basename(filename)}"
    if settings.blob_storage_path_prefix:
        blobname = f"{settings.blob_storage_path_prefix}/{blobname}"
    logging.warning(
        msg="Uploading blob",
        extra={"local_file": filename, "remote_container": settings.blob_storage_container, "remote_file": blobname},
    )
    blob = client.get_blob_client(container=settings.blob_storage_container, blob=blobname)
    with open(filename, "rb") as f:
        blob.upload_blob(f)
    return None


def dump_database() -> Union[dict, None]:
    date = datetime.utcnow()
    database_name = urlparse(settings.psql_connection_string).path[1:]
    filename = f"/tmp/{date.strftime('%FT%H%M%SZ')}-{database_name}.psql"
    command = ["pg_dump", "--format=custom", settings.psql_connection_string]
    if is_leader(dbname=database_name):
        for i in range(settings.retry_count):
            with open(filename, "wb") as f:
                attempt = i + 1
                log_extras = {
                    "database": database_name,
                    "file": filename,
                    "retry_count": settings.retry_count,
                    "attempt": attempt,
                }
                try:
                    logging.warning(msg="Export start", extra=log_extras)
                    check_call(command, stdout=f, stderr=DEVNULL)
                    logging.warning(msg="Export complete", extra=log_extras)
                    return {"filename": filename, "date": date}
                except CalledProcessError:
                    if attempt < settings.retry_count:
                        logging.error(msg="Export failed, retrying", extra=log_extras)
                        sleep(settings.retry_delay)
                        continue
                    else:
                        logging.error(msg="Export failed, giving up", extra=log_extras)
                        return None
    else:
        logging.warning(msg="Leader Election Failed, Skipping", extra={"database": database_name})


if __name__ == "__main__":
    dump = dump_database()
    if dump is not None:
        upload_blob(filename=dump.get("filename"), date=dump.get("date"))
        cleanup(filename=dump.get("filename"))
