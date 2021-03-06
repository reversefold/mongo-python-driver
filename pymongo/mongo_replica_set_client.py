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

"""Tools for connecting to a MongoDB replica set.

.. seealso:: :doc:`/examples/high_availability` for more examples of
   how to connect to a replica set.

To get a :class:`~pymongo.database.Database` instance from a
:class:`MongoReplicaSetClient` use either dictionary-style or
attribute-style access:

.. doctest::

  >>> from pymongo import MongoReplicaSetClient
  >>> c = MongoReplicaSetClient('localhost:27017', replicaSet='repl0')
  >>> c.test_database
  Database(MongoReplicaSetClient([u'...', u'...']), u'test_database')
  >>> c['test_database']
  Database(MongoReplicaSetClient([u'...', u'...']), u'test_database')
"""

import atexit
import datetime
import socket
import struct
import time
import warnings
import weakref

from bson.py3compat import b
from pymongo import (auth,
                     common,
                     database,
                     helpers,
                     message,
                     pool,
                     thread_util,
                     uri_parser)
from pymongo.read_preferences import (
    ReadPreference, select_member, modes, MovingAverage)
from pymongo.errors import (AutoReconnect,
                            ConfigurationError,
                            ConnectionFailure,
                            DuplicateKeyError,
                            InvalidDocument,
                            OperationFailure)

EMPTY = b("")
MAX_BSON_SIZE = 4 * 1024 * 1024
MAX_RETRY = 3

# Member states
PRIMARY = 1
SECONDARY = 2
OTHER = 3

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

def _partition_node(node):
    """Split a host:port string returned from mongod/s into
    a (host, int(port)) pair needed for socket.connect().
    """
    host = node
    port = 27017
    idx = node.rfind(':')
    if idx != -1:
        host, port = node[:idx], int(node[idx + 1:])
    if host.startswith('['):
        host = host[1:-1]
    return host, port


# Concurrency notes: A MongoReplicaSetClient keeps its view of the replica-set
# state in an RSState instance. RSStates are immutable, except for
# host-pinning. Pools, which are internally thread / greenlet safe, can be
# copied from old to new RSStates safely. The client updates its view of the
# set's state not by modifying its RSState but by replacing it with an updated
# copy.

# In __init__, MongoReplicaSetClient gets a list of potential members called
# 'seeds' from its initial parameters, and calls refresh(). refresh() iterates
# over the the seeds in arbitrary order looking for a member it can connect to.
# Once it finds one, it calls 'ismaster' and sets self.__hosts to the list of
# members in the response, and connects to the rest of the members. refresh()
# sets the MongoReplicaSetClient's RSState. Finally, __init__ launches the
# replica-set monitor.

# The monitor calls refresh() every 30 seconds, or whenever the client has
# encountered an error that prompts it to wake the monitor.

# Every method that accesses the RSState multiple times within the method makes
# a local reference first and uses that throughout, so it's isolated from a
# concurrent method replacing the RSState with an updated copy. This technique
# avoids the need to lock around accesses to the RSState.


class RSState(object):
    def __init__(
            self, threadlocal, host_to_member=None, arbiters=None, writer=None,
            error_message='No primary available'):
        """An immutable snapshot of the client's view of the replica set state.

        :Parameters:
          - `threadlocal`: Thread- or greenlet-local storage
          - `host_to_member`: Optional dict: (host, port) -> Member instance
          - `arbiters`: Optional sequence of arbiters as (host, port)
          - `writer`: Optional (host, port) of primary
          - `error_message`: Optional error if `writer` is None
        """
        self._threadlocal = threadlocal  # threading.local or gevent local
        self._arbiters = frozenset(arbiters or [])  # set of (host, port)
        self._writer = writer  # (host, port) of the primary, or None
        self._error_message = error_message
        self._host_to_member = host_to_member or {}
        self._hosts = frozenset(self._host_to_member)
        self._members = frozenset(self._host_to_member.values())

        if writer and self._host_to_member[writer].up:
            self._primary_member = self._host_to_member[writer]
        else:
            self._primary_member = None

    def clone_with_host_down(self, host, error_message):
        """Get a clone, marking as "down" the member with the given (host, port)
        """
        members = self._host_to_member.copy()
        down_member = members.pop(host, None)
        if down_member:
            members[host] = down_member.clone_down()

        if host == self.writer:
            # The primary went down; record the error message.
            return RSState(
                self._threadlocal, members, self._arbiters,
                None, error_message)
        else:
            # Some other host went down. Keep our current primary or, if it's
            # already down, keep our current error message.
            return RSState(
                self._threadlocal, members, self._arbiters,
                self._writer, self._error_message)

    def clone_without_writer(self, threadlocal):
        """Get a clone without a primary. Unpins all threads.

        :Parameters:
          - `threadlocal`: Thread- or greenlet-local storage
        """
        return RSState(
            threadlocal, self._host_to_member.copy(), self._arbiters, None)

    @property
    def arbiters(self):
        """Set of (host, port) pairs."""
        return self._arbiters

    @property
    def writer(self):
        """(host, port) of primary, or None."""
        return self._writer

    @property
    def primary_member(self):
        return self._primary_member

    @property
    def hosts(self):
        """Set of (host, port) tuples of data members of the replica set."""
        return self._hosts

    @property
    def members(self):
        """Set of Member instances."""
        return self._members

    @property
    def error_message(self):
        """The error, if any, raised when trying to connect to the primary"""
        return self._error_message

    @property
    def secondaries(self):
        """Set of (host, port) pairs."""
        # Unlike the other properties, this isn't cached because it isn't used
        # in regular operations.
        return set([
            host for host, member in self._host_to_member.items()
            if member.is_secondary])

    def get(self, host):
        """Return a Member instance or None for the given (host, port)."""
        return self._host_to_member.get(host)

    def pin_host(self, host, mode, tag_sets, latency):
        """Pin this thread / greenlet to a member.

        `host` is a (host, port) pair. The remaining parameters are a read
        preference.
        """
        # Fun fact: Unlike in thread_util.ThreadIdent, we needn't lock around
        # assignment here. Assignment to a threadlocal is only unsafe if it
        # can cause other Python code to run implicitly.
        self._threadlocal.host = host
        self._threadlocal.read_preference = (mode, tag_sets, latency)

    def keep_pinned_host(self, mode, tag_sets, latency):
        """Does a read pref match the last used by this thread / greenlet?"""
        return self._threadlocal.read_preference == (mode, tag_sets, latency)

    @property
    def pinned_host(self):
        """The (host, port) last used by this thread / greenlet, or None."""
        return getattr(self._threadlocal, 'host', None)

    def unpin_host(self):
        """Forget this thread / greenlet's last used member."""
        self._threadlocal.host = self._threadlocal.read_preference = None

    @property
    def threadlocal(self):
        return self._threadlocal

    def __str__(self):
        return '<RSState [%s] writer="%s">' % (
            ', '.join(str(member) for member in self._host_to_member.itervalues()),
            self.writer and '%s:%s' % self.writer or None)


