"""Authenticated, bounded loopback identity/cancel channel for an owned worker.

The caller proves the expected UUID/PID/creation identity before a cancellation
is acted on. A persisted PID alone grants no process control. Secrets stay in
private scheduler metadata and are never returned as public status/evidence.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import uuid

from .analysis_protocol import canonical_record, parse_record

_REQUEST = 'AnalysisWorkerControlRequest/v1'
_RESPONSE = 'AnalysisWorkerControlResponse/v1'
_HEX = re.compile('[0-9a-f]{64}\\Z')
_UUID = re.compile('[0-9a-f]{32}\\Z')
_LIMIT = 4096


def _signature(version, body, secret):
    data = json.dumps({'schema_version': version, 'body': body}, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hmac.new(bytes.fromhex(secret), data, hashlib.sha256).hexdigest()


def _packet(version, body, secret, schema):
    envelope = {'schema_version': version, 'body': body, 'mac': _signature(version, body, secret)}
    return canonical_record(envelope, schema_name=schema) + b'\n'


def _receive(connection):
    data = bytearray()
    while b'\n' not in data:
        piece = connection.recv(min(1024, _LIMIT + 1 - len(data)))
        if not piece: raise ValueError('incomplete control message')
        data.extend(piece)
        if len(data) > _LIMIT: raise ValueError('control message exceeds limit')
    message, _, trailing = bytes(data).partition(b'\n')
    if trailing: raise ValueError('one control message per connection is required')
    return message.decode('utf-8')


def _verified(text, secret, schema):
    envelope = parse_record(text, schema_name=schema)
    expected = _signature(envelope['schema_version'], envelope['body'], secret)
    if not hmac.compare_digest(envelope['mac'], expected):
        raise ValueError('unverified control message')
    return envelope['body']


class WorkerControlServer:
    def __init__(self, *, snapshot, cancel, worker_uuid=None, secret=None):
        self.worker_uuid = worker_uuid or uuid.uuid4().hex
        self._secret = secret or secrets.token_hex(32)
        if not _UUID.fullmatch(self.worker_uuid) or not _HEX.fullmatch(self._secret):
            raise ValueError('fixed-size private worker identity required')
        self.pid = os.getpid()
        self.created_ns = time.time_ns()
        self._snapshot, self._cancel = snapshot, cancel
        self._stop = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self._socket.bind(('127.0.0.1', 0))
        self._socket.listen(8)
        self._socket.settimeout(.2)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name='fea-worker-control', daemon=True)

    def private_claim(self):
        """Internal persistence input; never include this object in status."""
        return {'worker_uuid': self.worker_uuid, 'secret': self._secret, 'pid': self.pid, 'created_ns': self.created_ns, 'port': self.port}

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError('worker metadata control thread did not close')

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set(): break
                continue
            with connection:
                connection.settimeout(.5)
                try:
                    body = _verified(_receive(connection), self._secret, 'worker-control-request')
                    identity = {'worker_uuid': self.worker_uuid, 'pid': self.pid, 'created_ns': self.created_ns}
                    if any(body[key] != value for key, value in identity.items()):
                        continue
                    if body['action'] == 'cancel' and body['run_id'] is None:
                        continue
                    snapshot = self._snapshot()
                    status = 'identity_verified'
                    if body['action'] == 'cancel':
                        # Only a small in-memory event/metadata callback belongs
                        # on this thread; no FreeCAD document operations.
                        status = 'cancel_requested' if self._cancel(body['run_id']) is True else 'cancel_ignored'
                    response = {**body, 'status': status, 'stage': snapshot['stage'], 'active_run_id': snapshot['active_run_id'], 'heartbeat_ns': time.time_ns()}
                    connection.sendall(_packet(_RESPONSE, response, self._secret, 'worker-control-response'))
                except Exception:
                    # Unauthenticated/malformed packets cannot enter logs,
                    # expose claim material, or interrupt the solver owner.
                    continue


def contact_worker(claim, *, action='identity', run_id=None, timeout=.5):
    """Return authenticated public identity or None; connect only to loopback."""
    try:
        if type(claim) is not dict or claim.get('host', '127.0.0.1') != '127.0.0.1' or action not in ('identity', 'cancel') or type(timeout) not in (int, float) or not 0 < timeout <= 2:
            return None
        secret = claim['secret']
        if type(secret) is not str or not _HEX.fullmatch(secret) or type(claim['worker_uuid']) is not str or not _UUID.fullmatch(claim['worker_uuid']) or type(claim['port']) is not int or not 1 <= claim['port'] <= 65535:
            return None
        if action == 'cancel' and run_id is None:
            return None
        body = {'worker_uuid': claim['worker_uuid'], 'pid': claim['pid'], 'created_ns': claim['created_ns'], 'challenge': secrets.token_hex(32), 'action': action, 'run_id': run_id}
        packet = _packet(_REQUEST, body, secret, 'worker-control-request')
        with socket.create_connection(('127.0.0.1', claim['port']), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(packet)
            response = _verified(_receive(connection), secret, 'worker-control-response')
        if any(response[key] != value for key, value in body.items()):
            return None
        return response
    except (OSError, ValueError, KeyError, TypeError):
        return None
