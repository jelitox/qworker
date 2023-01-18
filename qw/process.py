import asyncio
import multiprocessing as mp
import resource as res
import subprocess
from collections.abc import Callable
import socket
import aioredis
from navconfig.logging import logging
from qw.exceptions import QWException
from qw.discovery import get_server_discovery
from .utils.json import json_encoder
from .conf import (
    NOFILES,
    WORKER_REDIS,
    QW_WORKER_LIST,
    WORKER_DISCOVERY_PORT
)

from .server import start_server

JOB_LIST = []

def raise_nofile(value: int = 4096) -> tuple[str, int]:
    """raise_nofile.

    sets nofile soft limit to at least 4096.
    """
    soft, hard = res.getrlimit(res.RLIMIT_NOFILE)
    # print(f'getting hard ulimit -n {soft} {hard}')
    if soft < value:
        soft = value

    if hard < soft:
        hard = soft
    try:
        print(f'setting soft & hard ulimit -n {soft} {hard}')
        res.setrlimit(res.RLIMIT_NOFILE, (soft, hard))
    except (ValueError, AttributeError) as err:
        logging.exception(err)
        try:
            ulimit = 'ulimit -{type} {value};'
            subprocess.Popen(ulimit.format(type='n', value=hard), shell=True)
        except Exception as e: # pylint: disable=W0703
            print('failed to set ulimit, giving up')
            logging.exception(e, stack_info=False)
    return 'nofile', (soft, hard)


class spawn_process(object):
    def __init__(self, args, event_loop):
        self.loop: asyncio.AbstractEventLoop = event_loop
        self.host: str = args.host
        self.address = socket.gethostbyname(socket.gethostname())
        self.port: int = args.port
        self.worker: str = f"{args.wkname}-{args.port}"
        self.debug: bool = args.debug
        self.redis: Callable = None
        self.discovery_server: Callable = None
        self.transport: asyncio.Transport = None
        # increase the ulimit of server
        raise_nofile(value=NOFILES)
        for i in range(args.workers):
            p = mp.Process(
                target=start_server,
                name=f'{self.worker}_{i}',
                args=(i, args.host, args.port, args.debug, )
            )
            JOB_LIST.append(p)
            p.start()


    async def start_redis(self):
        # starting redis:
        try:
            self.redis = aioredis.ConnectionPool.from_url(
                    WORKER_REDIS,
                    decode_responses=True,
                    encoding='utf-8'
            )
        except Exception as e:
            raise Exception(e) from e

    async def stop_redis(self):
        try:
            conn = aioredis.Redis(connection_pool=self.redis)
            await conn.delete(
                QW_WORKER_LIST
            )
            await self.redis.disconnect(inuse_connections = True)
        except Exception as err: # pylint: disable=W0703
            logging.exception(err)

    async def register_worker(self):
        worker = json_encoder({
            self.worker: (self.address, self.port)
        })
        conn = aioredis.Redis(connection_pool=self.redis)
        await conn.lpush(
            QW_WORKER_LIST,
            worker
        )
        if self.discovery_server:
            self.discovery_server.register_worker(
                server=self.worker, addr=(self.address, self.port)
            )
        else:
            # self-registration into Discovery Service:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            srv_addr = ('', WORKER_DISCOVERY_PORT)
            try:
                sock.sendto(worker.encode(), srv_addr)
            except socket.timeout as ex:
                raise QWException(
                    "Unable to register Worker on this Network."
                ) from ex

    async def remove_worker(self):
        worker = {
            self.worker: (self.address, self.port)
        }
        conn = aioredis.Redis(connection_pool=self.redis)
        await conn.lrem(
            QW_WORKER_LIST,
            1,
            json_encoder(worker)
        )
        if self.discovery_server:
            self.discovery_server.remove_worker(
                server=self.worker
            )

    def start(self):
        try:
            # start a redis connection
            self.loop.run_until_complete(
                self.start_redis()
            )
            try:
                self.transport, self.discovery_server = self.loop.run_until_complete(
                    get_server_discovery(event_loop=self.loop)
                )
                print(':: Starting Discovery Server ::')
            except Exception:
                pass
            # register worker in the worker list
            self.loop.run_until_complete(
                self.register_worker()
            )
        except Exception as err: # pylint: disable=W0703
            logging.error(err)

    def terminate(self):
        try:
            self.loop.run_until_complete(
                self.remove_worker()
            )
            self.loop.run_until_complete(
                self.stop_redis()
            )
            if self.transport:
                self.transport.close()
        except Exception as err: # pylint: disable=W0703
            logging.exception(err)
        for j in JOB_LIST:
            j.terminate()
            j.join()
