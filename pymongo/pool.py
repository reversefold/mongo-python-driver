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

import os
import socket
import sys
import time
import threading
import weakref

from pymongo import thread_util
from pymongo.common import HAS_SSL
from pymongo.errors import ConnectionFailure, ConfigurationError

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


import logging
log = logging.getLogger(__name__)


NO_REQUEST = None
NO_SOCKET_YET = -1


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
    def __init__(self, sock, pool_id, host=None):
        self.sock = sock
        self.host = host
        self.authset = set()
        self.closed = False
        self.last_checkout = time.time()
        self.forced = False

        # The pool's pool_id changes with each reset() so we can close sockets
        # created before the last reset.
        self.pool_id = pool_id

    def close(self):
        log.info('SocketInfo.close %r', self)
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


# Do *not* explicitly inherit from object or Jython won't call __del__
# http://bugs.jython.org/issue1057
class Pool:
    def __init__(self, pair, max_size, net_timeout, conn_timeout, use_ssl,
                 use_greenlets, ssl_keyfile=None, ssl_certfile=None,
                 ssl_cert_reqs=None, ssl_ca_certs=None):
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

        if self.max_size is None:
            self._socket_semaphore = thread_util.DummySemaphore()
        else:
            self._socket_semaphore = thread_util.BoundedSemaphore(
                self.max_size, self.use_greenlets)

        self.outstanding_socks = set()

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

        if self.max_size is None:
            self._socket_semaphore = thread_util.DummySemaphore()
        else:
            self._socket_semaphore = thread_util.BoundedSemaphore(
                self.max_size, self.use_greenlets)

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
        log.info('Connected a new socket: %r', sock)
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
        return SocketInfo(sock, self.pool_id, hostname)

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
        tid = self._ident.get()

        if self.pid != os.getpid():
            log.info('[%r] Pid change, resetting pool', tid)
            self.reset()

        # Have we opened a socket for this request?
        req_state = self._get_request_state()
        if req_state not in (NO_SOCKET_YET, NO_REQUEST):
            log.info('[%r] request in progress: req_state %r', tid, req_state)
            # There's a socket for this request, check it and return it
            checked_sock = self._check(req_state, pair, acquire_on_connect=True)
            log.info('[%r] checked_sock: %r', tid, checked_sock)
            if checked_sock != req_state:
                log.info('[%r] checked_sock != req_state', tid)
                self._set_request_state(checked_sock)

            checked_sock.last_checkout = time.time()
            if checked_sock in self.outstanding_socks:
                raise Exception()
            self.outstanding_socks.add(checked_sock)
            log.info('[%r] returning socket %r', tid, checked_sock)
            return checked_sock

        forced = False
        # We're not in a request, just get any free socket or create one
        if force:
            # If we're doing an internal operation, attempt to play nicely with
            # max_size, but if there is no open "slot" force the connection
            # and mark it as forced so we don't release the semaphore without
            # having acquired it for this socket.
            if not self._socket_semaphore.acquire(False):
                log.info('[%r] force=True and could not acquire socket semaphore, forcing a new socket', tid)
                forced = True
        elif not self._socket_semaphore.acquire(True, self.conn_timeout):
            log.info('[%r] Could not acquire socket semaphore in %r', tid, self.conn_timeout)
            raise socket.timeout()
        sock_info, from_pool = None, None
        try:
            try:
                # set.pop() isn't atomic in Jython less than 2.7, see
                # http://bugs.jython.org/issue1854
                log.info('[%r] Getting sock from pool', tid)
                self.lock.acquire()
                sock_info, from_pool = self.sockets.pop(), True
                log.info('[%r] Got sock from pool: %r', tid, sock_info)
            finally:
                self.lock.release()
        except KeyError:
            log.info('[%r] Pool queue empty, making a new connection', tid)
            sock_info, from_pool = self.connect(pair), False
            log.info('[%r] new connection: %r', tid, sock_info)

        if from_pool:
            log.info('[%r] Checking pool socket: %r', tid, sock_info)
            sock_info = self._check(sock_info, pair)
            log.info('[%r] Checked pool socket: %r', tid, sock_info)

        sock_info.forced = forced

        if req_state == NO_SOCKET_YET:
            # start_request has been called but we haven't assigned a socket to
            # the request yet. Let's use this socket for this request until
            # end_request.
            log.info('[%r] Setting socket to req_state: %r', tid, sock_info)
            self._set_request_state(sock_info)

        sock_info.last_checkout = time.time()
        self.outstanding_socks.add(sock_info)
        log.info('[%r] returning socket %r', tid, sock_info)
        return sock_info

    def start_request(self):
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

    def discard_socket(self, sock_info):
        """Close and discard the active socket.
        """
        if sock_info not in (NO_REQUEST, NO_SOCKET_YET):
            sock_info.close()

            if sock_info == self._get_request_state():
                # Discarding request socket; prepare to use a new request
                # socket on next get_socket().
                self._set_request_state(NO_SOCKET_YET)

    def maybe_return_socket(self, sock_info):
        """Return the socket to the pool unless it's the request socket.
        """
        tid = self._ident.get()
        log.info('[%r] maybe_return_socket: %r', tid, sock_info)
