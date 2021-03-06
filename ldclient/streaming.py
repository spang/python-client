from __future__ import absolute_import

import json
from threading import Thread

import backoff
import time

from ldclient.interfaces import UpdateProcessor
from ldclient.sse_client import SSEClient
from ldclient.util import _stream_headers, log

# allows for up to 5 minutes to elapse without any data sent across the stream. The heartbeats sent as comments on the
# stream will keep this from triggering
stream_read_timeout = 5 * 60


class StreamingUpdateProcessor(Thread, UpdateProcessor):
    def __init__(self, config, requester, store, ready):
        Thread.__init__(self)
        self.daemon = True
        self._uri = config.stream_uri
        self._config = config
        self._requester = requester
        self._store = store
        self._running = False
        self._ready = ready

    # Retry/backoff logic:
    # Upon any error establishing the stream connection we retry with backoff + jitter.
    # Upon any error processing the results of the stream we reconnect after one second.
    def run(self):
        log.info("Starting StreamingUpdateProcessor connecting to uri: " + self._uri)
        self._running = True
        while self._running:
            try:
                messages = self._connect()
                for msg in messages:
                    if not self._running:
                        break
                    message_ok = self.process_message(self._store, self._requester, msg)
                    if message_ok is True and self._ready.is_set() is False:
                        log.info("StreamingUpdateProcessor initialized ok.")
                        self._ready.set()
            except Exception:
                log.warning("Caught exception. Restarting stream connection after one second.",
                            exc_info=True)
                time.sleep(1)

    def _backoff_expo():
        return backoff.expo(max_value=30)

    @backoff.on_exception(_backoff_expo, BaseException, max_tries=None, jitter=backoff.full_jitter)
    def _connect(self):
        return SSEClient(
            self._uri,
            verify=self._config.verify_ssl,
            headers=_stream_headers(self._config.sdk_key),
            connect_timeout=self._config.connect_timeout,
            read_timeout=stream_read_timeout)

    def stop(self):
        log.info("Stopping StreamingUpdateProcessor")
        self._running = False

    def initialized(self):
        return self._running and self._ready.is_set() is True and self._store.initialized is True

    # Returns True if we initialized the feature store
    @staticmethod
    def process_message(store, requester, msg):
        if msg.event == 'put':
            flags = json.loads(msg.data)
            versions_summary = list(map(lambda f: "{0}:{1}".format(f.get("key"), f.get("version")), flags.values()))
            log.debug("Received put event with {0} flags and versions: {1}".format(len(flags), versions_summary))
            store.init(flags)
            return True
        elif msg.event == 'patch':
            payload = json.loads(msg.data)
            key = payload['path'][1:]
            flag = payload['data']
            log.debug("Received patch event for flag key: [{0}] New version: [{1}]"
                      .format(flag.get("key"), str(flag.get("version"))))
            store.upsert(key, flag)
        elif msg.event == "indirect/patch":
            key = msg.data
            log.debug("Received indirect/patch event for flag key: " + key)
            store.upsert(key, requester.get_one(key))
        elif msg.event == "indirect/put":
            log.debug("Received indirect/put event")
            store.init(requester.get_all())
            return True
        elif msg.event == 'delete':
            payload = json.loads(msg.data)
            key = payload['path'][1:]
            # noinspection PyShadowingNames
            version = payload['version']
            log.debug("Received delete event for flag key: [{0}] New version: [{1}]"
                      .format(key, version))
            store.delete(key, version)
        else:
            log.warning('Unhandled event in stream processor: ' + msg.event)
        return False
