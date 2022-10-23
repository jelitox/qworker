from navconfig import config

def get_worker_list(workers: list):
    """Convert a list of workers in a tuple of worker:port for Scheduler."""
    wl = []
    for worker in workers:
        w,p = worker.split(':')
        wl.append((w, p))
    return wl

### Worker Configuration
WORKER_DEFAULT_HOST = config.get('WORKER_DEFAULT_HOST', fallback='0.0.0.0')
WORKER_DEFAULT_PORT = config.get('WORKER_DEFAULT_PORT', fallback=8888)
WORKER_DEFAULT_QTY = config.get('WORKER_DEFAULT_QTY', fallback=4)
WORKER_QUEUE_SIZE = config.get('WORKER_QUEUE_SIZE', fallback=4)


REDIS_HOST = config.get('REDIS_HOST', fallback='localhost')
REDIS_PORT = config.get('REDIS_PORT', fallback=6379)
REDIS_WORKER_DB = config.get('REDIS_WORKER_DB', fallback=2)

WORKER_REDIS = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_WORKER_DB}"

WORKERS = [e.strip() for e in list(config.get(
    'WORKER_LIST', fallback='127.0.0.1:8181').split(","))]
WORKER_LIST = get_worker_list(WORKERS)

HIGH_LIST = [e.strip() for e in list(config.get(
    'WORKER_HIGH_LIST', fallback='127.0.0.1:8899').split(","))]
WORKER_HIGH_LIST = get_worker_list(HIGH_LIST)


try:
    from settings.settings import WORKER_LIST, WORKER_HIGH_LIST, WORKER_REDIS, WORKER_DEFAULT_HOST, WORKER_DEFAULT_PORT # pylint: disable=W0611
except ImportError:
    pass
