# Copyright 2011-2012 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

import atexit
import os
import socket
import sys
import time
import threading
import weakref

from pymongo import thread_util
from pymongo.common import HAS_SSL
from pymongo.errors import AutoReconnect, ConnectionFailure, ConfigurationError

try:
    from ssl import match_hostname
except ImportError:
    from pymongo.ssl_match_hostname import match_hostname

if HAS_SSL:
    import ssl

if sys.platform.startswith('java'):
    from select import cpython_compatible_select as select
else:
    from select import select

try:
    import gevent.coros
except ImportError:
    pass


import logging
log = logging.getLogger(__name__)


NO_REQUEST = None
NO_SOCKET_YET = -1

MONITORS = set()


def register_monitor(monitor):
    ref = weakref.ref(monitor, _on_monitor_deleted)
    MONITORS.add(ref)


def _on_monitor_deleted(ref):
    """Remove the weakreference from the set
    of active MONITORS. We no longer
    care about keeping track of it
    """
    MONITORS.remove(ref)


def shutdown_monitors():
    # Keep a local copy of MONITORS as
    # shutting down threads has a side effect
    # of removing them from the MONITORS set()
    monitors = list(MONITORS)
    for ref in monitors:
        monitor = ref()
        if monitor:
            monitor.shutdown()
            monitor.join()
atexit.register(shutdown_monitors)


class Monitor(object):
    """Base class for pool monitors.
    """
    def __init__(self, pool, event_class, refresh_interval):
        self.pool = weakref.proxy(pool, self.shutdown)
        self.event = event_class()
        self.stopped = False
        self.refresh_interval = refresh_interval

    def shutdown(self, dummy=None):
        """Signal the monitor to shutdown.
        """
        self.stopped = True
        self.event.set()

    def schedule_refresh(self):
        """Refresh immediately
        """
        self.event.set()

    def monitor(self):
        """Run until the Pool is collected or an
        unexpected error occurs.
        """
        log.info('Pool monitor start')
        try:
            while True:
                self.event.wait(self.refresh_interval)
                if self.stopped:
                    break
                self.event.clear()
                log.info('Pool monitor checking request socks')
                try:
                    self.pool.check_request_socks(force=True)
                except AutoReconnect:
                    import traceback
                    log.info('AutoReconnect in monitor %s', traceback.format_exc())
                    pass
                # Pool has been collected or there
                # was an unexpected error.
                except:
                    import traceback
                    log.info('Exception killing monitor %s', traceback.format_exc())
                    break
        finally:
            log.info('Pool monitor end')


class MonitorThread(Monitor, threading.Thread):
    """Thread based replica set monitor.
    """
    def __init__(self, pool, refresh_interval):
        Monitor.__init__(self, pool, threading.Event, refresh_interval)
        threading.Thread.__init__(self)
        self.setName("PoolMonitorThread")

    def run(self):
        """Override Thread's run method.
        """
        self.monitor()


have_gevent = False
try:
    from gevent import Greenlet
    from gevent.event import Event

    # Used by ReplicaSetConnection
    from gevent.local import local as gevent_local
    have_gevent = True

    class MonitorGreenlet(Monitor, Greenlet):
        """Greenlet based replica set monitor.
        """
        def __init__(self, pool, refresh_interval):
            Monitor.__init__(self, pool, Event, refresh_interval)
            Greenlet.__init__(self)

        # Don't override `run` in a Greenlet. Add _run instead.
        # Refer to gevent's Greenlet docs and source for more
        # information.
        def _run(self):
            """Define Greenlet's _run method.
            """
            self.monitor()

except ImportError:
    pass


def _closed(sock):
    """Return True if we know socket has been closed, False otherwise.
    """
    try:
        rd, _, _ = select([sock], [], [], 0)
    # Any exception here is equally bad (select.error, ValueError, etc.).
    except:
        return True
    return len(rd) > 0


