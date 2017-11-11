# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:

# Copyright (c) 2016, Battelle Memorial Institute
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in
#    the documentation and/or other materials provided with the
#    distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation
# are those of the authors and should not be interpreted as representing
# official policies, either expressed or implied, of the FreeBSD
# Project.
#
# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor Battelle,
# nor any of their employees, nor any jurisdiction or organization that
# has cooperated in the development of these materials, makes any
# warranty, express or implied, or assumes any legal liability or
# responsibility for the accuracy, completeness, or usefulness or any
# information, apparatus, product, software, or process disclosed, or
# represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does not
# necessarily constitute or imply its endorsement, recommendation, or
# favoring by the United States Government or any agency thereof, or
# Battelle Memorial Institute. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY
# operated by BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830
# }}}
import csv
import datetime
from dateutil import parser
import logging
import os
import re
import sys

from volttron.platform.vip.agent import *
from volttron.platform.agent import utils
from volttron.platform.messaging.utils import normtopic
from volttron.platform.messaging import headers as headers_mod
import gevent

from collections import defaultdict

utils.setup_logging()
_log = logging.getLogger(__name__)
__version__ = '4.0.0'

HEADER_NAME_DATE = headers_mod.DATE
HEADER_NAME_TIMESTAMP = headers_mod.TIMESTAMP
HEADER_NAME_CONTENT_TYPE = headers_mod.CONTENT_TYPE
SCHEDULE_RESPONSE_SUCCESS = 'SUCCESS'
SCHEDULE_RESPONSE_FAILURE = 'FAILURE'
SCHEDULE_ACTION_NEW = 'NEW_SCHEDULE'
SCHEDULE_ACTION_CANCEL = 'CANCEL_SCHEDULE'

__authors__ = ['Robert Lutes <robert.lutes@pnnl.gov>',
               'Kyle Monson <kyle.monson@pnnl.gov>',
               'Craig Allwardt <craig.allwardt@pnnl.gov>']
__copyright__ = 'Copyright (c) 2016, Battelle Memorial Institute'
__license__ = 'FreeBSD'


def DataPub(config_path, **kwargs):
    '''Emulate device driver to publish data and Actuatoragent for testing.

    The first column in the data file must be the timestamp and it is not
    published to the bus unless the config option:
    'use_timestamp' - True will use timestamp in input file.
    timestamps. False will use the current now time and publish using it.
    '''

    conf = utils.load_config(config_path)
    _log.debug(str(conf))
    use_timestamp = conf.get('use_timestamp', True)
    remember_playback = conf.get('remember_playback', False)
    reset_playback = conf.get('reset_playback', False)

    publish_interval = float(conf.get('publish_interval', 5))

    base_path = conf.get('basepath', "")

    input_data = conf.get('input_data', [])

    # unittype_map maps the point name to the proper units.
    unittype_map = conf.get('unittype_map', {})
    
    # should we keep playing the file over and over again.
    replay_data = conf.get('replay_data', False)

    max_data_frequency = conf.get("max_data_frequency")

    return Publisher(use_timestamp=use_timestamp,
                     publish_interval=publish_interval,
                     base_path=base_path,
                     input_data=input_data,
                     unittype_map=unittype_map,
                     max_data_frequency=max_data_frequency,
                     replay_data=replay_data,
                     **kwargs)


