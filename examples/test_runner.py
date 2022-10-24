import asyncio
import time
from random import randint
from qw.client import QClient
from qw.utils import cPrint

qw = QClient()

print('SERVER : ', qw.get_servers())

async def very_long_task(seconds: int):
    print(f'This Function Sleep for {seconds} sec.')
    await asyncio.sleep(seconds)


async def run_task():
    result = await asyncio.gather(
        *[qw.run(very_long_task, 20)]
    )
    print(result)

if __name__ == '__main__':
    start_time = time.time()
    loop = asyncio.get_event_loop()
    top = loop.run_until_complete(
        run_task()
    )
    end_time = time.time() - start_time
    print(top)
    cPrint(f'Task took {end_time} seconds to run', level='DEBUG')