class Monitor(object):
    """Base class for replica set monitors.
    """
    _refresh_interval = 30

    def __init__(self, rsc, event_class):
        self.rsc = weakref.proxy(rsc, self.shutdown)
        self.event = event_class()
        self.refreshed = event_class()
        self.stopped = False

    def shutdown(self, dummy=None):
        """Signal the monitor to shutdown.
        """
        self.stopped = True
        self.event.set()

    def schedule_refresh(self):
        """Refresh immediately
        """
        self.refreshed.clear()
        self.event.set()

    def wait_for_refresh(self, timeout_seconds):
        """Block until a scheduled refresh completes
        """
        self.refreshed.wait(timeout_seconds)

    def monitor(self):
        """Run until the RSC is collected or an
        unexpected error occurs.
        """
        while True:
            self.event.wait(Monitor._refresh_interval)
            if self.stopped:
                break
            self.event.clear()

            try:
                try:
                    self.rsc.refresh(force=True)
                finally:
                    self.refreshed.set()
            except AutoReconnect:
                pass

            # RSC has been collected or there
            # was an unexpected error.
            except:
                break


class Member(object):
    """Immutable representation of one member of a replica set.

    :Parameters:
      - `host`: A (host, port) pair
      - `connection_pool`: A Pool instance
      - `ismaster_response`: A dict, MongoDB's ismaster response
      - `ping_time`: A MovingAverage instance
      - `up`: Whether we think this member is available
    """
    # For unittesting only. Use under no circumstances!
    _host_to_ping_time = {}

    def __init__(self, host, connection_pool, ismaster_response, ping_time, up):
        self.host = host
        self.pool = connection_pool
        self.ismaster_response = ismaster_response
        self.ping_time = ping_time
        self.up = up

        if ismaster_response['ismaster']:
            self.state = PRIMARY
        elif ismaster_response.get('secondary'):
            self.state = SECONDARY
        else:
            self.state = OTHER

        self.tags = ismaster_response.get('tags', {})
        self.max_bson_size = ismaster_response.get(
            'maxBsonObjectSize', MAX_BSON_SIZE)

    def clone_with(self, ismaster_response, ping_time_sample):
        """Get a clone updated with ismaster response and a single ping time.
        """
        ping_time = self.ping_time.clone_with(ping_time_sample)
        return Member(self.host, self.pool, ismaster_response, ping_time, True)

    def clone_down(self):
        """Get a clone of this Member, but with up=False.
        """
        return Member(
            self.host, self.pool, self.ismaster_response, self.ping_time,
            False)

    @property
    def is_primary(self):
        return self.state == PRIMARY

    @property
    def is_secondary(self):
        return self.state == SECONDARY

    def get_avg_ping_time(self):
        """Get a moving average of this member's ping times.
        """
        if self.host in Member._host_to_ping_time:
            # Simulate ping times for unittesting
            return Member._host_to_ping_time[self.host]

        return self.ping_time.get()

    def matches_mode(self, mode):
        if mode == ReadPreference.PRIMARY and not self.is_primary:
            return False

        if mode == ReadPreference.SECONDARY and not self.is_secondary:
            return False

        # If we're not primary or secondary, then we're in a state like
        # RECOVERING and we don't match any mode
        return self.is_primary or self.is_secondary

    def matches_tags(self, tags):
        """Return True if this member's tags are a superset of the passed-in
           tags. E.g., if this member is tagged {'dc': 'ny', 'rack': '1'},
           then it matches {'dc': 'ny'}.
        """
        for key, value in tags.items():
            if key not in self.tags or self.tags[key] != value:
                return False

        return True

    def matches_tag_sets(self, tag_sets):
        """Return True if this member matches any of the tag sets, e.g.
           [{'dc': 'ny'}, {'dc': 'la'}, {}]
        """
        for tags in tag_sets:
            if self.matches_tags(tags):
                return True

        return False

    def __str__(self):
        return '<Member "%s:%s" primary=%r up=%r>' % (
            self.host[0], self.host[1], self.is_primary, self.up)