#        import traceback
#        log.info(''.join(traceback.format_stack()))
        if sock_info not in self.outstanding_socks:
            raise Exception()
        if self.pid != os.getpid():
            log.info('[%r] Pids do not match, resetting: %r', tid, sock_info)
            self.reset()
        elif sock_info not in (NO_REQUEST, NO_SOCKET_YET):
            log.info('[%r] Socket is real: %r %r %r %r', tid, sock_info, sock_info.closed, sock_info.closed and "CLOSED" or "OPEN", id(sock_info))
            if sock_info.closed:
                log.info('[%r] Closed socket being returned: %r', tid, sock_info)
                if (not sock_info.forced
                    and sock_info.pool_id == self.pool_id
                ):
                    log.info('[%r] Releasing semaphore: %r', tid, sock_info)
                    self._socket_semaphore.release()

                self.outstanding_socks.remove(sock_info)
                return

            if sock_info != self._get_request_state():
                log.info('[%r] Not request socket, returning to the pool: %r', tid, sock_info)
                self._return_socket(sock_info)
            else:
                log.info('[%r] Request socket, keeping: %r', tid, sock_info)

    def _return_socket(self, sock_info):
        """Return socket to the pool. If pool is full the socket is discarded.
        """
        tid = self._ident.get()
        log.info('[%r] _return_socket: %r', tid, sock_info)
        if sock_info not in self.outstanding_socks:
            raise Exception()
        self.outstanding_socks.remove(sock_info)
        if (len(self.sockets) < self.max_size
            and sock_info.pool_id == self.pool_id
        ):
            log.info('[%r] Room in the pool, returning %r', tid, sock_info)
            self.sockets.add(sock_info)
        else:
            log.info('[%r] Pool full, closing %r', tid, sock_info)
            sock_info.close()
        if sock_info.forced:
            log.info('[%r] socket was forced, setting unforced', tid)
            sock_info.forced = False
        else:
            log.info('[%r] Releasing socket semaphore', tid)
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
        tid = self._ident.get()
        error = False

        if sock_info.closed:
            log.info('[%r] _check, sock already closed', tid)
            error = True

        elif self.pool_id != sock_info.pool_id:
            log.info('[%r] _check, pool_id does not match', tid)
            sock_info.close()
            error = True

        elif time.time() - sock_info.last_checkout > 1:
            log.info('[%r] _check, time since last checkout > 1', tid)
            if _closed(sock_info.sock):
                log.info('[%r] _check, sock was closed, marking closed', tid)
                sock_info.close()
                error = True

        if not error:
            log.info('[%r] _check, sock ok', tid)
            return sock_info
        else:
            try:
                log.info('[%r] _check, sock dead, connecting a new one', tid)
                if acquire_on_connect:
                    if not self._socket_semaphore.acquire(True, self.conn_timeout):
                        raise socket.timeout()
                return self.connect(pair)
            except socket.error:
                self.reset()
                raise

    def _set_request_state(self, sock_info):
        tid = self._ident.get()
        log.info('[%r] _set_request_state %r', tid, sock_info)

        if sock_info == NO_REQUEST:
            # Ending a request
            self._ident.unwatch()
            self._tid_to_sock.pop(tid, None)
        else:
            self._tid_to_sock[tid] = sock_info

            if not self._ident.watching():
                log.info('[%r] Watching thread', tid)
                # Closure over tid and poolref. Don't refer directly to self,
                # otherwise there's a cycle.

                # Do not access threadlocals in this function, or any
                # function it calls! In the case of the Pool subclass and
                # mod_wsgi 2.x, on_thread_died() is triggered when mod_wsgi
                # calls PyThreadState_Clear(), which deferences the
                # ThreadVigil and triggers the weakref callback. Accessing
                # thread locals in this function, while PyThreadState_Clear()
                # is in progress can cause leaks, see PYTHON-353.
                poolref = weakref.ref(self)
                def on_thread_died(ref):
                    log.info('[%r] Thread died', tid)
                    try:
                        pool = poolref()
                        if pool:
                            log.info('[%r] Pool still alive on thread death: %r', tid, pool)
                            # End the request
                            request_sock = pool._tid_to_sock.pop(tid, None)

                            # Was thread ever assigned a socket before it died?
                            if request_sock not in (NO_REQUEST, NO_SOCKET_YET):
                                log.info('[%r] thread had a socket %r, returning to the pool', tid, request_sock)
                                pool._return_socket(request_sock)
                            else:
                                log.info('[%r] thread did not have a socket', tid)
                        else:
                            log.info('[%r] Pool already gone on thread death', tid)
                    except:
                        # Random exceptions on interpreter shutdown.
                        import traceback
                        log.info('[%r] Exception cleaning up after thread death: %r', tid, traceback.format_exc())

                self._ident.watch(on_thread_died)
            else:
                log.info('[%r] Already watching thread', tid)

    def _get_request_state(self):
        tid = self._ident.get()
        return self._tid_to_sock.get(tid, NO_REQUEST)

    def __del__(self):
        log.info('Pool.__del__: %r outstanding sockets: %r', self, self.outstanding_socks)
        log.info('Pool.__del__: %r sockets: %r', self, self.sockets)

        # Avoid ResourceWarnings in Python 3
        for sock_info in self.sockets:
            sock_info.close()

        for request_sock in self._tid_to_sock.values():
            if request_sock not in (NO_REQUEST, NO_SOCKET_YET):
                request_sock.close()
                # TODO(reversefold): Is this needed?
                # self._socket_semaphore.release()

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
