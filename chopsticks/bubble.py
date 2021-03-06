from __future__ import print_function
import sys
sys.path = [p for p in sys.path if p.startswith('/')]


def debug(msg):
    print(msg, file=sys.stderr)

# Reshuffle fds so that we can't break our transport by printing to stdout
import os
infd = os.dup(0)
outfd = os.dup(1)
inpipe = os.fdopen(infd, 'rb')
outpipe = os.fdopen(outfd, 'wb', 0)
sys.stdin.close()
sys.stdin = open(os.devnull, 'rb')
sys.stdout.close()
sys.stdout = open(os.devnull, 'wb')

PY2 = sys.version_info < (3,)
PY3 = not PY2
import threading
if PY2:
    __metaclass__ = type
    from Queue import Queue
    import cPickle as pickle

    def exec_(_code_, _globs_=None, _locs_=None):
        """Execute code in a namespace."""
        if _globs_ is None:
            frame = sys._getframe(1)
            _globs_ = frame.f_globals
            if _locs_ is None:
                _locs_ = frame.f_locals
            del frame
        elif _locs_ is None:
            _locs_ = _globs_
        exec("""exec _code_ in _globs_, _locs_""")
else:
    from queue import Queue
    import pickle
    exec_ = getattr(__builtins__, 'exec')
import json
import struct
import imp
import base64
from collections import namedtuple
import signal
from hashlib import sha1

outqueue = Queue(maxsize=10)
tasks = Queue()
done = object()

running = True

Imp = namedtuple('Imp', 'exists is_pkg file source')
PREFIX = 'controller://'


class Loader:
    cache = {}
    ev = threading.Event()

    def __init__(self, path):
        if not path.startswith(PREFIX):
            raise ImportError()
        self.path = path

    def get(self, fullname):
        if fullname in self.cache:
            return self.cache[fullname]
        self.ev.clear()
        outqueue.put({'imp': fullname})
        self.ev.wait()
        imp = self.cache[fullname]
        if not imp.exists:
            raise ImportError()
        return imp

    def find_module(self, fullname, path=None):
        try:
            self.get(fullname)
        except ImportError:
            return None
        return self

    def load_module(self, fullname):
        m = self.get(fullname)
        mod = sys.modules.setdefault(fullname, imp.new_module(fullname))
        mod.__file__ = PREFIX + m.file
        mod.__loader__ = self
        if m.is_pkg:
            mod.__path__ = [PREFIX + m.file]
            mod.__package__ = fullname
        else:
            mod.__package__ = fullname.rpartition('.')[0]
        exec(compile(m.source, mod.__file__, 'exec'), mod.__dict__)
        return mod

    def is_package(self, fullname):
        return self.get(fullname).is_pkg

    def get_source(self, fullname):
        return self.get(fullname).source


sys.path.append(PREFIX)
sys.path_hooks.append(Loader)


def handle_call(req_id, params):
    """Pass a request to the main thread."""
    try:
        callable, args, kwargs = pickle.loads(base64.b64decode(params))
    except:
        ret = callable(*args, **kwargs)
    else:
        tasks.put((req_id, callable, args, kwargs))


def handle_fetch(req_id, path):
    """Fetch a file by path."""
    tasks.put((req_id, do_fetch, (req_id, path,)))


def do_fetch(req_id, path):
    """Send chunks of a file to the orchestration host."""
    h = sha1()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(10240)
            if not chunk:
                break
            h.update(chunk)
            data = base64.b64encode(chunk)
            if not PY2:
                data = data.decode('ascii')
            outqueue.put({
                'req_id': req_id,
                'data': data
            })
    return {
        'remote_path': os.path.abspath(path),
        'sha1sum': h.hexdigest(),
    }


def do_call(req_id, callable, args=(), kwargs={}):
    try:
        ret = callable(*args, **kwargs)
    except:
        import traceback
        msg = {
            'req_id': req_id,
            'tb': traceback.format_exc()
        }
    else:
        msg = {
            'req_id': req_id,
            'ret': ret,
        }
    outqueue.put(msg)


def handle_imp(mod, exists, is_pkg, file, source):
    Loader.cache[mod] = Imp(exists, is_pkg, file, source)
    Loader.ev.set()


def read_msg():
    buf = inpipe.read(4)
    if not buf:
        return
    (size,) = struct.unpack('!L', buf)
    chunk = inpipe.read(size)
    if PY3:
        chunk = chunk.decode('ascii')
    return json.loads(chunk)


def reader():
    try:
        while True:
            obj = read_msg()
            if not obj:
                return
            op = obj.pop('op')
            handler = globals()['handle_' + op]
            if PY2:
                obj = dict((str(k), v) for k, v in obj.iteritems())
            handler(**obj)
    finally:
        outqueue.put(done)
        tasks.put(done)
        # SIGINT will raise KeyboardInterrupt in the main (ie. task) thread
        # TODO: Perhaps give this some timeout, in case operations can complete
        # successfully?
        os.kill(os.getpid(), signal.SIGINT)


def writer():
    while True:
        msg = outqueue.get()
        if msg is done:
            break
        # pickle is unsafe for the return transport
        buf = json.dumps(msg)
        if PY3:
            buf = buf.encode('ascii')
        outpipe.write(struct.pack('!L', len(buf)))
        outpipe.write(buf)


for func in (reader, writer):
    threading.Thread(target=func).start()


while True:
    task = tasks.get()
    if task is done:
        break
    do_call(*task)