class SocketInfo(object):
    """Store a socket with some metadata
    """
    def __init__(self, sock, pool_id, host=None, pool=None):
        self.sock = sock
        self.host = host
        self.authset = set()
        self.closed = False
        self.last_checkout = time.time()
        self.forced = False

        # The pool's pool_id changes with each reset() so we can close sockets
        # created before the last reset.
        self.pool_id = pool_id
        self.poolref = weakref.ref(pool)

    def close(self):
        self.closed = True
        # Avoid exceptions on interpreter shutdown.
        try:
            self.sock.close()
        except:
            pass

    def __eq__(self, other):
        # Need to check if other is NO_REQUEST or NO_SOCKET_YET, and then check
        # if its sock is the same as ours
        return hasattr(other, 'sock') and self.sock == other.sock

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(self.sock)

    def __repr__(self):
        return "SocketInfo(%s)%s at %s" % (
            repr(self.sock),
            self.closed and " CLOSED" or "",
            id(self)
        )

    def __del__(self):
        log.info('SocketInfo.__del__ %r', self)
        if self.closed:
            log.info('SocketInfo.__del__ already closed %r', self)
            return
        if not self.pool():
            log.info('SocketInfo.__del__ Pool already gone %r', self)
            return
        if self in self.pool().sockets:
            log.info('SocketInfo.__del__ already returned %r', self)
            return
        log.info('SocketInfo.__del__ calling maybe_return_socket %r', self)
        self.pool().maybe_return_socket(self)