class MongoReplicaSetClient(common.BaseObject):
    """Connection to a MongoDB replica set.
    """

    def __init__(self, hosts_or_uri=None, max_pool_size=10,
                 document_class=dict, tz_aware=False, _connect=True, **kwargs):
        """Create a new connection to a MongoDB replica set.

        The resultant client object has connection-pooling built
        in. It also performs auto-reconnection when necessary. If an
        operation fails because of a connection error,
        :class:`~pymongo.errors.ConnectionFailure` is raised. If
        auto-reconnection will be performed,
        :class:`~pymongo.errors.AutoReconnect` will be
        raised. Application code should handle this exception
        (recognizing that the operation failed) and then continue to
        execute.

        Raises :class:`~pymongo.errors.ConnectionFailure` if
        the connection cannot be made.

        The `hosts_or_uri` parameter can be a full `mongodb URI
        <http://dochub.mongodb.org/core/connections>`_, in addition to
        a string of `host:port` pairs (e.g. 'host1:port1,host2:port2').
        If `hosts_or_uri` is None 'localhost:27017' will be used.

        .. note:: Instances of :class:`MongoReplicaSetClient` start a
           background task to monitor the state of the replica set. This allows
           it to quickly respond to changes in replica set configuration.
           Before discarding an instance of :class:`MongoReplicaSetClient` make
           sure you call :meth:`~close` to ensure that the monitor task is
           cleanly shut down.

        :Parameters:
          - `hosts_or_uri` (optional): A MongoDB URI or string of `host:port`
            pairs. If a host is an IPv6 literal it must be enclosed in '[' and
            ']' characters following the RFC2732 URL syntax (e.g. '[::1]' for
            localhost)
          - `max_pool_size` (optional): The maximum number of idle connections
            to keep open in each pool for future use
          - `document_class` (optional): default class to use for
            documents returned from queries on this client
          - `tz_aware` (optional): if ``True``,
            :class:`~datetime.datetime` instances returned as values
            in a document by this :class:`MongoReplicaSetClient` will be timezone
            aware (otherwise they will be naive)
          - `replicaSet`: (required) The name of the replica set to connect to.
            The driver will verify that each host it connects to is a member of
            this replica set. Can be passed as a keyword argument or as a
            MongoDB URI option.

          | **Other optional parameters can be passed as keyword arguments:**

          - `host`: For compatibility with :class:`~mongo_client.MongoClient`.
            If both `host` and `hosts_or_uri` are specified `host` takes
            precedence.
          - `port`: For compatibility with :class:`~mongo_client.MongoClient`.
            The default port number to use for hosts.
          - `socketTimeoutMS`: (integer) How long (in milliseconds) a send or
            receive on a socket can take before timing out.
          - `connectTimeoutMS`: (integer) How long (in milliseconds) a
            connection can take to be opened before timing out.
          - `auto_start_request`: If ``True``, each thread that accesses
            this :class:`MongoReplicaSetClient` has a socket allocated to it
            for the thread's lifetime, for each member of the set. For
            :class:`~pymongo.read_preferences.ReadPreference` PRIMARY,
            auto_start_request=True ensures consistent reads, even if you read
            after an unacknowledged write. For read preferences other than
            PRIMARY, there are no consistency guarantees. Default to ``False``.
          - `use_greenlets`: If ``True``, use a background Greenlet instead of
            a background thread to monitor state of replica set. Additionally,
            :meth:`start_request()` assigns a greenlet-local, rather than
            thread-local, socket.
            `use_greenlets` with :class:`MongoReplicaSetClient` requires
            `Gevent <http://gevent.org/>`_ to be installed.
            DEPRECATED in favor of `thread_support_module`.
          - `thread_support_module`: ``threading``, ``gevent``, or a module
            which implements the necessary interface. Defaults to ``threading``.
            See :module: `~pymongo.thread_util_threading`.

          | **Write Concern options:**

          - `w`: (integer or string) Write operations will block until they have
            been replicated to the specified number or tagged set of servers.
            `w=<int>` always includes the replica set primary (e.g. w=3 means
            write to the primary and wait until replicated to **two**
            secondaries). Passing w=0 **disables write acknowledgement** and all
            other write concern options.
          - `wtimeout`: (integer) Used in conjunction with `w`. Specify a value
            in milliseconds to control how long to wait for write propagation
            to complete. If replication does not complete in the given
            timeframe, a timeout exception is raised.
          - `j`: If ``True`` block until write operations have been committed
            to the journal. Ignored if the server is running without journaling.
          - `fsync`: If ``True`` force the database to fsync all files before
            returning. When used with `j` the server awaits the next group
            commit before returning.

          | **Read preference options:**

          - `read_preference`: The read preference for this client.
            See :class:`~pymongo.read_preferences.ReadPreference` for available
            options.
          - `tag_sets`: Read from replica-set members with these tags.
            To specify a priority-order for tag sets, provide a list of
            tag sets: ``[{'dc': 'ny'}, {'dc': 'la'}, {}]``. A final, empty tag
            set, ``{}``, means "read from any member that matches the mode,
            ignoring tags." :class:`MongoReplicaSetClient` tries each set of
            tags in turn until it finds a set of tags with at least one matching
            member.
          - `secondary_acceptable_latency_ms`: (integer) Any replica-set member
            whose ping time is within secondary_acceptable_latency_ms of the
            nearest member may accept reads. Default 15 milliseconds.
            **Ignored by mongos** and must be configured on the command line.
            See the localThreshold_ option for more information.

          | **SSL configuration:**

          - `ssl`: If ``True``, create the connection to the servers using SSL.
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

        .. versionchanged:: 2.5
           Added additional ssl options
        .. versionadded:: 2.4

        .. _localThreshold: http://docs.mongodb.org/manual/reference/mongos/#cmdoption-mongos--localThreshold
        """
        self.__opts = {}
        self.__seeds = set()
        self.__index_cache = {}
        self.__auth_credentials = {}

        self.__max_pool_size = common.validate_positive_integer(
                                        'max_pool_size', max_pool_size)
        self.__tz_aware = common.validate_boolean('tz_aware', tz_aware)
        self.__document_class = document_class
        self.__monitor = None

        # Compatibility with mongo_client.MongoClient
        host = kwargs.pop('host', hosts_or_uri)

        port = kwargs.pop('port', 27017)
        if not isinstance(port, int):
            raise TypeError("port must be an instance of int")

        username = None
        db_name = None
        options = {}
        if host is None:
            self.__seeds.add(('localhost', port))
        elif '://' in host:
            res = uri_parser.parse_uri(host, port)
            self.__seeds.update(res['nodelist'])
            username = res['username']
            password = res['password']
            db_name = res['database']
            options = res['options']
        else:
            self.__seeds.update(uri_parser.split_hosts(host, port))

        # _pool_class and _monitor_class are for deep customization of PyMongo,
        # e.g. Motor. SHOULD NOT BE USED BY DEVELOPERS EXTERNAL TO 10GEN.
        self.pool_class = kwargs.pop('_pool_class', pool.Pool)
        monitor_class = kwargs.pop('_monitor_class', None)

        if 'use_greenlets' in kwargs and 'thread_support_module' in kwargs:
            raise ConfigurationError('Only one of use_greenlets and '
                                     'thread_module_support may be used')

        if kwargs.get('use_greenlets') is not None:
            if kwargs['use_greenlets']:
                kwargs['thread_support_module'] = 'gevent'
            else:
                kwargs['thread_support_module'] = 'threading'
        else:
            kwargs.setdefault('thread_support_module', 'threading')

        for option, value in kwargs.iteritems():
            option, value = common.validate(option, value)
            self.__opts[option] = value
        self.__opts.update(options)

        self.__use_greenlets = self.__opts.get('use_greenlets', None)
        # default is set above
        self.__thread_support_module = self.__opts['thread_support_module']

        self.__rs_state = RSState(self.__thread_support_module.local())

        self.__request_counter = thread_util.Counter(self.__thread_support_module)

        self.__auto_start_request = self.__opts.get('auto_start_request', False)
        if self.__auto_start_request:
            self.start_request()

        self.__name = self.__opts.get('replicaset')
        if not self.__name:
            raise ConfigurationError("the replicaSet "
                                     "keyword parameter is required.")

        self.__net_timeout = self.__opts.get('sockettimeoutms')
        self.__conn_timeout = self.__opts.get('connecttimeoutms')
        self.__use_ssl = self.__opts.get('ssl', None)
        self.__ssl_keyfile = self.__opts.get('ssl_keyfile', None)
        self.__ssl_certfile = self.__opts.get('ssl_certfile', None)
        self.__ssl_cert_reqs = self.__opts.get('ssl_cert_reqs', None)
        self.__ssl_ca_certs = self.__opts.get('ssl_ca_certs', None)

        ssl_kwarg_keys = [k for k in kwargs.keys() if k.startswith('ssl_')]
        if not self.__use_ssl and ssl_kwarg_keys:
            raise ConfigurationError("ssl has not been enabled but the "
                                     "following ssl parameters have been set: "
                                     "%s. Please set `ssl=True` or remove."
                                     % ', '.join(ssl_kwarg_keys))

        if self.__ssl_cert_reqs and not self.__ssl_ca_certs:
                raise ConfigurationError("If `ssl_cert_reqs` is not "
                                         "`ssl.CERT_NONE` then you must "
                                         "include `ssl_ca_certs` to be able "
                                         "to validate the server.")

        if ssl_kwarg_keys and self.__use_ssl is None:
            # ssl options imply ssl = True
            self.__use_ssl = True

        if self.__use_ssl and not common.HAS_SSL:
            raise ConfigurationError("The ssl module is not available. If you "
                                     "are using a python version previous to "
                                     "2.6 you must install the ssl package "
                                     "from PyPI.")

        super(MongoReplicaSetClient, self).__init__(**self.__opts)
        if self.slave_okay:
            warnings.warn("slave_okay is deprecated. Please "
                          "use read_preference instead.", DeprecationWarning,
                          stacklevel=2)

        if _connect:
            try:
                self.refresh()
            except AutoReconnect, e:
                # ConnectionFailure makes more sense here than AutoReconnect
                raise ConnectionFailure(str(e))

        db_name = options.get('authsource', db_name)
        if db_name and username is None:
            warnings.warn("database name or authSource in URI is being "
                          "ignored. If you wish to authenticate to %s, you "
                          "must provide a username and password." % (db_name,))
        if username:
            mechanism = options.get('authmechanism', 'MONGODB-CR')
            if mechanism == 'GSSAPI':
                source = '$external'
            else:
                source = db_name or 'admin'
            credentials = (source, unicode(username),
                           unicode(password), mechanism)
            try:
                self._cache_credentials(source, credentials, _connect)
            except OperationFailure, exc:
                raise ConfigurationError(str(exc))

        # Start the monitor after we know the configuration is correct.
        if monitor_class:
            self.__monitor = monitor_class(self)
        else:
            self.__monitor = self.__thread_support_module.ReplSetMonitor(self)
        register_monitor(self.__monitor)

        if _connect:
            self.__monitor.start()

    def _cached(self, dbname, coll, index):
        """Test if `index` is cached.
        """
        cache = self.__index_cache
        now = datetime.datetime.utcnow()
        return (dbname in cache and
                coll in cache[dbname] and
                index in cache[dbname][coll] and
                now < cache[dbname][coll][index])

    def _cache_index(self, dbase, collection, index, cache_for):
        """Add an index to the index cache for ensure_index operations.
        """
        now = datetime.datetime.utcnow()
        expire = datetime.timedelta(seconds=cache_for) + now

        if dbase not in self.__index_cache:
            self.__index_cache[dbase] = {}
            self.__index_cache[dbase][collection] = {}
            self.__index_cache[dbase][collection][index] = expire

        elif collection not in self.__index_cache[dbase]:
            self.__index_cache[dbase][collection] = {}
            self.__index_cache[dbase][collection][index] = expire

        else:
            self.__index_cache[dbase][collection][index] = expire

    def _purge_index(self, database_name,
                     collection_name=None, index_name=None):
        """Purge an index from the index cache.

        If `index_name` is None purge an entire collection.

        If `collection_name` is None purge an entire database.
        """
        if not database_name in self.__index_cache:
            return

        if collection_name is None:
            del self.__index_cache[database_name]
            return

        if not collection_name in self.__index_cache[database_name]:
            return

        if index_name is None:
            del self.__index_cache[database_name][collection_name]
            return

        if index_name in self.__index_cache[database_name][collection_name]:
            del self.__index_cache[database_name][collection_name][index_name]

    def _cache_credentials(self, source, credentials, connect=True):
        """Add credentials to the database authentication cache
        for automatic login when a socket is created. If `connect` is True,
        verify the credentials on the server first.

        Raises OperationFailure if other credentials are already stored for
        this source.
        """
        if source in self.__auth_credentials:
            # Nothing to do if we already have these credentials.
            if credentials == self.__auth_credentials[source]:
                return
            raise OperationFailure('Another user is already authenticated '
                                   'to this database. You must logout first.')

        if connect:
            # Try to authenticate even during failover.
            member = select_member(
                self.__rs_state.members, ReadPreference.PRIMARY_PREFERRED)

            if not member:
                raise AutoReconnect(
                    "No replica set members available for authentication")

            sock_info = self.__socket(member)
            try:
                # Since __check_auth was called in __socket
                # there is no need to call it here.
                auth.authenticate(credentials, sock_info, self.__simple_command)
                sock_info.authset.add(credentials)
            finally:
                member.pool.maybe_return_socket(sock_info)

        self.__auth_credentials[source] = credentials

    def _purge_credentials(self, source):
        """Purge credentials from the database authentication cache.
        """
        if source in self.__auth_credentials:
            del self.__auth_credentials[source]

    def __check_auth(self, sock_info):
        """Authenticate using cached database credentials.
        """
        if self.__auth_credentials or sock_info.authset:
            cached = set(self.__auth_credentials.itervalues())

            authset = sock_info.authset.copy()

            # Logout any credentials that no longer exist in the cache.
            for credentials in authset - cached:
                self.__simple_command(sock_info, credentials[0], {'logout': 1})
                sock_info.authset.discard(credentials)

            for credentials in cached - authset:
                auth.authenticate(credentials,
                                  sock_info, self.__simple_command)
                sock_info.authset.add(credentials)

    @property
    def seeds(self):
        """The seed list used to connect to this replica set.

        A sequence of (host, port) pairs.
        """
        return self.__seeds

    @property
    def hosts(self):
        """All active and passive (priority 0) replica set
        members known to this client. This does not include
        hidden or slaveDelay members, or arbiters.

        A sequence of (host, port) pairs.
        """
        return self.__rs_state.hosts

    @property
    def primary(self):
        """The (host, port) of the current primary of the replica set.

        Returns None if there is no primary.
        """
        return self.__rs_state.writer

    @property
    def secondaries(self):
        """The secondary members known to this client.

        A sequence of (host, port) pairs.
        """
        return self.__rs_state.secondaries

    @property
    def arbiters(self):
        """The arbiters known to this client.

        A sequence of (host, port) pairs.
        """
        return self.__rs_state.arbiters

    @property
    def is_mongos(self):
        """If this instance is connected to mongos (always False).

        .. versionadded:: 2.3
        """
        return False

    @property
    def max_pool_size(self):
        """The maximum number of idle connections kept open in each pool for
        future use.

        .. note:: ``max_pool_size`` does not cap the number of concurrent
          connections to a replica set member; there is currently no way to
          limit the number of connections. ``max_pool_size`` only limits the
          number of **idle** connections kept open when they are returned to
          a pool.
        """
        return self.__max_pool_size

    @property
    def use_greenlets(self):
        """Whether calling :meth:`start_request` assigns greenlet-local,
        rather than thread-local, sockets.

        .. versionadded:: 2.4.2
        """
        return self.__use_greenlets

    def get_document_class(self):
        """document_class getter"""
        return self.__document_class

    def set_document_class(self, klass):
        """document_class setter"""
        self.__document_class = klass

    document_class = property(get_document_class, set_document_class,
                              doc="""Default class to use for documents
                              returned from this client.
                              """)

    @property
    def tz_aware(self):
        """Does this client return timezone-aware datetimes?
        """
        return self.__tz_aware

    @property
    def max_bson_size(self):
        """Returns the maximum size BSON object the connected primary
        accepts in bytes. Defaults to 4MB in server < 1.7.4. Returns
        0 if no primary is available.
        """
        rs_state = self.__rs_state
        if rs_state.primary_member:
            return rs_state.primary_member.max_bson_size
        return 0

    @property
    def auto_start_request(self):
        """Is auto_start_request enabled?
        """
        return self.__auto_start_request

    def __simple_command(self, sock_info, dbname, spec):
        """Send a command to the server.
           Returns (response, ping_time in seconds).
        """
        rqst_id, msg, _ = message.query(0, dbname + '.$cmd', 0, -1, spec)
        start = time.time()
        try:
            sock_info.sock.sendall(msg)
            response = self.__recv_msg(1, rqst_id, sock_info)
        except:
            sock_info.close()
            raise

        end = time.time()
        response = helpers._unpack_response(response)['data'][0]
        msg = "command %r failed: %%s" % spec
        helpers._check_command_response(response, None, msg)
        return response, end - start

    def __is_master(self, host):
        """Directly call ismaster.
           Returns (response, connection_pool, ping_time in seconds).
        """
        connection_pool = self.pool_class(
            host,
            self.__max_pool_size,
            self.__net_timeout,
            self.__conn_timeout,
            self.__use_ssl,
            thread_support_module=self.__thread_support_module,
            ssl_keyfile=self.__ssl_keyfile,
            ssl_certfile=self.__ssl_certfile,
            ssl_cert_reqs=self.__ssl_cert_reqs,
            ssl_ca_certs=self.__ssl_ca_certs)

        if self.in_request():
            connection_pool.start_request()

        sock_info = connection_pool.get_socket()
        try:
            response, ping_time = self.__simple_command(
                sock_info, 'admin', {'ismaster': 1}
            )

            connection_pool.maybe_return_socket(sock_info)
            return response, connection_pool, ping_time
        except (ConnectionFailure, socket.error):
            connection_pool.discard_socket(sock_info)
            raise

    def __schedule_refresh(self, sync=False):
        """Awake the monitor to update our view of the replica set's state.

        If `sync` is True, block until the refresh completes.

        If multiple application threads call __schedule_refresh while refresh
        is in progress, the work of refreshing the state is only performed
        once.
        """
        self.__monitor.schedule_refresh()
        if sync:
            self.__monitor.wait_for_refresh(timeout_seconds=5)

    def refresh(self, force=False):
        """Iterate through the existing host list, or possibly the
        seed list, to update the list of hosts and arbiters in this
        replica set.
        """
        # Only one thread / greenlet calls refresh() at a time: the one
        # running __init__() or the monitor. We won't modify the state, only
        # replace it at the end.
        rs_state = self.__rs_state
        errors = []
        if rs_state.hosts:
            # Try first those hosts we think are up, then the down ones.
            nodes = sorted(
                rs_state.hosts, key=lambda host: rs_state.get(host).up)
        else:
            nodes = self.__seeds

        hosts = set()

        # This will become the new RSState.
        members = {}
        arbiters = set()
        writer = None

        # Look for first member from which we can get a list of all members.
        for node in nodes:
            member, sock_info = rs_state.get(node), None
            try:
                try:
                    if member:
                        sock_info = self.__socket(member, force=force)
                        response, ping_time = self.__simple_command(
                            sock_info, 'admin', {'ismaster': 1})
                        new_member = member.clone_with(response, ping_time)
                    else:
                        response, pool, ping_time = self.__is_master(node)
                        new_member = Member(
                            node, pool, response, MovingAverage([ping_time]), True)

                    # Check that this host is part of the given replica set.
                    set_name = response.get('setName')
                    # The 'setName' field isn't returned by mongod before 1.6.2
                    # so we can't assume that if it's missing this host isn't in
                    # the specified set.
                    if set_name and set_name != self.__name:
                        host, port = node
                        raise ConfigurationError("%s:%d is not a member of "
                                                 "replica set %s"
                                                 % (host, port, self.__name))
                    if "arbiters" in response:
                        arbiters = set([
                            _partition_node(h) for h in response["arbiters"]])
                    if "hosts" in response:
                        hosts.update([_partition_node(h)
                                      for h in response["hosts"]])
                    if "passives" in response:
                        hosts.update([_partition_node(h)
                                      for h in response["passives"]])

                    # Start off the new 'members' dict with this member
                    # but don't add seed list members.
                    if node in hosts:
                        members[node] = new_member
                        if response['ismaster']:
                            writer = node

                except (ConnectionFailure, socket.error), why:
                    if member:
                        member.pool.discard_socket(sock_info)
                        sock_info = None
                    errors.append("%s:%d: %s" % (node[0], node[1], str(why)))
            finally:
                if member and sock_info:
                    member.pool.maybe_return_socket(sock_info)
            if hosts:
                break
        else:
            if errors:
                raise AutoReconnect(', '.join(errors))
            raise ConfigurationError('No suitable hosts found')

        # Ensure we have a pool for each member, and find the primary.
        for host in hosts:
            if host in members:
                # This member was the first we connected to, in the loop above.
                continue

            member, sock_info = rs_state.get(host), None
            try:
                try:
                    if member:
                        sock_info = self.__socket(member)
                        res, ping_time = self.__simple_command(
                            sock_info, 'admin', {'ismaster': 1})
                        new_member = member.clone_with(res, ping_time)
                    else:
                        res, connection_pool, ping_time = self.__is_master(host)
                        new_member = Member(
                            host, connection_pool, res, MovingAverage([ping_time]),
                            True)

                    members[host] = new_member

                except (ConnectionFailure, socket.error):
                    if member:
                        member.pool.discard_socket(sock_info)
                    continue
            finally:
                if member and sock_info:
                    member.pool.maybe_return_socket(sock_info)

            if res['ismaster']:
                writer = host

        if writer == rs_state.writer:
            threadlocal = self.__rs_state.threadlocal
        else:
            # We unpin threads from members if the primary has changed, since
            # no monotonic consistency can be promised now anyway.
            threadlocal = self.__thread_support_module.local()

        # Replace old state with new.
        self.__rs_state = RSState(threadlocal, members, arbiters, writer)

    def __find_primary(self):
        """Returns a connection to the primary of this replica set,
        if one exists, or raises AutoReconnect.
        """
        primary = self.__rs_state.primary_member
        if primary:
            return primary

        # We had a failover.
        self.__schedule_refresh(sync=True)

        # Try again. This time copy the RSState reference so we're guaranteed
        # primary_member and error_message are from the same state.
        rs_state = self.__rs_state
        if rs_state.primary_member:
            return rs_state.primary_member

        # Couldn't find the primary.
        raise AutoReconnect(rs_state.error_message)

    def __socket(self, member, force=False):
        """Get a SocketInfo from the pool.
        """
        if self.auto_start_request and not self.in_request():
            self.start_request()

        sock_info = member.pool.get_socket(force=force)

        try:
            self.__check_auth(sock_info)
        except OperationFailure:
            member.pool.maybe_return_socket(sock_info)
            raise
        return sock_info

    def disconnect(self):
        """Disconnect from the replica set primary, unpin all members, and
        refresh our view of the replica set.
        """
        rs_state = self.__rs_state
        if rs_state.primary_member:
            rs_state.primary_member.pool.reset()

        threadlocal = self.__thread_support_module.local()
        self.__rs_state = rs_state.clone_without_writer(threadlocal)
        self.__schedule_refresh()

    def close(self):
        """Close this client instance.

        This method first terminates the replica set monitor, then disconnects
        from all members of the replica set.

        .. warning:: This method stops the replica set monitor task. The
           replica set monitor is required to properly handle replica set
           configuration changes, including a failure of the primary.
           Once :meth:`~close` is called this client instance must not be reused.

        .. versionchanged:: 2.2.1
           The :meth:`close` method now terminates the replica set monitor.
        """
        if self.__monitor:
            self.__monitor.shutdown()
            # Use a reasonable timeout.
            self.__monitor.join(1.0)
            self.__monitor = None

        self.__rs_state = RSState(self.__thread_support_module.local())

    def alive(self):
        """Return ``False`` if there has been an error communicating with the
        primary, else ``True``.

        This method attempts to check the status of the primary with minimal
        I/O. The current thread / greenlet retrieves a socket (its request
        socket if it's in a request, or a random idle socket if it's not in a
        request) from the primary's connection pool and checks whether calling
        select_ on it raises an error. If there are currently no idle sockets,
        or if there is no known primary, :meth:`alive` will attempt to actually
        find and connect to the primary.

        A more certain way to determine primary availability is to ping it::

            client.admin.command('ping')

        .. _select: http://docs.python.org/2/library/select.html#select.select
        """
        # In the common case, a socket is available and was used recently, so
        # calling select() on it is a reasonable attempt to see if the OS has
        # reported an error. Note this can be wasteful: __socket implicitly
        # calls select() if the socket hasn't been checked in the last second,
        # or it may create a new socket, in which case calling select() is
        # redundant.
        member, sock_info = None, None
        try:
            try:
                member = self.__find_primary()
                sock_info = self.__socket(member)
                return not pool._closed(sock_info.sock)
            except (socket.error, ConnectionFailure):
                return False
        finally:
            if sock_info is not None:
                member.pool.maybe_return_socket(sock_info)

    def __check_response_to_last_error(self, response):
        """Check a response to a lastError message for errors.

        `response` is a byte string representing a response to the message.
        If it represents an error response we raise OperationFailure.

        Return the response as a document.
        """
        response = helpers._unpack_response(response)

        assert response["number_returned"] == 1
        error = response["data"][0]

        helpers._check_command_response(error, self.disconnect)

        error_msg = error.get("err", "")
        if error_msg is None:
            return error
        if error_msg.startswith("not master"):
            self.disconnect()
            raise AutoReconnect(error_msg)

        if "code" in error:
            if error["code"] in (11000, 11001, 12582):
                raise DuplicateKeyError(error["err"], error["code"])
            else:
                raise OperationFailure(error["err"], error["code"])
        else:
            raise OperationFailure(error["err"])

    def __recv_data(self, length, sock_info):
        """Lowest level receive operation.

        Takes length to receive and repeatedly calls recv until able to
        return a buffer of that length, raising ConnectionFailure on error.
        """
        message = EMPTY
        while length:
            chunk = sock_info.sock.recv(length)
            if chunk == EMPTY:
                raise ConnectionFailure("connection closed")
            length -= len(chunk)
            message += chunk
        return message

    def __recv_msg(self, operation, request_id, sock):
        """Receive a message in response to `request_id` on `sock`.

        Returns the response data with the header removed.
        """
        header = self.__recv_data(16, sock)
        length = struct.unpack("<i", header[:4])[0]
        resp_id = struct.unpack("<i", header[8:12])[0]
        assert resp_id == request_id, "ids don't match %r %r" % (resp_id,
                                                                 request_id)
        assert operation == struct.unpack("<i", header[12:])[0]

        return self.__recv_data(length - 16, sock)

    def __check_bson_size(self, msg, max_size):
        """Make sure the message doesn't include BSON documents larger
        than the connected server will accept.

        :Parameters:
          - `msg`: message to check
        """
        if len(msg) == 3:
            request_id, data, max_doc_size = msg
            if max_doc_size > max_size:
                raise InvalidDocument("BSON document too large (%d bytes)"
                                      " - the connected server supports"
                                      " BSON document sizes up to %d"
                                      " bytes." %
                                      (max_doc_size, max_size))
            return (request_id, data)
        # get_more and kill_cursors messages
        # don't include BSON documents.
        return msg

    def _send_message(self, msg,
                      with_last_error=False, _connection_to_use=None):
        """Say something to Mongo.

        Raises ConnectionFailure if the message cannot be sent. Raises
        OperationFailure if `with_last_error` is ``True`` and the
        response to the getLastError call returns an error. Return the
        response from lastError, or ``None`` if `with_last_error` is
        ``False``.

        :Parameters:
          - `msg`: message to send
          - `with_last_error`: check getLastError status after sending the
            message
        """
        # This may be the first time we're connecting to the set.
        if self.__monitor and not self.__monitor.started:
            self.__monitor.start()

        if _connection_to_use in (None, -1):
            member = self.__find_primary()
        else:
            member = self.__rs_state.get(_connection_to_use)

        sock_info = None
        try:
            try:
                sock_info = self.__socket(member)
                rqst_id, data = self.__check_bson_size(msg, member.max_bson_size)
                sock_info.sock.sendall(data)
                # Safe mode. We pack the message together with a lastError
                # message and send both. We then get the response (to the
                # lastError) and raise OperationFailure if it is an error
                # response.
                rv = None
                if with_last_error:
                    response = self.__recv_msg(1, rqst_id, sock_info)
                    rv = self.__check_response_to_last_error(response)
                return rv
            except OperationFailure:
                raise
            except(ConnectionFailure, socket.error), why:
                member.pool.discard_socket(sock_info)
                if _connection_to_use in (None, -1):
                    self.disconnect()
                raise AutoReconnect(str(why))
            except:
                sock_info.close()
                raise
        finally:
            if sock_info is not None:
                member.pool.maybe_return_socket(sock_info)

    def __send_and_receive(self, member, msg, **kwargs):
        """Send a message on the given socket and return the response data.

        Can raise socket.error.
        """
        sock_info = None
        try:
            try:
                sock_info = self.__socket(member)

                if "network_timeout" in kwargs:
                    sock_info.sock.settimeout(kwargs['network_timeout'])

                rqst_id, data = self.__check_bson_size(msg, member.max_bson_size)
                sock_info.sock.sendall(data)
                response = self.__recv_msg(1, rqst_id, sock_info)

                if "network_timeout" in kwargs:
                    sock_info.sock.settimeout(self.__net_timeout)

                return response
            except:
                member.pool.discard_socket(sock_info)
                raise
        finally:
            if sock_info is not None:
                member.pool.maybe_return_socket(sock_info)

    def __try_read(self, member, msg, **kwargs):
        """Attempt a read from a member; on failure mark the member "down" and
           wake up the monitor thread to refresh as soon as possible.
        """
        try:
            return self.__send_and_receive(member, msg, **kwargs)
        except socket.timeout, e:
            # Could be one slow query, don't refresh.
            host, port = member.host
            raise AutoReconnect("%s:%d: %s" % (host, port, e))
        except (socket.error, ConnectionFailure), why:
            # Try to replace our RSState with a clone where this member is
            # marked "down", to reduce exceptions on other threads, or repeated
            # exceptions on this thread. We accept that there's a race
            # condition (another thread could be replacing our state with a
            # different version concurrently) but this approach is simple and
            # lock-free.
            self.__rs_state = self.__rs_state.clone_with_host_down(
                member.host, str(why))

            self.__schedule_refresh()
            host, port = member.host
            raise AutoReconnect("%s:%d: %s" % (host, port, why))

    def _send_message_with_response(self, msg, _connection_to_use=None,
                                    _must_use_master=False, **kwargs):
        """Send a message to Mongo and return the response.

        Sends the given message and returns (host used, response).

        :Parameters:
          - `msg`: (request_id, data) pair making up the message to send
          - `_connection_to_use`: Optional (host, port) of member for message,
            used by Cursor for getMore and killCursors messages.
          - `_must_use_master`: If True, send to primary.
        """
        # This may be the first time we're connecting to the set.
        if self.__monitor and not self.__monitor.started:
            self.__monitor.start()

        rs_state = self.__rs_state
        tag_sets = kwargs.get('tag_sets', [{}])
        mode = kwargs.get('read_preference', ReadPreference.PRIMARY)
        if _must_use_master:
            mode = ReadPreference.PRIMARY
            tag_sets = [{}]

        if not rs_state.primary_member:
            # Primary was down last we checked. Start a refresh if one is not
            # already in progress. If caller requested the primary, wait to
            # see if it's up, otherwise continue with known-good members.
            sync = (mode == ReadPreference.PRIMARY)
            self.__schedule_refresh(sync=sync)
            rs_state = self.__rs_state

        latency = kwargs.get(
            'secondary_acceptable_latency_ms',
            self.secondary_acceptable_latency_ms)

        try:
            if _connection_to_use is not None:
                if _connection_to_use == -1:
                    member = rs_state.primary_member
                    error_message = rs_state.error_message
                else:
                    member = rs_state.get(_connection_to_use)
                    error_message = '%s:%s not available' % _connection_to_use

                if not member:
                    raise AutoReconnect(error_message)

                return member.pool.pair, self.__try_read(
                    member, msg, **kwargs)
        except AutoReconnect:
            if _connection_to_use in (-1, rs_state.writer):
                # Primary's down. Refresh.
                self.disconnect()
            raise

        # To provide some monotonic consistency, we use the same member as
        # long as this thread is in a request and all reads use the same
        # mode, tags, and latency. The member gets unpinned if pref changes,
        # if member changes state, if we detect a failover, or if this thread
        # calls end_request().
        errors = []

        pinned_host = rs_state.pinned_host
        pinned_member = rs_state.get(pinned_host)
        if (pinned_member
                and pinned_member.matches_mode(mode)
                and pinned_member.matches_tag_sets(tag_sets)  # TODO: REMOVE?
                and rs_state.keep_pinned_host(mode, tag_sets, latency)):
            try:
                return (
                    pinned_member.host,
                    self.__try_read(pinned_member, msg, **kwargs))
            except AutoReconnect, why:
                if _must_use_master or mode == ReadPreference.PRIMARY:
                    self.disconnect()
                    raise
                else:
                    errors.append(str(why))

        # No pinned member, or pinned member down or doesn't match read pref
        rs_state.unpin_host()

        members = list(rs_state.members)
        while len(errors) < MAX_RETRY:
            member = select_member(
                members=members,
                mode=mode,
                tag_sets=tag_sets,
                latency=latency)

            if not member:
                # Ran out of members to try
                break

            try:
                # Sets member.up False on failure, so select_member won't try
                # it again.
                response = self.__try_read(member, msg, **kwargs)

                # Success
                if self.in_request():
                    # Keep reading from this member in this thread / greenlet
                    # unless read preference changes
                    rs_state.pin_host(member.host, mode, tag_sets, latency)
                return member.host, response
            except AutoReconnect, why:
                errors.append(str(why))
                members.remove(member)

        # Ran out of tries
        if mode == ReadPreference.PRIMARY:
            msg = "No replica set primary available for query"
        elif mode == ReadPreference.SECONDARY:
            msg = "No replica set secondary available for query"
        else:
            msg = "No replica set members available for query"

        msg += " with ReadPreference %s" % modes[mode]

        if tag_sets != [{}]:
            msg += " and tags " + repr(tag_sets)

        raise AutoReconnect(msg, errors)

    def start_request(self):
        """Ensure the current thread or greenlet always uses the same socket
        until it calls :meth:`end_request`. For
        :class:`~pymongo.read_preferences.ReadPreference` PRIMARY,
        auto_start_request=True ensures consistent reads, even if you read
        after an unacknowledged write. For read preferences other than PRIMARY,
        there are no consistency guarantees.

        In Python 2.6 and above, or in Python 2.5 with
        "from __future__ import with_statement", :meth:`start_request` can be
        used as a context manager:

        >>> client = pymongo.MongoReplicaSetClient()
        >>> db = client.test
        >>> _id = db.test_collection.insert({})
        >>> with client.start_request():
        ...     for i in range(100):
        ...         db.test_collection.update({'_id': _id}, {'$set': {'i':i}})
        ...
        ...     # Definitely read the document after the final update completes
        ...     print db.test_collection.find({'_id': _id})

        .. versionadded:: 2.2
           The :class:`~pymongo.pool.Request` return value.
           :meth:`start_request` previously returned None
        """
        # We increment our request counter's thread- or greenlet-local value
        # for every call to start_request; however, we only call each pool's
        # start_request once to start a request, and call each pool's
        # end_request once to end it. We don't let pools' request counters
        # exceed 1. This keeps things sane when we create and delete pools
        # within a request.
        if 1 == self.__request_counter.inc():
            for member in self.__rs_state.members:
                member.pool.start_request()

        return pool.Request(self)

    def in_request(self):
        """True if :meth:`start_request` has been called, but not
        :meth:`end_request`, or if `auto_start_request` is True and
        :meth:`end_request` has not been called in this thread or greenlet.
        """
        return bool(self.__request_counter.get())

    def end_request(self):
        """Undo :meth:`start_request` and allow this thread's connections to
        replica set members to return to the pool.

        Calling :meth:`end_request` allows the :class:`~socket.socket` that has
        been reserved for this thread by :meth:`start_request` to be returned
        to the pool. Other threads will then be able to re-use that
        :class:`~socket.socket`. If your application uses many threads, or has
        long-running threads that infrequently perform MongoDB operations, then
        judicious use of this method can lead to performance gains. Care should
        be taken, however, to make sure that :meth:`end_request` is not called
        in the middle of a sequence of operations in which ordering is
        important. This could lead to unexpected results.
        """
        rs_state = self.__rs_state
        if 0 == self.__request_counter.dec():
            for member in rs_state.members:
                # No effect if not in a request
                member.pool.end_request()

            rs_state.unpin_host()

    def __eq__(self, other):
        # XXX: Implement this?
        return NotImplemented

    def __ne__(self, other):
        return NotImplemented

    def __repr__(self):
        return "MongoReplicaSetClient(%r)" % (["%s:%d" % n
                                               for n in self.hosts],)

    def __getattr__(self, name):
        """Get a database by name.

        Raises :class:`~pymongo.errors.InvalidName` if an invalid
        database name is used.

        :Parameters:
          - `name`: the name of the database to get
        """
        return database.Database(self, name)

    def __getitem__(self, name):
        """Get a database by name.

        Raises :class:`~pymongo.errors.InvalidName` if an invalid
        database name is used.

        :Parameters:
          - `name`: the name of the database to get
        """
        return self.__getattr__(name)

    def close_cursor(self, cursor_id, _conn_id):
        """Close a single database cursor.

        Raises :class:`TypeError` if `cursor_id` is not an instance of
        ``(int, long)``. What closing the cursor actually means
        depends on this client's cursor manager.

        :Parameters:
          - `cursor_id`: id of cursor to close
        """
        if not isinstance(cursor_id, (int, long)):
            raise TypeError("cursor_id must be an instance of (int, long)")

        self._send_message(message.kill_cursors([cursor_id]),
                           _connection_to_use=_conn_id)

    def server_info(self):
        """Get information about the MongoDB primary we're connected to.
        """
        return self.admin.command("buildinfo")

    def database_names(self):
        """Get a list of the names of all databases on the connected server.
        """
        return [db["name"] for db in
                self.admin.command("listDatabases")["databases"]]

    def drop_database(self, name_or_database):
        """Drop a database.

        Raises :class:`TypeError` if `name_or_database` is not an instance of
        :class:`basestring` (:class:`str` in python 3) or Database

        :Parameters:
          - `name_or_database`: the name of a database to drop, or a
            :class:`~pymongo.database.Database` instance representing the
            database to drop
        """
        name = name_or_database
        if isinstance(name, database.Database):
            name = name.name

        if not isinstance(name, basestring):
            raise TypeError("name_or_database must be an instance of "
                            "%s or Database" % (basestring.__name__,))

        self._purge_index(name)
        self[name].command("dropDatabase")

    def copy_database(self, from_name, to_name,
                      from_host=None, username=None, password=None):
        """Copy a database, potentially from another host.

        Raises :class:`TypeError` if `from_name` or `to_name` is not
        an instance of :class:`basestring` (:class:`str` in python 3).
        Raises :class:`~pymongo.errors.InvalidName` if `to_name` is
        not a valid database name.

        If `from_host` is ``None`` the current host is used as the
        source. Otherwise the database is copied from `from_host`.

        If the source database requires authentication, `username` and
        `password` must be specified.

        :Parameters:
          - `from_name`: the name of the source database
          - `to_name`: the name of the target database
          - `from_host` (optional): host name to copy from
          - `username` (optional): username for source database
          - `password` (optional): password for source database

        .. note:: Specifying `username` and `password` requires server
           version **>= 1.3.3+**.
        """
        if not isinstance(from_name, basestring):
            raise TypeError("from_name must be an instance "
                            "of %s" % (basestring.__name__,))
        if not isinstance(to_name, basestring):
            raise TypeError("to_name must be an instance "
                            "of %s" % (basestring.__name__,))

        database._check_name(to_name)

        command = {"fromdb": from_name, "todb": to_name}

        if from_host is not None:
            command["fromhost"] = from_host

        try:
            self.start_request()

            if username is not None:
                nonce = self.admin.command("copydbgetnonce",
                                           fromhost=from_host)["nonce"]
                command["username"] = username
                command["nonce"] = nonce
                command["key"] = auth._auth_key(nonce, username, password)

            return self.admin.command("copydb", **command)
        finally:
            self.end_request()
