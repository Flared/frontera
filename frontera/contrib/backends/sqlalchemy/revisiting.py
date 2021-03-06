# -*- coding: utf-8 -*-
from __future__ import absolute_import
from w3lib.url import to_native_str
import logging
from datetime import datetime, timedelta
from time import time, sleep
from calendar import timegm

from sqlalchemy import Column, BigInteger

from frontera import Request
from frontera.contrib.backends.partitioners import Crc32NamePartitioner
from frontera.contrib.backends.sqlalchemy import SQLAlchemyBackend
from frontera.contrib.backends.sqlalchemy.models import QueueModelMixin, DeclarativeBase
from frontera.core.components import Queue as BaseQueue, States
from frontera.utils.misc import get_crc32
from frontera.utils.url import parse_domain_from_url_fast
from six.moves import range


def utcnow_timestamp():
    d = datetime.utcnow()
    return timegm(d.timetuple())


class RevisitingQueueModel(QueueModelMixin, DeclarativeBase):
    __tablename__ = 'revisiting_queue'

    crawl_at = Column(BigInteger, nullable=False)


def retry_and_rollback(func):
    def func_wrapper(self, *args, **kwargs):
        tries = 5
        while True:
            try:
                return func(self, *args, **kwargs)
            except Exception as exc:
                self.logger.exception(exc)
                self.session.rollback()
                sleep(5)
                tries -= 1
                if tries > 0:
                    self.logger.info("Tries left %i" % tries)
                    continue
                else:
                    raise exc
    return func_wrapper


class RevisitingQueue(BaseQueue):
    def __init__(self, session_cls, queue_cls, partitioner, dequeued_delay):
        self.session = session_cls()
        self.queue_model = queue_cls
        self.logger = logging.getLogger("sqlalchemy.revisiting.queue")
        self.partitioner = partitioner
        assert isinstance(dequeued_delay, timedelta)
        self.dequeued_delay = dequeued_delay.total_seconds()

    def frontier_stop(self):
        self.session.close()

    def get_next_requests(self, max_n_requests, partition_id, **kwargs):
        results = []
        to_save = []
        try:
            for item in self.query_next_requests(max_n_requests, partition_id):
                results.append(self.request_from_record(item))
                item.crawl_at = utcnow_timestamp() + self.dequeued_delay
                to_save.append(item)
            self.session.bulk_save_objects(to_save)
            self.session.commit()
        except Exception as exc:
            self.logger.exception(exc)
            self.session.rollback()
        return results

    def query_next_requests(self, max_n_requests, partition_id):
        return self.session.query(self.queue_model).\
            filter(RevisitingQueueModel.crawl_at <= utcnow_timestamp(),
                   RevisitingQueueModel.partition_id == partition_id).\
            order_by(RevisitingQueueModel.score.desc(), RevisitingQueueModel.crawl_at).\
            limit(max_n_requests)

    def request_from_record(self, item):
        method = 'GET' if not item.method else item.method
        meta = item.meta.copy()
        meta[b'queue_id'] = item.id
        return Request(item.url, method=method, meta=meta, headers=item.headers,
                       cookies=item.cookies)

    @retry_and_rollback
    def schedule(self, batch):
        to_save = []
        for fprint, score, request, schedule in batch:
            if schedule:
                data = self.request_data(fprint, score, request)
                queue_id = request.meta.get(b'queue_id')
                if queue_id:
                    self.session.query(self.queue_model).filter_by(id=queue_id).update(data)
                else:
                    q = self.queue_model(**data)
                    to_save.append(q)
                request.meta[b'state'] = States.QUEUED
        self.session.bulk_save_objects(to_save)
        self.session.commit()

    def request_data(self, fprint, score, request):
        key = self.partitioner.get_key(request)
        _, hostname, _, _, _, _ = parse_domain_from_url_fast(request.url)
        host_crc32 = get_crc32(hostname) if hostname else 0
        if key is None:
            self.logger.error("Can't get partition key for URL %s, fingerprint %s" % (request.url, fprint))
            partition_id = self.partitioner.partitions[0]
        else:
            partition_id = self.partitioner.partition(key)
        schedule_at = request.meta[b'crawl_at'] if b'crawl_at' in request.meta else utcnow_timestamp()
        return dict(
            fingerprint=to_native_str(fprint), score=score, url=to_native_str(request.url),
            meta=request.meta, headers=request.headers, cookies=request.cookies,
            method=to_native_str(request.method), partition_id=partition_id, host_crc32=host_crc32,
            created_at=time()*1E+6, crawl_at=schedule_at)

    @retry_and_rollback
    def count(self):
        return self.session.query(self.queue_model).count()


class Backend(SQLAlchemyBackend):
    def __init__(self, manager):
        super(Backend, self).__init__(manager)
        settings = manager.settings
        self.interval = settings.get("SQLALCHEMYBACKEND_REVISIT_INTERVAL")
        assert isinstance(self.interval, timedelta)
        self.interval = self.interval.total_seconds()

    def _create_queue(self, settings):
        return RevisitingQueue(
            self.session_cls,
            RevisitingQueueModel,
            self.partitioner,
            settings.get('SQLALCHEMYBACKEND_DEQUEUED_DELAY'))

    def _schedule(self, requests):
        batch = []
        for request in requests:
            if request.meta[b'state'] in [States.NOT_CRAWLED]:
                request.meta[b'crawl_at'] = utcnow_timestamp()
            elif request.meta[b'state'] in [States.CRAWLED, States.ERROR]:
                request.meta[b'crawl_at'] = utcnow_timestamp() + self.interval
            else:
                continue    # QUEUED
            batch.append((request.meta[b'fingerprint'], self._get_score(request), request, True))
        self.queue.schedule(batch)
        self.metadata.update_score(batch)
        self.queue_size += len(batch)

    def page_crawled(self, response):
        super(Backend, self).page_crawled(response)
        self.states.set_states(response.request)
        self._schedule([response.request])
        self.states.update_cache(response.request)