# Do *not* explicitly inherit from object or Jython won't call __del__
# http://bugs.jython.org/issue1057
class Pool:
    def __init__(self, pair, max_size, net_timeout, conn_timeout, use_ssl,
                 use_greenlets, ssl_keyfile=None, ssl_certfile=None,
                 ssl_cert_reqs=None, ssl_ca_certs=None,
                 wait_queue_timeout=None, wait_queue_multiple=None):
        """
        :Parameters:
          - `pair`: a (hostname, port) tuple
          - `max_size`: The maximum number of open sockets. Calls to
            `get_socket` will block if this is set, this pool has opened
            `max_size` sockets, and there are none idle. Set to `None` to
             disable.
          - `net_timeout`: timeout in seconds for operations on open connection
          - `conn_timeout`: timeout in seconds for establishing connection
          - `use_ssl`: bool, if True use an encrypted connection
          - `use_greenlets`: bool, if True then start_request() assigns a
              socket to the current greenlet - otherwise it is assigned to the
              current thread
          - `ssl_keyfile`: The private keyfile used to identify the local
            connection against mongod.  If included with the ``certfile` then
            only the ``ssl_certfile`` is needed.  Implies ``ssl=True``.
          - `ssl_certfile`: The certificate file used to identify the local
            connection against mongod. Implies ``ssl=True``.
          - `ssl_cert_reqs`: Specifies whether a certificate is required from
            the other side of the connection, and whether it will be validated
            if provided. It must be one of the three values ``ssl.CERT_NONE``
            (certificates ignored), ``ssl.CERT_OPTIONAL``
            (not required, but validated if provided), or ``ssl.CERT_REQUIRED``
            (required and validated). If the value of this parameter is not
            ``ssl.CERT_NONE``, then the ``ssl_ca_certs`` parameter must point
            to a file of CA certificates. Implies ``ssl=True``.
          - `ssl_ca_certs`: The ca_certs file contains a set of concatenated
            "certification authority" certificates, which are used to validate
            certificates passed from the other end of the connection.
            Implies ``ssl=True``.
          - `wait_queue_timeout`: (integer) How long (in milliseconds) a
            thread will wait for a socket from the pool if the pool has no
            free sockets.
          - `wait_queue_multiple`: (integer) Multiplied by max_pool_size to give
            the number of threads allowed to wait for a socket at one time.
        """
        if use_greenlets and not thread_util.have_greenlet:
            raise ConfigurationError(
                "The greenlet module is not available. "
                "Install the greenlet package from PyPI."
            )

        self.use_greenlets = use_greenlets
        self.sockets = set()
        self.lock = threading.Lock()

        # Keep track of resets, so we notice sockets created before the most
        # recent reset and close them.
        self.pool_id = 0
        self.pid = os.getpid()
        self.pair = pair
        self.max_size = max_size
        self.net_timeout = net_timeout
        self.conn_timeout = conn_timeout
        self.wait_queue_timeout = wait_queue_timeout
        self.wait_queue_multiple = wait_queue_multiple
        self.use_ssl = use_ssl
        self.ssl_keyfile = ssl_keyfile
        self.ssl_certfile = ssl_certfile
        self.ssl_cert_reqs = ssl_cert_reqs
        self.ssl_ca_certs = ssl_ca_certs

        if HAS_SSL and use_ssl and not ssl_cert_reqs:
            self.ssl_cert_reqs = ssl.CERT_NONE

        self._ident = thread_util.create_ident(self.use_greenlets)

        # Map self._ident.get() -> request socket
        self._tid_to_sock = {}

        # Count the number of calls to start_request() per thread or greenlet
        self._request_counter = thread_util.Counter(self.use_greenlets)

        if self.wait_queue_multiple is None:
            max_waiters = None
        else:
            max_waiters = self.max_size * self.wait_queue_multiple

        if self.max_size is None:
            self._socket_semaphore = thread_util.DummySemaphore()
        elif self.use_greenlets:
            if max_waiters is None:
                self._socket_semaphore = gevent.coros.BoundedSemaphore(
                    self.max_size)
            else:
                self._socket_semaphore = (
                    thread_util.MaxWaitersBoundedSemaphoreGevent(
                    self.max_size, max_waiters))
        else:
            if max_waiters is None:
                self._socket_semaphore = thread_util.BoundedSemaphore(
                    self.max_size)
            else:
                self._socket_semaphore = (
                    thread_util.MaxWaitersBoundedSemaphoreThread(
                        self.max_size, max_waiters))

        self._poolrefs = {}
        if self.net_timeout:
            # Start the monitor after we know the configuration is correct.
            if self.use_greenlets:
                self.__monitor = MonitorGreenlet(self, self.net_timeout / 2000)
            else:
                self.__monitor = MonitorThread(self, 0.2)#self.net_timeout / 2000)
                self.__monitor.setDaemon(True)
            register_monitor(self.__monitor)
            self.__monitor.start()
        else:
            self.__monitor = None

    def reset(self):
        # Ignore this race condition -- if many threads are resetting at once,
        # the pool_id will definitely change, which is all we care about.
        self.pool_id += 1
        self.pid = os.getpid()

        sockets = None
        try:
            # Swapping variables is not atomic. We need to ensure no other
            # thread is modifying self.sockets, or replacing it, in this
            # critical section.
            self.lock.acquire()
            sockets, self.sockets = self.sockets, set()
        finally:
            self.lock.release()

        for sock_info in sockets:
            sock_info.close()

    def create_connection(self, pair):
        """Connect to *pair* and return the socket object.

        This is a modified version of create_connection from
        CPython >=2.6.
        """
        host, port = pair or self.pair

        # Check if dealing with a unix domain socket
        if host.endswith('.sock'):
            if not hasattr(socket, "AF_UNIX"):
                raise ConnectionFailure("UNIX-sockets are not supported "
                                        "on this system")
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.connect(host)
                return sock
            except socket.error, e:
                if sock is not None:
                    sock.close()
                raise e

        # Don't try IPv6 if we don't support it. Also skip it if host
        # is 'localhost' (::1 is fine). Avoids slow connect issues
        # like PYTHON-356.
        family = socket.AF_INET
        if socket.has_ipv6 and host != 'localhost':
            family = socket.AF_UNSPEC

        err = None
        for res in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
            af, socktype, proto, dummy, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(self.conn_timeout or 20.0)
                sock.connect(sa)
                return sock
            except socket.error, e:
                err = e
                if sock is not None:
                    sock.close()

        if err is not None:
            raise err
        else:
            # This likely means we tried to connect to an IPv6 only
            # host with an OS/kernel or Python interpreter that doesn't
            # support IPv6. The test case is Jython2.5.1 which doesn't
            # support IPv6 at all.
            raise socket.error('getaddrinfo failed')

    def connect(self, pair):
        """Connect to Mongo and return a new (connected) socket. Note that the
           pool does not keep a reference to the socket -- you must call
           return_socket() when you're done with it.
        """
        sock = self.create_connection(pair)
        hostname = (pair or self.pair)[0]

        if self.use_ssl:
            try:
                sock = ssl.wrap_socket(sock,
                                       certfile=self.ssl_certfile,
                                       keyfile=self.ssl_keyfile,
                                       ca_certs=self.ssl_ca_certs,
                                       cert_reqs=self.ssl_cert_reqs)
                if self.ssl_cert_reqs:
                    match_hostname(sock.getpeercert(), hostname)

            except ssl.SSLError:
                sock.close()
                raise ConnectionFailure("SSL handshake failed. MongoDB may "
                                        "not be configured with SSL support.")

        sock.settimeout(self.net_timeout)
        return SocketInfo(sock, self.pool_id, hostname, self)

    def get_socket(self, pair=None, force=False):
        """Get a socket from the pool.

        Returns a :class:`SocketInfo` object wrapping a connected
        :class:`socket.socket`, and a bool saying whether the socket was from
        the pool or freshly created.

        :Parameters:
          - `pair`: optional (hostname, port) tuple
          - `force`: optional boolean, forces a connection to be returned
              without blocking, even if `max_size` has been reached.
        """
        # We use the pid here to avoid issues with fork / multiprocessing.
        # See test.test_client:TestClient.test_fork for an example of
        # what could go wrong otherwise
        if self.pid != os.getpid():
            self.reset()

        # Have we opened a socket for this request?
        req_state = self._get_request_state()
        if req_state not in (NO_SOCKET_YET, NO_REQUEST):
            # There's a socket for this request, check it and return it
            checked_sock = self._check(req_state, pair, acquire_on_connect=True)
            if checked_sock != req_state:
                self._set_request_state(checked_sock)

            checked_sock.last_checkout = time.time()
            return checked_sock

        forced = False
        # We're not in a request, just get any free socket or create one
        if force:
            # If we're doing an internal operation, attempt to play nicely with
            # max_size, but if there is no open "slot" force the connection
            # and mark it as forced so we don't release the semaphore without
            # having acquired it for this socket.
            if not self._socket_semaphore.acquire(False):
                forced = True
        elif not self._socket_semaphore.acquire(True, self.wait_queue_timeout):
            raise socket.timeout()
        sock_info, from_pool = None, None
        try:
            try:
                # set.pop() isn't atomic in Jython less than 2.7, see
                # http://bugs.jython.org/issue1854
                self.lock.acquire()
                sock_info, from_pool = self.sockets.pop(), True
            finally:
                self.lock.release()
        except KeyError:
            sock_info, from_pool = self.connect(pair), False

        if from_pool:
            sock_info = self._check(sock_info, pair)

        sock_info.forced = forced

        if req_state == NO_SOCKET_YET:
            # start_request has been called but we haven't assigned a socket to
            # the request yet. Let's use this socket for this request until
            # end_request.
            self._set_request_state(sock_info)

        sock_info.last_checkout = time.time()
        return sock_info

    __get_socket = get_socket
    def _get_socket(self, pair=None, force=False):
        sock = self.__get_socket(pair, force)
        log.info('[%r] Pool.get_socket got %r', self._ident.get(), sock)
        return sock
    get_socket = _get_socket

    def start_request(self):
        log.info('[%r] Pool.start_request', self._ident.get())
        if self._get_request_state() == NO_REQUEST:
            # Add a placeholder value so we know we're in a request, but we
            # have no socket assigned to the request yet.
            self._set_request_state(NO_SOCKET_YET)

        self._request_counter.inc()

    def in_request(self):
        return bool(self._request_counter.get())

    def end_request(self):
        # TODO(reversefold): Is this needed? tid is never used
        tid = self._ident.get()

        log.info('[%r] Pool.end_request', tid)

        # Check if start_request has ever been called in this thread / greenlet
        count = self._request_counter.get()
        if count:
            self._request_counter.dec()
            if count == 1:
                # End request
                sock_info = self._get_request_state()
                self._set_request_state(NO_REQUEST)
                if sock_info not in (NO_REQUEST, NO_SOCKET_YET):
                    self._return_socket(sock_info)

    def refresh(self):
        self.__monitor.schedule_refresh()

    def check_request_socks(self, force=False):
        log.info('Pool.check_request_socks')
        now = time.time()
        for tid in self._tid_to_sock.keys():
            sock_info = self._tid_to_sock.get(tid, None)
            if sock_info in (None, NO_REQUEST, NO_SOCKET_YET):
                continue
            log.info('[%r] Checking %r %r %r', tid, sock_info, now - sock_info.last_checkout, self.net_timeout)
            if now - sock_info.last_checkout > self.net_timeout:
                log.info('[%r] Socket has not been used for more than %r, closing %r', tid, self.net_timeout, sock_info)
                # Assuming that the thread has died but is failing to call
                # on_thread_died, close and return its socket to the pool
                sock_info.close()
                self.maybe_return_socket(sock_info, tid=tid)
                self._tid_to_sock[tid] = NO_SOCKET_YET

    def discard_socket(self, sock_info):
        """Close and discard the active socket.
        """
        if sock_info not in (NO_REQUEST, NO_SOCKET_YET):
            sock_info.close()

            if sock_info == self._get_request_state():
                # Discarding request socket; prepare to use a new request
                # socket on next get_socket().
                self._set_request_state(NO_SOCKET_YET)

    def maybe_return_socket(self, sock_info, tid=None):
        """Return the socket to the pool unless it's the request socket.
        """