class Publisher(Agent):
    '''Simulate real device.  Publish csv data to message bus.

    Configuration consists of csv file and publish topic
    '''
    def __init__(self, use_timestamp=False,
                 publish_interval=5.0, base_path="", input_data=[], unittype_map={},
                 max_data_frequency=None, replay_data=False,
                 **kwargs):
        '''Initialize data publisher class attributes.'''
        super(Publisher, self).__init__(**kwargs)

        self._meta_data = {}  # maps from device topic to meta data map.
        self._name_map = {}  # maps from column name to (device topic, point name)
        self._data = []  # incoming data
        self._publish_interval = publish_interval
        self._use_timestamp = use_timestamp
        self._loop_greenlet = None
        self._next_allowed_publish = None
        self._max_data_frequency = None
        self._replay_data = False
        self._input_data = None



        self.default_config = {"use_timestamp": use_timestamp,
                               "publish_interval": publish_interval,
                               "base_path": base_path,
                               "input_data": input_data,
                               "unittype_map": unittype_map,
                               "replay_data": replay_data,
                               "max_data_frequency": max_data_frequency}

        self.vip.config.set_default("config", self.default_config)
        self.vip.config.subscribe(self.configure, actions=["NEW"], pattern="config")
        self.vip.config.subscribe(self.config_error, actions=["UPDATE"], pattern="config")

    def config_error(self, config_name, action, contents):
        _log.error("Currently the data publisher must be restarted for changes to take effect.")

    def configure(self, config_name, action, contents):
        config = self.default_config.copy()
        config.update(contents)

        if self._loop_greenlet is not None:
            self._loop_greenlet.kill()

        _log.info('Config Data: {}'.format(config))

        base_path = config.get("base_path", "")
        unittype_map = config.get("unittype_map", {})

        self._input_data = config.get("input_data", [])

        self._publish_interval = config.get("publish_interval", 5.0)
        self._use_timestamp = config.get("use_timestamp", False)

        self._max_data_frequency = config.get("max_data_frequency", None)

        self._replay_data = bool(config.get("replay_data", False))

        if self._max_data_frequency is not None:
            self._max_data_frequency = datetime.timedelta(seconds=self._max_data_frequency)

        names = []
        if isinstance(self._input_data, list):
            if self._input_data:
                item = self._input_data[0]
                names = item.keys()
            self._data = self._input_data
        else:
            handle = open(self._input_data, 'rb')
            self._data = csv.DictReader(handle)
            names = self._data.fieldnames[:]

        self._name_map = self.build_maps(names, base_path)
        self._meta_data = self.build_metadata(self._name_map, unittype_map)

        self._loop_greenlet = self.core.spawn(self.publish_loop)

    @staticmethod
    def build_metadata(name_map, unittype_map):
        results = defaultdict(dict)
        for topic, point in name_map.itervalues():
            unit_type = Publisher._get_unit(point, unittype_map)
            results[topic][point] = unit_type
        return results

    @staticmethod
    def build_maps(names, base_path):
        results = {}
        for name in names:
            if name == "Timestamp":
                continue
            name_parts = name.split("/")
            point = name_parts[-1]
            topic = normtopic(base_path + '/' + "/".join(name_parts[:-1]))

            results[name] = (topic, point)

        return results

    @staticmethod
    def _get_unit(point, unittype_map):
        ''' Get a unit type based upon the regular expression in the config file.

            if NOT found returns percent as a default unit.
        '''
        for k, v in unittype_map.items():
            if re.match(k, point):
                return v
        return 'percent'

    def _publish_point_all(self, topic, data, meta_data, headers):
        # makesure topic+point gives a true value.
        all_topic = topic + "/all"

        message = [data, meta_data]

        self.vip.pubsub.publish(peer='pubsub',
                                topic=all_topic,
                                message=message,  # [data, {'source': 'publisher3'}],
                                headers=headers).get(timeout=2)

    def build_publishes(self, row):
        results = defaultdict(dict)
        for name, value in row.iteritems():
            topic, point = self._name_map[name]
            parsed_value = float(value)
            results[topic][point] = parsed_value
        return results


    def check_frequency(self, now):
        """Check to see if the passed in timestamp exceeds the configured
        max_data_frequency."""
        if self._max_data_frequency is None:
            return True

        now = utils.parse_timestamp_string(now)

        if self._next_allowed_publish is None:
            midnight = now.date()
            midnight = datetime.datetime.combine(midnight, datetime.time.min)
            self._next_allowed_publish = midnight
            while now > self._next_allowed_publish:
                self._next_allowed_publish += self._max_data_frequency

        if now < self._next_allowed_publish:
            return False

        while now >= self._next_allowed_publish:
            self._next_allowed_publish += self._max_data_frequency

        return True

    def publish_loop(self):
        """Publish data from file to message bus."""
        while True:
            for row in self._data:
                if self._use_timestamp and "Timestamp" in row:
                    now = row['Timestamp']
                    if not self.check_frequency(now):
                        continue
                else:
                    now = datetime.datetime.now().isoformat(' ')

                headers = {HEADER_NAME_DATE: now, HEADER_NAME_TIMESTAMP: now}
                row.pop('Timestamp', None)

                publish_dict = self.build_publishes(row)

                _log.debug("Publishing data for timestamp: {}".format(now))

                for topic, message in publish_dict.iteritems():
                    self._publish_point_all(topic, message, self._meta_data, headers)

                gevent.sleep(self._publish_interval)

            if not self._replay_data:
                break

            # Reset the csv reader if we are reading from a file.
            _log.debug("Restarting playback.")
            # Reset data frequency counter.
            self._next_allowed_publish = None
            if not isinstance(self._input_data, list):
                handle = open(self._input_data, 'rb')
                self._data = csv.DictReader(handle)

    @RPC.export
    def set_point(self, requester_id, topic, value, **kwargs):
        requester_id = bytes(self.vip.rpc.context.vip_message.peer)
        _log.info("Set point: {} {} {}".format(requester_id, topic, value))
        return None

    @RPC.export
    def set_multiple_points(self, requester_id, topics_values, **kwargs):
        devices = defaultdict(list)
        for topic, value in topics_values:
            topic = topic.strip('/')
            self.set_point(requester_id, topic, value)

        results = {}

        return results

    @RPC.export
    def request_new_schedule(self, requester_id, task_id, priority, requests):
        requester_id = bytes(self.vip.rpc.context.vip_message.peer)
        _log.info("Schedule requested: {} {} {} {}".format(requester_id, task_id, priority, requests))
        results = {'result': 'SUCCESS',
                   'data': {},
                   'info': ""}

        return results

    @RPC.export
    def request_cancel_schedule(self, requester_id, task_id):
        requester_id = bytes(self.vip.rpc.context.vip_message.peer)
        _log.info("Schedule canceled: {} {}".format(requester_id, task_id))
        results = {'result': 'SUCCESS',
                   'data': {},
                   'info': ""}

        return results

    # @Core.receiver('onfinish')
    # def finish(self, sender):
    #     if self._src_file_handle is not None:
    #         try:
    #             self._src_file_handle.close()
    #         except Exception as e:
    #             _log.error(e.message)




def main(argv=sys.argv):
    '''Main method called by the eggsecutable.'''
    utils.vip_main(DataPub, version=__version__)

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
