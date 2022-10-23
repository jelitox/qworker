"""QueueWorker Server Implementation"""
import asyncio
import inspect
import logging
import multiprocessing as mp
import os
import queue
import socket
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import partial
from typing import Callable

import cloudpickle
import jsonpickle
import uvloop
from asyncdb.exceptions import default_exception_handler
from dataintegration.exceptions import *
from dataintegration.utils.json import json_encoder

from settings.settings import (WORKER_DEFAULT_HOST, WORKER_DEFAULT_PORT,
                               WORKER_DEFAULT_QTY, WORKER_QUEUE_SIZE)

from .utils import cPrint
from .wrappers import FuncWrapper, QueueWrapper, TaskWrapper

asyncio.set_event_loop_policy(
    uvloop.EventLoopPolicy()
)
uvloop.install()

try:
    DEFAULT_HOST = WORKER_DEFAULT_HOST
except Exception as e:
    DEFAULT_HOST = socket.gethostbyname(socket.gethostname())


def start_server(num_worker, host, port, debug: bool):
    """thread worker function"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    worker = QWorker(
        host=host,
        port=port,
        event_loop=loop,
        debug=debug,
        worker_id=num_worker
    )
    loop.set_exception_handler(default_exception_handler)
    try:
        loop.run_until_complete(
            worker.start()
        )
    except (KeyboardInterrupt, Exception):
        print(f'Shutdown Worker {worker.name}')
        loop.run_until_complete(
            worker.shutdown()
        )
    finally:
        loop.close()


class QWorker:
    """Queue Task Worker server.

    Attributes:
        host: Hostname of the server.
        port: Port number of the server.
        loop: Event loop to run in.
        task_executor: Executor that will run tasks from clients.
    """
    def __init__(
            self,
            host: str = DEFAULT_HOST,
            port: int = WORKER_DEFAULT_PORT,
            worker_id: int = None,
            name: str = '',
            event_loop: asyncio.AbstractEventLoop = None,
            debug: bool = False,
            protocol = None
    ):
        self.host = host
        self.port = port
        self.debug = debug
        self.queue = None
        self.consumers = []
        self.executor = None
        self._id = worker_id
        if name:
            self._name = name
        else:
            self._name = mp.current_process().name
        self._executor = ThreadPoolExecutor(
            max_workers=WORKER_DEFAULT_QTY
        )
        if not event_loop:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        else:
            self._loop = event_loop
        self._server: Callable = None
        self._pid = os.getpid()
        self._protocol = protocol

    @property
    def name(self):
        return self._name

    async def start(self):
        """Starts Queue server."""
        self.queue = asyncio.Queue(maxsize=WORKER_QUEUE_SIZE)
        self.executor = ProcessPoolExecutor(
            max_workers=WORKER_DEFAULT_QTY
        )
        if self._protocol:
            coro = self._loop.create_server(
                self._protocol,
                host=self.host,
                port=self.port,
                family=socket.AF_INET,
                reuse_port=True
            )
        else:
            coro = asyncio.start_server(
                self.connection_handler,
                host=self.host,
                port=self.port,
                family=socket.AF_INET,
                reuse_port=True,
                loop=self._loop
            )
        # server
        self._server = await coro
        try:
            await self.fire_consumers()
            sock = self._server.sockets[0].getsockname()
            logging.info(
                f'Serving {self._name}:{self._id} on {sock}, pid: {self._pid}'
            )
        except Exception as err:
            logging.error(err)
            raise Exception(f"Error: {err}") from err
        # Serve requests until Ctrl+C is pressed
        try:
            async with self._server:
                await self._server.serve_forever()
        except Exception as err:
            logging.exception(err, stack_info=True)

    async def fire_consumers(self):
        """Fire up the consumers."""
        self.consumers = [
            asyncio.create_task(
                self.task_handler(self.queue)) for _ in range(WORKER_QUEUE_SIZE)
        ]

    async def empty_queue(self, q: asyncio.Queue):
        """Processing and shutting down the Queue."""
        for _ in range(q.qsize()):
            try:
                q.get_nowait()
                q.task_done()
            except queue.Empty:
                pass
        await q.join()

    async def shutdown(self):
        if self.debug is True:
            cPrint(f'Shutting down worker {self.name!s}')
        try:
            # forcing close the queue
            await self.empty_queue(self.queue)
        except KeyboardInterrupt:
            pass
        # also: cancel the idle consumers:
        for c in self.consumers:
            c.cancel()
        try:
            self._server.close()
            await self._server.wait_closed()
        except Exception as err:
            logging.exception(err, stack_info=True)
        if self.debug is True:
            cPrint('::: QueueWorker Server Closed ::: ', level='INFO')

    @classmethod
    def create_server(cls, num_worker, host, port, debug: bool = False):
        """Factory method that creates an instance of the Worker Server.

        Args:
            host: Hostname of the server.
            port: Port number of the server.
            num_worker: Number of this worker.
            debug: optional boolean for enabling debug
        Returns:
            An instance of the server.
        """
        event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(event_loop)
        return cls(
            host=host,
            port=port,
            event_loop=event_loop,
            debug=debug,
            worker_id=num_worker
        )

    def run_process(self, fn):
        """Unpickles task, runs it and pickles result."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        fn.set_loop(loop)
        # task = loop.create_task(self.run_function(fn, loop))
        # group = asyncio.gather(
        #     *[task], return_exceptions=True
        # )
        try:
            result = loop.run_until_complete(
                self.run_function(fn, loop)
            )
            return result
        except (RuntimeError, Exception) as err:
            raise Exception(f"Error: {err}") from err
        finally:
            loop.close()

    async def run_function(self, fn, event_loop: asyncio.AbstractEventLoop):
        result = None
        print(f'Running Task {fn!s} with id {fn.id} in worker {self.name!s}')
        try:
            asyncio.set_event_loop(event_loop)
            if inspect.isawaitable(fn):
                result = await fn()
            else:
                result = fn()
        except Exception as err:
            result = err
        return result

    async def run_task(self, task: TaskWrapper):
        result = None
        try:
            await task.create()
            result = await task.run()
        except Exception as err:
            result = err
        finally:
            try:
                await task.close()
            except Exception:
                pass
        print(f'RUN TASK {task!s} RESULT> ', result)
        return result

    def run_queued_task(self, task):
        """Run a DI-task in a isolated process pool."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task.set_loop(loop)
        task = loop.create_task(self.run_task(task))
        # group = asyncio.gather(
        #     *[task], return_exceptions=True
        # )
        result = None
        try:
            result = loop.run_until_complete(task)
            if isinstance(result, Exception):
                return result
            print(f'QUEUED TASK {task!s} RESULT> ', result)
            return result
        except (RuntimeError, Exception) as err:
            raise Exception(f"Error: {err}") from err
        finally:
            loop.close()

    async def task_handler(self, q: asyncio.Queue):
        """Method for handling the tasks received by the connection handler."""
        while True:
            task = await q.get()
            if self.debug:
                cPrint(f'Running Queued Task {task!s}', level='DEBUG')
            # processing the task received
            if isinstance(task, TaskWrapper):
                # Running a DataIntegrator Task
                task.set_loop(self._loop)
                task.debug = self.debug
                result = await self.run_task(task)
                logging.debug(f'{task!s} Result: {result!r}')
            elif isinstance(task, FuncWrapper):
                # running a FuncWrapper
                result = None
                try:
                    result = await self.run_task(task)
                except Exception as err:
                    result = err
                logging.debug(f'{task!s} Result: {result!r}')
            else:
                # TODO: try to Execute the object deserialized
                pass
            q.task_done()
            logging.debug(f'consumed: {task}')
            logging.debug(f'QUEUE Size after Work: {self.queue.qsize()}')

    async def connection_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """ Handler for Task Execution.

        receives the client request and launch the task.

        Args:
            reader: asyncio StreamReader, client information
            writer: asyncio StreamWriter, infor to send to client.
        Returns:
            Task Result.
        """
        # # TODO: task can select which executor to use, else use default:
        print(f':: Starting Handler on worker {self.name!s} ::')
        serialized_task = b''
        while True:
            serialized_task = await reader.read(-1)
            if reader.at_eof():
                break
        result = None
        task_uuid = uuid.uuid4()
        addr = writer.get_extra_info("peername")
        logging.debug(f"Received Task from {addr!r} into worker {self.name!s} pid: {self._pid}")
        try:
            task = cloudpickle.loads(serialized_task)
            logging.debug(f'TASK RECEIVED: {task}')
        except EOFError:
            # send a pong
            result = "Pong: Empty Data"
            await self.closing_writer(writer, result.encode('utf-8'))
            return True
        except Exception as err:
            e = Exception(f'No Valid Function was sent to Worker: {err}')
            result = cloudpickle.dumps(e)
            await self.closing_writer(writer, result)
            return False
        # evaluate different tasks
        if not task or isinstance(task, str):
            addrs = ', '.join(str(sock.getsockname()) for sock in self._server.sockets)
            if task == 'health':
                # can return health of worker
                status = {
                    "queue": {
                        "size": self.queue.qsize(),
                        "full": self.queue.full(),
                        "empty": self.queue.empty(),
                        "consumers": len(self.consumers)
                    },
                    "worker": {
                        "name": self.name,
                        "serving": addrs
                    }
                }
            else:
            # its a simple keepalive:
                status = {
                    "pong": "Empty data",
                    "worker": {
                        "name": self.name,
                        "serving": addrs
                    }
                }
            result = json_encoder(status)
            await self.closing_writer(writer, result.encode('utf-8'))
            return True
        elif isinstance(task, QueueWrapper):
            # Set Debug level of task:
            task.debug = self.debug
            if task.queued is True:
                try:
                    task.id = task_uuid
                    await self.queue.put(task)
                    logging.debug(f'Current QUEUE Size: {self.queue.qsize()}')
                    result = f'Task {task!s} with id {task_uuid} was queued.'.encode('utf-8')
                except asyncio.QueueFull:
                    logging.debug(
                        f"Worker {self.name!s} Queue is Full, discarding Task {task!r}"
                    )
                    result = {
                        "error": f"Worker {self.name!s} Queue is Full, discarding Task {task!r}"
                    }
                    await self.closing_writer(writer, result)
                    return False
            else:
                try:
                    # executed and send result to client
                    task.id = task_uuid
                    fn = partial(self.run_process, task)
                    result = await self._loop.run_in_executor(self._executor, fn)
                except Exception as err:
                    try:
                        result = cloudpickle.dumps(err)
                    except Exception as e:
                        result = cloudpickle.dumps(
                            Exception(f'Error on Worker: {str(e)}')
                        )
                    await self.closing_writer(writer, result)
                    return False
        else:
            # put work in Queue:
            try:
                await self.queue.put(task)
                await asyncio.sleep(.1)
                logging.debug(f'Current QUEUE Size: {self.queue.qsize()}')
                result = f'Task {task!s} with id {task_uuid} was queued.'.encode('utf-8')
            except asyncio.QueueFull:
                logging.debug(
                    f"Worker Queue is Full, discarding Task {task!r}"
                )
        if result is None:
            result = [
                {"Error": True, "Task": task, "uuid": task_uuid, "worker": self.name}
            ]
        try:
            if isinstance(result, BaseException):
                # sending Task Exception
                result = jsonpickle.encode(result)
            elif inspect.isgeneratorfunction(result) or isinstance(result, list):
                try:
                    result = json_encoder(list(result))
                except TypeError:
                    result = f"{result!r}" # cannot pickle a generator object
            result = cloudpickle.dumps(result)
        except Exception as err:
            result = cloudpickle.dumps(err)
            logging.error(f'Error dumping result: {err!s}')
        await self.closing_writer(writer, result)
        return True

    async def closing_writer(self, writer: asyncio.StreamWriter, result):
        """Sending results and closing the streamer."""
        writer.write(result)
        await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
        if self.debug is True:
            cPrint(f"Closing client socket, pid: {self._pid}", level='DEBUG')
        writer.close()
