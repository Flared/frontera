# -*- coding: utf-8 -*-
from __future__ import absolute_import

from logging import getLogger
from time import sleep

import six
from kafka import KafkaConsumer, KafkaProducer, TopicPartition

from frontera.contrib.backends.partitioners import FingerprintPartitioner, Crc32NamePartitioner
from frontera.contrib.messagebus.kafka.async import OffsetsFetcherAsync
from frontera.core.messagebus import BaseMessageBus, BaseSpiderLogStream, BaseSpiderFeedStream, \
    BaseStreamConsumer, BaseScoringLogStream, BaseStreamProducer
from frontera.utils.misc import load_object
from twisted.internet.task import LoopingCall
from traceback import format_tb


logger = getLogger("messagebus.kafka")


class Consumer(BaseStreamConsumer):
    """
    Used in DB and SW worker. SW consumes per partition.
    """
    def __init__(self, location, topic, group, partition_id):
        self._location = location
        self._group = group
        self._topic = topic
        self._consumer = KafkaConsumer(
            bootstrap_servers=self._location,
            group_id=self._group,
            max_partition_fetch_bytes=10485760,
            consumer_timeout_ms=100,
            client_id="%s-%s" % (self._topic, str(partition_id) if partition_id is not None else "all"),
            request_timeout_ms=120 * 1000,
        )

        if partition_id is not None:
            self._partition_ids = [TopicPartition(self._topic, partition_id)]
            self._consumer.assign(self._partition_ids)
        else:
            self._partition_ids = [TopicPartition(self._topic, pid) for pid in self._consumer.partitions_for_topic(self._topic)]
            self._consumer.subscribe(topics=[self._topic])
            if self._consumer._use_consumer_group():
                self._consumer._coordinator.ensure_coordinator_known()
                self._consumer._coordinator.ensure_active_group()

        self._consumer._update_fetch_positions(self._partition_ids)
        self._start_looping_call()

    def _start_looping_call(self, interval=60):
        def errback(failure):
            logger.exception(failure.value)
            if failure.frames:
                logger.critical(str("").join(format_tb(failure.getTracebackObject())))
            self._poll_task.start(interval).addErrback(errback)

        self._poll_task = LoopingCall(self._poll_client)
        self._poll_task.start(interval).addErrback(errback)

    def _poll_client(self):
        self._consumer._client.poll()

    def get_messages(self, timeout=0.1, count=1):
        result = []
        while count > 0:
            try:
                m = next(self._consumer)
                result.append(m.value)
                count -= 1
            except StopIteration:
                break
        return result

    def get_offset(self, partition_id):
        for tp in self._partition_ids:
            if tp.partition == partition_id:
                return self._consumer.position(tp)
        raise KeyError("Can't find partition %d", partition_id)

    def close(self):
        self._poll_task.stop()
        self._consumer.commit()
        # getting kafka client event loop running some more and execute commit
        tries = 3
        while tries:
            self.get_messages()
            sleep(2.0)
            tries -= 1
        self._consumer.close()


class SimpleProducer(BaseStreamProducer):
    def __init__(self, location, topic, compression):
        self._location = location
        self._topic = topic
        self._compression = compression
        self._create()

    def _create(self):
        self._producer = KafkaProducer(bootstrap_servers=self._location, retries=5,
                                       compression_type=self._compression)

    def send(self, key, *messages):
        for msg in messages:
            self._producer.send(self._topic, value=msg)

    def flush(self):
        self._producer.flush()

    def close(self):
        self._producer.close()


class KeyedProducer(BaseStreamProducer):
    def __init__(self, location, topic_done, partitioner, compression):
        self._location = location
        self._topic_done = topic_done
        self._partitioner = partitioner
        self._compression = compression
        self._producer = KafkaProducer(bootstrap_servers=self._location, partitioner=partitioner, retries=5,
                                       compression_type=self._compression)

    def send(self, key, *messages):
        for msg in messages:
            self._producer.send(self._topic_done, key=key, value=msg)

    def flush(self):
        self._producer.flush()

    def get_offset(self, partition_id):
        pass

    def partition(self, key):
        return self._partitioner(key)


