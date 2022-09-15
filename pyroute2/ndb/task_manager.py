import logging
import time
import traceback
from functools import partial

from pyroute2 import config

from . import schema
from .events import (
    DBMExitException,
    InvalidateHandlerException,
    RescheduleException,
    ShutdownException,
)
from .messages import cmsg_event, cmsg_failed, cmsg_sstart

log = logging.getLogger(__name__)


def Events(*argv):
    for sequence in argv:
        if sequence is not None:
            for item in sequence:
                yield item


class TaskManager:
    def __init__(self, ndb):
        self.ndb = ndb
        self.log = ndb.log
        self._event_map = {}
        self.ctime = self.gctime = time.time()

    def register_handler(self, event, handler):
        if event not in self._event_map:
            self._event_map[event] = []
        self._event_map[event].append(handler)

    def unregister_handler(self, event, handler):
        self._event_map[event].remove(handler)

    @staticmethod
    def default_handler(target, event):
        if isinstance(getattr(event, 'payload', None), Exception):
            raise event.payload
        log.debug('unsupported event ignored: %s' % type(event))

    def check_sources_started(self, _locals, target, event):
        _locals['countdown'] -= 1
        if _locals['countdown'] == 0:
            self.ndb._dbm_ready.set()

    def run(self):
        _locals = {'countdown': len(self.ndb._nl)}

        # init the events map
        event_map = {
            cmsg_event: [lambda t, x: x.payload.set()],
            cmsg_failed: [lambda t, x: (self.ndb.schema.mark(t, 1))],
            cmsg_sstart: [partial(self.check_sources_started, _locals)],
        }
        self._event_map = event_map

        event_queue = self.ndb._event_queue

        try:
            dbconfig = schema.DBConfig()
            dbconfig.provider = schema.DBProvider(self.ndb._db_provider)
            dbconfig.spec = self.ndb._db_spec
            self.ndb.schema = schema.DBSchema(
                dbconfig,
                self.ndb,
                self.ndb._event_queue,
                self._event_map,
                self.ndb._db_rtnl_log,
                self.log.channel('schema'),
            )

        except Exception as e:
            self.ndb._dbm_error = e
            self.ndb._dbm_ready.set()
            return

        for spec in self.ndb._nl:
            spec['event'] = None
            self.ndb.sources.add(**spec)

        for (event, handlers) in self.ndb.schema.event_map.items():
            for handler in handlers:
                self.register_handler(event, handler)

        stop = False
        source = None
        reschedule = []
        while not stop:
            source, events = event_queue.get()
            events = Events(events, reschedule)
            reschedule = []
            try:
                for event in events:
                    handlers = event_map.get(
                        event.__class__, [self.default_handler]
                    )

                    for handler in tuple(handlers):
                        try:
                            target = event['header']['target']
                            handler(target, event)
                        except RescheduleException:
                            if 'rcounter' not in event['header']:
                                event['header']['rcounter'] = 0
                            if event['header']['rcounter'] < 3:
                                event['header']['rcounter'] += 1
                                self.log.debug('reschedule %s' % (event,))
                                reschedule.append(event)
                            else:
                                self.log.error('drop %s' % (event,))
                        except InvalidateHandlerException:
                            try:
                                handlers.remove(handler)
                            except Exception:
                                self.log.error(
                                    'could not invalidate '
                                    'event handler:\n%s'
                                    % traceback.format_exc()
                                )
                        except ShutdownException:
                            stop = True
                            break
                        except DBMExitException:
                            return
                        except Exception:
                            self.log.error(
                                'could not load event:\n%s\n%s'
                                % (event, traceback.format_exc())
                            )
                    if time.time() - self.gctime > config.gc_timeout:
                        self.gctime = time.time()
            except Exception as e:
                self.log.error(f'exception <{e}> in source {source}')
                # restart the target
                try:
                    self.log.debug(f'requesting source {source} restart')
                    self.ndb.sources[source].state.set('restart')
                except KeyError:
                    self.log.debug(f'key error for {source}')
                    pass

        # release all the sources
        for target in tuple(self.ndb.sources.cache):
            source = self.ndb.sources.remove(target, sync=False)
            if source is not None and source.th is not None:
                self.log.debug(f'closing source {source}')
                source.close()
                if self.ndb._db_cleanup:
                    self.log.debug('flush DB for the target %s' % target)
                    self.ndb.schema.flush(target)
                else:
                    self.log.debug('leave DB for debug')

        # close the database
        self.ndb.schema.commit()
        self.ndb.schema.close()

        # close the logging
        for handler in self.log.logger.handlers:
            handler.close()
