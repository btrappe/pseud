import __builtin__
import logging
import os
import uuid

import msgpack
import zmq
import zope.interface

from .common import BaseRPC, format_remote_traceback
from .interfaces import (IClient,
                         ServiceNotFoundError,
                         TimeoutError,
                         VERSION,
                         WORK,
                         )
try:
    from ._tornado import Client, Server
    if os.getenv('NO_TORNADO'):
        raise ImportError
except ImportError:
    from ._gevent import Client, Server  # NOQA


logger = logging.getLogger(__name__)


class SyncBaseRPC(BaseRPC):
    """
    Support limited features and run synchronously
    Doesn't require tornado nor gevent
    This is suitable to use in synchronous environment like
    within wsgi process
    """
    def _make_context(self):
        return zmq.Context.instance()

    def _backend_init(self, io_loop=None):
        self.reader = None
        self.internal_loop = False
        self.io_loop = None

    def send_work(self, peer_identity, name, *args, **kw):
        message, uid = self._prepare_work(name, *args, **kw)
        logger.debug('Sending work: {!r}'.format(message))
        self.auth_backend.save_last_work(message)
        response = self.send_message(message)
        return response

    def _prepare_work(self, name, *args, **kw):
        work = msgpack.packb((name, args, kw))
        uid = uuid.uuid4().bytes
        message = [VERSION, uid, WORK, work]
        return message, uid

    def _handle_ok(self, message, message_uuid):
        value = msgpack.unpackb(message)
        logger.debug('SyncClient result {!r} from {!r}'.format(value,
                                                               message_uuid))
        return value

    def _handle_error(self, message, message_uuid):
        value = msgpack.unpackb(message)
        klass, message, trace_back = value
        full_message = '\n'.join((format_remote_traceback(trace_back),
                                  message))
        try:
            exception = getattr(__builtin__, klass)(full_message)
        except AttributeError:
            if klass == 'ServiceNotFoundError':
                # XXX Unhardcode me
                raise ServiceNotFoundError(full_message)
            else:
                # Not stdlib Exception
                # fallback on something that expose informations received
                # from remote worker
                raise Exception('\n'.join((klass, full_message)))
        else:
            raise exception

    def send_message(self, message):
        self.socket.send_multipart(message)
        try:
            response = self.socket.recv_multipart()
        except zmq.Again:
            raise TimeoutError
        return self.on_socket_ready(response)

    def _store_result_in_future(self, future, result):
        raise TypeError('SyncClient can not do that')

    def start(self):
        pass

    def read_forever(self, socket, callback):
        raise TypeError('SyncClient can not do that')

    def create_periodic_callback(self, callback, timer):
        raise TypeError('SyncClient can not do that')

    def create_later_callback(self, callback, timer):
        raise TypeError('SyncClient can not do that')

    def timeout_task(self, uuid):
        raise TypeError('SyncClient can not do that')

    def stop(self):
        if not self.socket.closed:
            self.socket.linger = 0
            self.socket.close()
        self.auth_backend.stop()
        self.heartbeat_backend.stop()


@zope.interface.implementer(IClient)
class SyncClient(SyncBaseRPC):
    socket_type = zmq.REQ

    def __init__(self, identity=None,
                 context=None, io_loop=None,
                 security_plugin='noop_auth_backend', timeout=5,
                 public_key=None, secret_key=None, peer_public_key=None,
                 password=None,
                 heartbeat_plugin='noop_heartbeat_backend',
                 proxy_to=None,
                 registry=None,
                 ):
        super(SyncClient, self).__init__(identity=identity,
                                         context=context, io_loop=io_loop,
                                         security_plugin=security_plugin,
                                         timeout=timeout,
                                         public_key=public_key,
                                         secret_key=secret_key,
                                         peer_public_key=peer_public_key,
                                         password=password,
                                         heartbeat_plugin=heartbeat_plugin,
                                         proxy_to=proxy_to,
                                         registry=registry,
                                         )