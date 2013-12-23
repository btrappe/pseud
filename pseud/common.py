import __builtin__
import functools
import logging
import operator
import sys
import textwrap
import traceback
import uuid

import msgpack
import zmq
import zope.component
import zope.interface

from .interfaces import (AUTHENTICATED,
                         ERROR,
                         HEARTBEAT,
                         HELLO,
                         IAuthenticationBackend,
                         IHeartbeatBackend,
                         OK,
                         ServiceNotFoundError,
                         UNAUTHORIZED,
                         VERSION,
                         WORK,
                         )
from .utils import (get_rpc_callable,
                    register_rpc,
                    create_local_registry,
                    )


logger = logging.getLogger(__name__)

_marker = object()


def format_remote_traceback(traceback):
    pivot = '\n{}'.format(3 * 4 * ' ')  # like three tabs
    return textwrap.dedent("""
        -- Beginning of remote traceback --
            {}
        -- End of remote traceback --
        """.format(pivot.join(traceback.splitlines())))


class AttributeWrapper(object):
    def __init__(self, rpc, name=None, peer_id=None):
        self.rpc = rpc
        self._part_names = name.split('.') if name is not None else []
        self.peer_id = peer_id

    def __getattr__(self, name, default=_marker):
        try:
            if default is _marker:
                return super(AttributeWrapper, self).__getattr__(name)
            return super(AttributeWrapper, self).__getattr__(name,
                                                             default=default)
        except AttributeError:
            self.name = name
            return self

    def name_getter(self):
        return '.'.join(self._part_names)

    def name_setter(self, value):
        self._part_names.append(value)

    name = property(name_getter, name_setter)

    def __call__(self, *args, **kw):
        destination = self.peer_id or self.rpc.peer_identity
        return self.rpc.send_work(destination, self.name, *args, **kw)