#        # Catch the case where a socket has already been returned to the pool
#        # when SocketInfo.__del__ is called
#        try:
#            self.lock.acquire()
#            if sock_info in self.sockets:
#                return
#        finally:
#            self.lock.release()

        if self.pid != os.getpid():
            if not sock_info.forced:
                self._socket_semaphore.release()
            self.reset()
        elif sock_info not in (NO_REQUEST, NO_SOCKET_YET):
            if sock_info.closed:
                if tid is None:
                    tid = self._ident.get()
                if sock_info.forced:
                    log.info('[%r] maybe_return_socket sock_info closed and forced %r', tid, sock_info)
                    sock_info.forced = False
                else:
                    log.info('[%r] maybe_return_socket sock_info closed, releasing semaphore %r', tid, sock_info)
                    self._socket_semaphore.release()
                return

            if sock_info != self._get_request_state():
                self._return_socket(sock_info)

    def _return_socket(self, sock_info, tid=None):
        """Return socket to the pool. If pool is full the socket is discarded.
        """
        if tid is None:
            tid = self._ident.get()
        log.info('[%r] Pool._return_socket returning %r', tid, sock_info)
        try:
            self.lock.acquire()
            if (len(self.sockets) < self.max_size
                and sock_info.pool_id == self.pool_id
            ):
                self.sockets.add(sock_info)
            else:
                sock_info.close()
        finally:
            self.lock.release()

        if sock_info.forced:
            sock_info.forced = False
        else:
            self._socket_semaphore.release()

    def _check(self, sock_info, pair, acquire_on_connect=False):
        """This side-effecty function checks if this pool has been reset since
        the last time this socket was used, or if the socket has been closed by
        some external network error, and if so, attempts to create a new socket.
        If this connection attempt fails we reset the pool and reraise the
        error.

        Checking sockets lets us avoid seeing *some*
        :class:`~pymongo.errors.AutoReconnect` exceptions on server
        hiccups, etc. We only do this if it's been > 1 second since
        the last socket checkout, to keep performance reasonable - we
        can't avoid AutoReconnects completely anyway.
        """
        error = False

        if sock_info.closed:
            error = True

        elif self.pool_id != sock_info.pool_id:
            sock_info.close()
            error = True

        elif time.time() - sock_info.last_checkout > 1:
            if _closed(sock_info.sock):
                sock_info.close()
                error = True

        if not error:
            return sock_info
        else:
            try:
                if acquire_on_connect:
                    if not self._socket_semaphore.acquire(True, self.wait_queue_timeout):
                        raise socket.timeout()
                return self.connect(pair)
            except socket.error:
                self.reset()
                raise

    def _set_request_state(self, sock_info):
        ident = self._ident
        tid = ident.get()
        import thread
        tident = thread.get_ident()

        if sock_info == NO_REQUEST:
            # Ending a request
            ident.unwatch()
            self._tid_to_sock.pop(tid, None)
        else:
            self._tid_to_sock[tid] = sock_info

            if not ident.watching():
                log.info('[%r] Pool._set_request_state Not yet watching thread', tid)
                # Closure over tid, poolref, and ident. Don't refer directly to
                # self, otherwise there's a cycle.

                # Do not access threadlocals in this function, or any
                # function it calls! In the case of the Pool subclass and
                # mod_wsgi 2.x, on_thread_died() is triggered when mod_wsgi
                # calls PyThreadState_Clear(), which deferences the
                # ThreadVigil and triggers the weakref callback. Accessing
                # thread locals in this function, while PyThreadState_Clear()
                # is in progress can cause leaks, see PYTHON-353.
                poolref = weakref.ref(self)
                self._poolrefs.setdefault(ident.get(), []).append(poolref)

                def on_thread_died(ref):
                    try:
                        log.info('[%r] [%r] on_thread_died', tid, tident)
                        ident.unwatch(tid)
                        pool = poolref()
                        if pool:
                            log.info('[%r] on_thread_died pool active', tid)
                            # End the request
                            request_sock = pool._tid_to_sock.pop(tid, None)

                            # Was thread ever assigned a socket before it died?
                            if request_sock not in (NO_REQUEST, NO_SOCKET_YET):
                                log.info('[%r] on_thread_died returning request_sock %r', tid, request_sock)
                                pool._return_socket(request_sock, tid)
                            else:
                                log.info('[%r] on_thread_died sock is %s', tid, 'NO_REQUEST' if request_sock == NO_REQUEST else 'NO_SOCKET_YET')

                    except:
                        # Random exceptions on interpreter shutdown.
                        try:
                            log.info('[%r] Exception in on_thread_died', tid)
                            import traceback
                            traceback.print_exc()
                        except:
                            pass
                        pass

                ident.watch(on_thread_died)
            else:
                log.info('[%r] Pool._set_request_state Already watching thread', tid)

    def _get_request_state(self):
        tid = self._ident.get()
        return self._tid_to_sock.get(tid, NO_REQUEST)

    def __del__(self):
        # Avoid ResourceWarnings in Python 3
        for sock_info in self.sockets:
            sock_info.close()

        for request_sock in self._tid_to_sock.values():
            if request_sock not in (NO_REQUEST, NO_SOCKET_YET):
                request_sock.close()


class Request(object):
    """
    A context manager returned by :meth:`start_request`, so you can do
    `with client.start_request(): do_something()` in Python 2.5+.
    """
    def __init__(self, connection):
        self.connection = connection

    def end(self):
        self.connection.end_request()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end()
        # Returning False means, "Don't suppress exceptions if any were
        # thrown within the block"
        return False