class SpiderLogStream(BaseSpiderLogStream):
    def __init__(self, messagebus):
        self._location = messagebus.kafka_location
        self._db_group = messagebus.spiderlog_dbw_group
        self._sw_group = messagebus.spiderlog_sw_group
        self._topic = messagebus.topic_done
        self._codec = messagebus.codec
        self._partitioner = messagebus.spider_log_partitioner

    def producer(self):
        return KeyedProducer(self._location, self._topic, self._partitioner,
                             self._codec)

    def consumer(self, partition_id, type):
        """
        Creates spider log consumer with BaseStreamConsumer interface
        :param partition_id: can be None or integer
        :param type: either 'db' or 'sw'
        :return:
        """
        group = self._sw_group if type == b'sw' else self._db_group
        c = Consumer(self._location, self._topic, group, partition_id)
        assert len(c._consumer.partitions_for_topic(self._topic)) == len(self._partitioner.partitions)
        return c


class SpiderFeedStream(BaseSpiderFeedStream):
    def __init__(self, messagebus):
        self._location = messagebus.kafka_location
        self._general_group = messagebus.spider_feed_group
        self._topic = messagebus.topic_todo
        self._max_next_requests = messagebus.max_next_requests
        self._offset_fetcher = OffsetsFetcherAsync(bootstrap_servers=self._location, topic=self._topic,
                                                   group_id=self._general_group)
        self._codec = messagebus.codec
        self._partitioner = messagebus.spider_feed_partitioner

    def consumer(self, partition_id):
        c = Consumer(self._location, self._topic, self._general_group, partition_id)
        assert len(c._consumer.partitions_for_topic(self._topic)) == len(self._partitioner.partitions)
        return c

    def available_partitions(self):
        partitions = []
        lags = self._offset_fetcher.get()
        for partition, lag in six.iteritems(lags):
            if lag < self._max_next_requests:
                partitions.append(partition)
        return partitions

    def producer(self):
        return KeyedProducer(self._location, self._topic, self._partitioner, self._codec)


class ScoringLogStream(BaseScoringLogStream):
    def __init__(self, messagebus):
        self._topic = messagebus.topic_scoring
        self._group = messagebus.scoringlog_dbw_group
        self._location = messagebus.kafka_location
        self._codec = messagebus.codec

    def consumer(self):
        return Consumer(self._location, self._topic, self._group, partition_id=None)

    def producer(self):
        return SimpleProducer(self._location, self._topic, self._codec)


class MessageBus(BaseMessageBus):
    def __init__(self, settings):
        self.topic_todo = settings.get('SPIDER_FEED_TOPIC')
        self.topic_done = settings.get('SPIDER_LOG_TOPIC')
        self.topic_scoring = settings.get('SCORING_LOG_TOPIC')

        self.spiderlog_dbw_group = settings.get('SPIDER_LOG_DBW_GROUP')
        self.spiderlog_sw_group = settings.get('SPIDER_LOG_SW_GROUP')
        self.scoringlog_dbw_group = settings.get('SCORING_LOG_DBW_GROUP')
        self.spider_feed_group = settings.get('SPIDER_FEED_GROUP')
        self.spider_partition_id = settings.get('SPIDER_PARTITION_ID')
        self.max_next_requests = settings.MAX_NEXT_REQUESTS
        self.codec = settings.get('KAFKA_CODEC')
        self.kafka_location = settings.get('KAFKA_LOCATION')

        if settings.get('QUEUE_HOSTNAME_PARTITIONING'):
            logger.warning('QUEUE_HOSTNAME_PARTITIONING is deprecated, use SPIDER_FEED_PARTITIONER instead.')
            settings.set('SPIDER_FEED_PARTITIONER', 'frontera.contrib.backends.partitioners.Crc32NamePartitioner')

        spider_log_partitions = list(range(settings.get('SPIDER_LOG_PARTITIONS')))
        spider_log_partitioner_cls = load_object(settings.get('SPIDER_LOG_PARTITIONER'))
        self.spider_log_partitioner = spider_log_partitioner_cls(spider_log_partitions)

        spider_feed_partitions = list(range(settings.get('SPIDER_FEED_PARTITIONS')))
        spider_feed_partitioner_cls = load_object(settings.get('SPIDER_FEED_PARTITIONER'))
        self.spider_feed_partitioner = spider_feed_partitioner_cls(spider_feed_partitions)

    def spider_log(self):
        return SpiderLogStream(self)

    def spider_feed(self):
        return SpiderFeedStream(self)

    def scoring_log(self):
        return ScoringLogStream(self)