class BaseRPC(object):
    def __init__(self, identity=None, peer_identity=None,
                 context=None, io_loop=None,
                 security_plugin='noop_auth_backend',
                 public_key=None, secret_key=None,
                 peer_public_key=None, timeout=5,
                 password=None,
                 heartbeat_plugin='noop_heartbeat_backend',
                 proxy_to=None,
                 registry=None):
        self.identity = identity
        self.context = context or self._make_context()
        self.peer_identity = peer_identity
        self.security_plugin = security_plugin
        self.future_pool = {}
        self.initialized = False
        self.auth_backend = zope.component.getAdapter(self,
                                                      IAuthenticationBackend,
                                                      name=self.security_plugin
                                                      )
        self.public_key = public_key
        self.secret_key = secret_key
        self.peer_public_key = peer_public_key
        self.password = password
        self.timeout = timeout
        self.heartbeat_backend = zope.component.getAdapter(
            self,
            IHeartbeatBackend,
            name=heartbeat_plugin)
        self.proxy_to = proxy_to
        self._backend_init(io_loop=io_loop)
        self.reader = None
        self.registry = (registry if registry is not None
                         else create_local_registry(identity or ''))

    def __getattr__(self, name, default=_marker):
        if name in ('connect', 'bind'):
            return functools.partial(self.connect_or_bind, name)
        try:
            if default is _marker:
                return super(BaseRPC, self).__getattr__(name)
            return super(BaseRPC, self).__getattr__(name, default=default)
        except AttributeError:
            if not self.initialized:
                raise RuntimeError('You must connect or bind first'
                                   ' in order to call {!r}'.format(name))
            return AttributeWrapper(self, name=name)

    def send_to(self, peer_id):
        return AttributeWrapper(self, peer_id=peer_id)

    def connect_or_bind(self, name, endpoint):
        socket = self.context.socket(self.socket_type)
        self.socket = socket
        if self.identity:
            socket.identity = self.identity
        if self.socket_type == zmq.ROUTER:
            socket.ROUTER_MANDATORY = True
        if self.socket_type == zmq.REQ:
            socket.RCVTIMEO = int(self.timeout * 1000)
        socket.SNDTIMEO = int(self.timeout * 1000)
        self.auth_backend.configure()
        self.heartbeat_backend.configure()
        caller = operator.methodcaller(name, endpoint)
        caller(socket)
        self.initialized = True

    def _prepare_work(self, peer_identity, name, *args, **kw):
        work = msgpack.packb((name, args, kw))
        uid = uuid.uuid4().bytes
        message = [peer_identity, '', VERSION, uid, WORK, work]
        return message, uid

    def create_timeout_detector(self, uuid):
        self.create_later_callback(functools.partial(self.timeout_task, uuid),
                                   self.timeout)

    def cleanup_future(self, uuid, future):
        try:
            del self.future_pool[uuid]
        except KeyError:
            pass

    def on_socket_ready(self, response):
        logger.debug('Message received for {!r}: {!r}'.format(self, response))
        if len(response) == 4:
            # From REQ socket
            version, message_uuid, message_type, message = response
            peer_id = None
        else:
            # from ROUTER socket
            peer_id, delimiter, version, message_uuid, message_type, message =\
                response
        assert version == VERSION
        if not self.auth_backend.is_authenticated(peer_id):
            if message_type != HELLO:
                self.auth_backend.handle_authentication(peer_id, message_uuid)
            else:
                self.auth_backend.handle_hello(peer_id, message_uuid,
                                               message)
        else:
            self.heartbeat_backend.handle_heartbeat(peer_id)
            if message_type == WORK:
                self._handle_work(message, peer_id, message_uuid)
            elif message_type == OK:
                return self._handle_ok(message, message_uuid)
            elif message_type == ERROR:
                self._handle_error(message, message_uuid)
            elif message_type == AUTHENTICATED:
                self.auth_backend.handle_authenticated(message)
            elif message_type == UNAUTHORIZED:
                self.auth_backend.handle_authentication(peer_id, message_uuid)
            elif message_type == HELLO:
                self.auth_backend.handle_hello(peer_id, message_uuid, message)
            elif message_type == HEARTBEAT:
                # Can ignore, because every message is an heartbeat
                pass
            else:
                logger.error('Unknown message_type'
                             ' received {!r}'.format(message_type))
                raise NotImplementedError

    def _handle_work_proxy(self, locator, args, kw, peer_id, message_uuid):
        worker_callable = get_rpc_callable(locator,
                                           registry=self.registry)
        return worker_callable(*args, **kw)

    def _handle_work(self, message, peer_id, message_uuid):
        locator, args, kw = msgpack.unpackb(message)
        try:
            try:
                result = self._handle_work_proxy(locator, args, kw, peer_id,
                                                 message_uuid)
            except ServiceNotFoundError:
                if self.proxy_to is None:
                    raise
                else:
                    result = self.proxy_to._handle_work_proxy(locator, args,
                                                              kw, peer_id,
                                                              message_uuid)

        except Exception:
            exc_type, exc_value = sys.exc_info()[:2]
            traceback_ = traceback.format_exc()
            name = exc_type.__name__
            message = str(exc_value)
            result = (name, message, traceback_)
            status = ERROR
        else:
            status = OK
        response = msgpack.packb(result)
        message = [peer_id, '', VERSION, message_uuid, status, response]
        logger.debug('Worker send reply {!r}'.format(message))
        self.send_message(message)

    def _handle_ok(self, message, message_uuid):
        value = msgpack.unpackb(message)
        logger.debug('Client result {!r} from {!r}'.format(value,
                                                           message_uuid))
        future = self.future_pool.pop(message_uuid)
        self._store_result_in_future(future, value)

    def _handle_error(self, message, message_uuid):
        value = msgpack.unpackb(message)
        future = self.future_pool.pop(message_uuid)
        klass, message, trace_back = value
        full_message = '\n'.join((format_remote_traceback(trace_back),
                                  message))
        try:
            exception = getattr(__builtin__, klass)(full_message)
        except AttributeError:
            if klass == 'ServiceNotFoundError':
                # XXX Unhardcode me
                exception = ServiceNotFoundError(full_message)
                future.set_exception(exception)
            else:
                # Not stdlib Exception
                # fallback on something that expose informations received
                # from remote worker
                future.set_exception(Exception('\n'.join((klass,
                                                          full_message))))
        else:
            future.set_exception(exception)

    @property
    def register_rpc(self):
        return functools.partial(register_rpc, registry=self.registry)
